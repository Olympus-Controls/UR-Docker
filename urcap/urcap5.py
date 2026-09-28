#!/usr/bin/env python3
"""Build and install the PolyScope 5 (e-Series) URCap (``.urcap``) — stdlib + a JDK.

A PolyScope 5 URCap is an OSGi bundle jar. UR's SDK builds it with Maven and the
maven-bundle-plugin; this does the same steps with ``javac`` / ``jdeps`` and
:mod:`zipfile`, the way ``urcapx.py`` replaces the PolyScope X npm toolchain:

* ``sdk [--image IMG]`` — the URCap API jars are not on Maven Central, and UR's SDK
  download sits behind a login, but every PolyScope 5 ships them as OSGi bundles in
  ``/ursim/GUI/bundle``. This copies the ones the URCap compiles against out of the
  e-Series URSim image into ``target/urcap5-sdk/`` (git-ignored; never committed —
  they are UR's).
* ``package SRC [--out DIR]`` — compiles ``SRC/src`` for Java 8 (PolyScope 5.26 runs
  1.8.0_371), derives ``Import-Package`` from the classes with ``jdeps`` (every
  non-``java.*`` package; ``com.ur.urcap.api.*`` as ``[1.0.0,2.0.0)``), writes the
  manifest from ``SRC/bundle.properties`` plus the two compatibility flags PolyScope's
  loader requires, embeds ``META-INF/maven/<group>/<artifact>/pom.xml`` naming the
  ``com.ur.urcap:api`` version (the file PolyScope reads the API version from), and
  zips it reproducibly: sorted entries, fixed timestamps, the manifest first. The jar
  also carries ``META-INF/urcap5-sources.sha256`` — a digest of the sources — so a
  test can tell a stale committed ``dist/`` without a JDK.
* ``install FILE --container NAME`` — the e-Series URSim image copies ``/urcaps/*.jar``
  into its bundle directory at start (``/entrypoint.sh``), so this copies the jar
  there and restarts the container. On a real robot, install from a USB stick:
  Settings → System → URCaps → ``+``.

    python3 urcap/urcap5.py sdk
    python3 urcap/urcap5.py package urcap/realsense-pilot-ps5 --out urcap/dist
    python3 urcap/urcap5.py install urcap/dist/realsense-pilot-ps5-0.2.0.urcap \
        --container ur-utils-ursim-e-ur3e
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_DIR = REPO_ROOT / "target" / "urcap5-sdk"
DEFAULT_IMAGE = "universalrobots/ursim_e-series:5.26.0"
# The bundles the URCap compiles against, by file-name prefix (versions vary by release).
SDK_JARS = (
    "polyscope-urcap-urcap-api-",  # com.ur.urcap.api.contribution*, domain*, userinteraction*
    "urcap-api-export-robotmovement-",  # ...userinteraction.robot.movement
    "polyscope-value-objects-urcap-api-",  # ...domain.value.*
    "osgi.core-",  # org.osgi.framework
    # DataModel's get/set overloads name these types, so javac needs them to pick one
    "polyscope-payload-urcap-api-",
    "polyscope-geom-feature-urcap-api-",
    "polyscope-tcp-urcap-api-",
)
API_RANGE = "[1.0.0,2.0.0)"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)  # zip entries' timestamp: the build is content-only
DIGEST_ENTRY = "META-INF/urcap5-sources.sha256"
REQUIRED_KEYS = (
    "Bundle-SymbolicName",
    "Bundle-Name",
    "Bundle-Vendor",
    "Bundle-Version",
    "Bundle-Activator",
    "URCapCompatibility-CB3",
    "URCapCompatibility-eSeries",
    "urcap.api.version",
)


class Urcap5Error(RuntimeError):
    pass


# -- properties + manifest ------------------------------------------------------------------


def read_properties(text: str) -> dict[str, str]:
    """``key=value`` lines; ``#`` comments and blank lines ignored."""
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not key.strip():
            raise Urcap5Error(f"bundle.properties line {n}: expected key=value")
        out[key.strip()] = value.strip()
    missing = [k for k in REQUIRED_KEYS if not out.get(k)]
    if missing:
        raise Urcap5Error(f"bundle.properties is missing {', '.join(missing)}")
    for flag in ("URCapCompatibility-CB3", "URCapCompatibility-eSeries"):
        if out[flag] not in ("true", "false"):
            raise Urcap5Error(f"{flag} must be true or false")
    if out["URCapCompatibility-CB3"] == out["URCapCompatibility-eSeries"] == "false":
        raise Urcap5Error("a URCap compatible with neither CB3 nor e-Series will not load")
    return out


def manifest_line(key: str, value: str) -> bytes:
    """One JAR-manifest header, wrapped at 72 bytes with a leading-space continuation."""
    raw = f"{key}: {value}".encode()
    lines = [raw[:72]]
    rest = raw[72:]
    while rest:
        lines.append(b" " + rest[:71])
        rest = rest[71:]
    return b"\r\n".join(lines) + b"\r\n"


def import_package(packages: list[str]) -> str:
    """``Import-Package`` for the packages the classes reference (``java.*`` is never
    imported in OSGi; the URCap API gets a major-version range)."""
    parts = []
    for pkg in sorted(set(packages)):
        if pkg.startswith("java.") or pkg == "java":
            continue
        parts.append(f'{pkg};version="{API_RANGE}"' if pkg.startswith("com.ur.urcap.api") else pkg)
    return ",".join(parts)


def build_manifest(props: dict[str, str], packages: list[str]) -> bytes:
    headers = [
        ("Manifest-Version", "1.0"),
        ("Bundle-ManifestVersion", "2"),
        ("Bundle-SymbolicName", props["Bundle-SymbolicName"]),
        ("Bundle-Name", props["Bundle-Name"]),
        ("Bundle-Vendor", props["Bundle-Vendor"]),
        ("Bundle-Version", props["Bundle-Version"]),
        ("Bundle-Activator", props["Bundle-Activator"]),
        # PolyScope 5's installer refuses a jar without it (URCapsServiceImpl.isValidFile).
        ("Bundle-Category", "URCap"),
        ("Bundle-RequiredExecutionEnvironment", "JavaSE-1.8"),
        ("Import-Package", import_package(packages)),
        ("URCapCompatibility-CB3", props["URCapCompatibility-CB3"]),
        ("URCapCompatibility-eSeries", props["URCapCompatibility-eSeries"]),
    ]
    if props.get("Bundle-Description"):
        headers.insert(6, ("Bundle-Description", props["Bundle-Description"]))
    return b"".join(manifest_line(k, v) for k, v in headers) + b"\r\n"


def pom_xml(props: dict[str, str]) -> tuple[str, bytes]:
    """The embedded pom PolyScope 5 reads the URCap API version from."""
    sym = props["Bundle-SymbolicName"]
    group, _, artifact = sym.rpartition(".")
    group = group or sym
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<project xmlns="http://maven.apache.org/POM/4.0.0">\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        f"  <groupId>{group}</groupId>\n"
        f"  <artifactId>{artifact}</artifactId>\n"
        f"  <version>{props['Bundle-Version']}</version>\n"
        "  <packaging>bundle</packaging>\n"
        f"  <name>{props['Bundle-Name']}</name>\n"
        "  <dependencies>\n"
        "    <dependency>\n"
        "      <groupId>com.ur.urcap</groupId>\n"
        "      <artifactId>api</artifactId>\n"
        f"      <version>{props['urcap.api.version']}</version>\n"
        "      <scope>provided</scope>\n"
        "    </dependency>\n"
        "  </dependencies>\n"
        "</project>\n"
    )
    return f"META-INF/maven/{group}/{artifact}/pom.xml", body.encode()


# -- sources + sdk --------------------------------------------------------------------------


def sources_digest(src: Path) -> str:
    """SHA-256 over ``bundle.properties`` and every ``src/**/*.java`` (path + bytes, sorted)."""
    h = hashlib.sha256()
    files = [src / "bundle.properties", *sorted((src / "src").rglob("*.java"))]
    for f in files:
        rel = f.relative_to(src).as_posix()
        h.update(rel.encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


def sdk_jars(sdk_dir: Path = SDK_DIR) -> list[Path]:
    jars = []
    for prefix in SDK_JARS:
        found = sorted(sdk_dir.glob(prefix + "*.jar"))
        if not found:
            raise Urcap5Error(
                f"{prefix}*.jar missing from {sdk_dir} — run `python3 urcap/urcap5.py sdk` first"
            )
        jars.append(found[-1])
    return jars


def fetch_sdk(image: str = DEFAULT_IMAGE, sdk_dir: Path = SDK_DIR) -> list[Path]:
    """Copy the API bundles out of a PolyScope 5 URSim image (docker create + cp; nothing runs)."""
    if not shutil.which("docker"):
        raise Urcap5Error("docker is not on PATH (needed to read the jars out of the URSim image)")
    sdk_dir.mkdir(parents=True, exist_ok=True)
    cid = subprocess.run(
        ["docker", "create", "--platform", "linux/amd64", image], capture_output=True, text=True, check=False
    )
    if cid.returncode != 0:
        raise Urcap5Error(f"docker create {image} failed: {cid.stderr.strip()}")
    container = cid.stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            cp = subprocess.run(
                ["docker", "cp", f"{container}:/ursim/GUI/bundle", tmp],
                capture_output=True,
                text=True,
                check=False,
            )
            if cp.returncode != 0:
                raise Urcap5Error(f"docker cp failed: {cp.stderr.strip()}")
            bundle = Path(tmp) / "bundle"
            for prefix in SDK_JARS:
                found = sorted(bundle.glob(prefix + "*.jar"))
                if not found:
                    raise Urcap5Error(f"{image} has no {prefix}*.jar in /ursim/GUI/bundle")
                shutil.copy2(found[-1], sdk_dir / found[-1].name)
    finally:
        subprocess.run(["docker", "rm", container], capture_output=True, check=False)
    return sdk_jars(sdk_dir)


# -- package --------------------------------------------------------------------------------


def _java_tool(name: str) -> str:
    tool = shutil.which(name)
    if not tool:
        raise Urcap5Error(f"{name} is not on PATH (a JDK 11+ builds for Java 8 with --release 8)")
    return tool


def referenced_packages(classes: Path, classpath: list[Path]) -> list[str]:
    """Packages the compiled classes use, from ``jdeps -verbose:package``."""
    out = subprocess.run(
        [_java_tool("jdeps"), "-verbose:package", "-cp", ":".join(str(p) for p in classpath), str(classes)],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise Urcap5Error(f"jdeps failed: {out.stderr.strip() or out.stdout.strip()}")
    pkgs = set()
    for line in out.stdout.splitlines():
        if "->" not in line:
            continue
        target = line.split("->", 1)[1].split()
        if target and "." in target[0] and "/" not in target[0] and not target[0].endswith(".jar"):
            pkgs.add(target[0])
    return sorted(pkgs)


def package(src: str | Path, out_dir: str | Path, *, sdk_dir: Path = SDK_DIR) -> Path:
    src, out_dir = Path(src), Path(out_dir)
    props = read_properties((src / "bundle.properties").read_text(encoding="utf-8"))
    java = sorted((src / "src").rglob("*.java"))
    if not java:
        raise Urcap5Error(f"no Java sources under {src / 'src'}")
    jars = sdk_jars(sdk_dir)
    own = props["Bundle-SymbolicName"].rpartition(".")[0]
    with tempfile.TemporaryDirectory() as tmp:
        classes = Path(tmp) / "classes"
        classes.mkdir()
        cc = subprocess.run(
            [
                _java_tool("javac"),
                "--release",
                "8",
                "-Xlint:all",
                "-Xlint:-options",
                "-Werror",
                "-encoding",
                "UTF-8",
                "-cp",
                ":".join(str(j) for j in jars),
                "-d",
                str(classes),
                *[str(f) for f in java],
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if cc.returncode != 0:
            raise Urcap5Error("javac failed:\n" + (cc.stderr or cc.stdout))
        packages = [p for p in referenced_packages(classes, jars) if not p.startswith(own)]
        entries: list[tuple[str, bytes]] = [
            (p.relative_to(classes).as_posix(), p.read_bytes()) for p in sorted(classes.rglob("*.class"))
        ]
    pom_path, pom = pom_xml(props)
    licence = src / "LICENSE" if (src / "LICENSE").is_file() else REPO_ROOT / "LICENSE"
    entries += [(pom_path, pom), (DIGEST_ENTRY, (sources_digest(src) + "\n").encode())]
    if licence.is_file():
        entries.append(("META-INF/LICENSE", licence.read_bytes()))
    out_dir.mkdir(parents=True, exist_ok=True)
    artifact = props["Bundle-SymbolicName"].rpartition(".")[2]
    name = "realsense-pilot-ps5" if artifact == "realsensepilot" else artifact
    out = out_dir / f"{name}-{props['Bundle-Version']}.urcap"
    tmp_out = out.with_suffix(".urcap.tmp")
    with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(zipfile.ZipInfo("META-INF/", FIXED_TIME), b"")
        _write(z, "META-INF/MANIFEST.MF", build_manifest(props, packages))
        for path, data in sorted(entries):
            _write(z, path, data)
    tmp_out.replace(out)
    return out


def _write(z: zipfile.ZipFile, path: str, data: bytes) -> None:
    info = zipfile.ZipInfo(path, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    z.writestr(info, data)


def read_bundle(path: str | Path) -> dict:
    """The manifest headers, the embedded API version and the sources digest of a ``.urcap``."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        raw = z.read("META-INF/MANIFEST.MF").decode()
        headers: dict[str, str] = {}
        key = None
        for line in raw.replace("\r\n", "\n").split("\n"):
            if line.startswith(" ") and key:
                headers[key] += line[1:]
            elif ": " in line:
                key, _, value = line.partition(": ")
                headers[key] = value
        digest = z.read(DIGEST_ENTRY).decode().strip() if DIGEST_ENTRY in names else None
        poms = [n for n in names if n.startswith("META-INF/maven/") and n.endswith("/pom.xml")]
        pom = z.read(poms[0]).decode() if poms else ""
    return {"headers": headers, "names": names, "sources_sha256": digest, "pom": pom}


# -- install --------------------------------------------------------------------------------


def install(path: str | Path, container: str) -> dict:
    """Copy the jar into the URSim container's ``/urcaps`` and restart it (its entrypoint
    copies ``/urcaps/*.jar`` into PolyScope's bundle directory at start)."""
    path = Path(path)
    if not path.is_file():
        raise Urcap5Error(f"{path} does not exist")
    for cmd in (
        ["docker", "cp", str(path), f"{container}:/urcaps/{path.stem}.jar"],
        ["docker", "restart", container],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if r.returncode != 0:
            raise Urcap5Error(f"{' '.join(cmd[:2])} failed: {r.stderr.strip()}")
    return {"ok": True, "container": container, "jar": f"/urcaps/{path.stem}.jar", "restarted": True}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="urcap5", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sd = sub.add_parser("sdk", help="copy the URCap API jars out of a PolyScope 5 URSim image")
    sd.add_argument("--image", default=DEFAULT_IMAGE)
    pk = sub.add_parser("package", help="build SRC into a .urcap")
    pk.add_argument("src")
    pk.add_argument("--out", default="urcap/dist")
    ins = sub.add_parser("install", help="install a .urcap into a running e-Series URSim container")
    ins.add_argument("file")
    ins.add_argument("--container", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "sdk":
            for jar in fetch_sdk(args.image):
                print(jar)
        elif args.cmd == "package":
            print(package(args.src, args.out))
        else:
            print(install(args.file, args.container))
    except Urcap5Error as exc:
        print(f"urcap5: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
