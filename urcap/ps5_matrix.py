#!/usr/bin/env python3
"""The PolyScope 5 URCap on every supported PolyScope 5 release — stdlib + Docker.

For each version in :data:`MATRIX` (the newest patch of each of the newest three
PolyScope 5 minors), against ``docker-compose.ps5-matrix.yml``:

1. stage the committed ``urcap/dist/realsense-pilot-ps5-<ver>.urcap`` as
   ``/urcaps/realsense-pilot-ps5.jar`` (the image's entrypoint copies ``/urcaps/*.jar``
   into PolyScope's bundle dir at every start) and bring the version's service up;
2. wait for the Dashboard to answer a ``robotmode`` other than ``NO_CONTROLLER``;
3. wait for PolyScope to have *started* the bundle (:func:`urcap_signal` on
   ``/ursim/polyscope.log``), failing on any exception that names the URCap's package;
4. power on + brake release, then run ``urcap/pick5_e2e.py`` — the Pick node's own
   URScript on this controller, against a pick server on this machine that the container
   reaches at its network's gateway;
5. on failure copy ``polyscope.log`` / ``URControl.log`` and the evidence out, and always
   ``docker compose down -v``.

One JSON summary per version (``--report`` writes them all).

    python3 urcap/ps5_matrix.py run --version 5.26 --artifacts ps5-artifacts
    python3 urcap/ps5_matrix.py run --version all
    python3 urcap/ps5_matrix.py down --version all
    python3 urcap/ps5_matrix.py ports                 # the host-port table
    python3 urcap/ps5_matrix.py check-tags            # Docker Hub vs MATRIX; exit 1 on drift
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO / "docker-compose.ps5-matrix.yml"
DIST = REPO / "urcap" / "dist"
STAGE = REPO / "target" / "ps5-matrix" / "urcaps"
JAR_NAME = "realsense-pilot-ps5.jar"
IMAGE = "universalrobots/ursim_e-series"
HUB_TAGS = f"https://hub.docker.com/v2/repositories/{IMAGE}/tags?page_size=100"
PACKAGE = "com.olympuscontrols.realsensepilot"
MATRIX_SIZE = 3
# `sudo docker` where the user is not in the docker group (the Makefile passes its DOCKER).
DOCKER = os.environ.get("DOCKER", "docker").split()


@dataclass(frozen=True)
class Version:
    """One matrix entry: the image tag and the host ports its service publishes."""

    tag: str  # the image tag, the newest patch of its minor
    dashboard: int
    primary: int
    rtde: int
    novnc: int
    pick: int  # the pick server's port on the host for this version's run

    @property
    def minor(self) -> str:
        return ".".join(self.tag.split(".")[:2])

    @property
    def service(self) -> str:
        return "ursim-" + self.minor.replace(".", "-")

    @property
    def project(self) -> str:
        return "ur-ps5-matrix-" + self.minor.replace(".", "")


def _version(tag: str, pick: int) -> Version:
    off = 100 * (int(tag.split(".")[1]) - 23)
    return Version(tag, 29999 + off, 30001 + off, 30004 + off, 6080 + off, pick)


# The source of truth for the compose file's ports (tests hold the file to it).
MATRIX: dict[str, Version] = {
    v.minor: v for v in (_version("5.24.0", 7641), _version("5.25.2", 7642), _version("5.26.1", 7643))
}

# Every host port docker-compose.yml (the default dev sims) and the cockpit bind.
DEFAULT_PORTS = frozenset(
    {15900, 6080, 29999, 30000, 30001, 30002, 30003, 30004, 30020, 502, 8000, 31001, 31004}
    | {7621, 7622, 7623, 7631}  # cockpit, its pick server, pick5_e2e's default, the sidecar
)


def host_ports(v: Version) -> list[int]:
    return [v.dashboard, v.primary, v.rtde, v.novnc, v.pick]


# -- Docker Hub tags ------------------------------------------------------------------------

_TAG = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_tag(tag: str) -> tuple[int, int, int] | None:
    """``5.26.1`` → ``(5, 26, 1)``; anything that isn't a full numeric PolyScope 5 tag
    (``latest``, ``5.26``, ``5.26.1-rc``, ``6.0.0``, ``05.1.0``) → None."""
    m = _TAG.fullmatch(tag)
    if not m or m.group(1) != "5" or any(p != str(int(p)) for p in m.groups()):
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def newest_minors(tags: list[str], n: int = MATRIX_SIZE) -> list[str]:
    """The newest patch tag of each of the newest ``n`` PolyScope 5 minors, oldest first."""
    best: dict[int, tuple[int, int, int]] = {}
    for tag in tags:
        v = parse_tag(tag)
        if v and (v[1] not in best or v > best[v[1]]):
            best[v[1]] = v
    chosen = sorted(best.values())[-n:] if n > 0 else []
    return [".".join(map(str, v)) for v in chosen]


def tag_drift(tags: list[str], matrix: list[str]) -> list[str]:
    """What is wrong with ``matrix`` given Docker Hub's ``tags``: one sentence per fix."""
    want = newest_minors(tags, len(matrix))
    have = sorted(matrix, key=lambda t: parse_tag(t) or (0, 0, 0))
    problems = []
    minor = lambda t: ".".join(t.split(".")[:2])  # noqa: E731
    want_minors, have_minors = [minor(t) for t in want], [minor(t) for t in have]
    for t in want:
        if minor(t) not in have_minors:
            drop = next((m for m in have_minors if m not in want_minors), None)
            problems.append(f"PolyScope {minor(t)} exists: add it" + (f" and drop {drop}" if drop else ""))
        elif t not in have:
            old = have[have_minors.index(minor(t))]
            problems.append(f"PolyScope {t} exists: bump {old} to it")
    for t in have:
        if t not in tags:
            problems.append(f"{IMAGE}:{t} is not on Docker Hub")
    return problems


def fetch_tags(url: str = HUB_TAGS) -> list[str]:
    names: list[str] = []
    while url:
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (fixed https URL)
            page = json.load(r)
        names += [t["name"] for t in page.get("results", [])]
        url = page.get("next") or ""
    return names


# -- the URCap's evidence in polyscope.log --------------------------------------------------

# A line naming the bundle (symbolic name, package, or the jar) with an OSGi / PolyScope
# "it is running" verb. Extended from real logs in tests/fixtures/ps5_matrix/.
_OURS = re.compile(r"com\.olympuscontrols|realsense-?pilot", re.I)
_STARTED = re.compile(
    r"\b(start(ed|ing)?|activ(e|ated|ating)|register(ed|ing)?|install(ed|ing)?|load(ed|ing)?)\b", re.I
)
_ERROR = re.compile(r"exception|\berror\b|severe|could not|failed|unresolved", re.I)


def urcap_signal(log: str, package: str = PACKAGE) -> dict:
    """What ``polyscope.log`` says about the URCap: ``{"started": [...], "errors": [...]}``.

    ``errors`` are lines that are both about the URCap and look like a failure, plus any
    Java stack frame inside the URCap's package (``at com.olympuscontrols...``) with the
    exception line that heads it. ``started`` are lines about the URCap with a
    start/activate/register/install verb and no failure word."""
    lines = log.splitlines()
    started: list[str] = []
    errors: list[str] = []
    for i, line in enumerate(lines):
        frame = line.strip().startswith("at ") and package in line
        if frame:
            head = next(
                (lines[j] for j in range(i - 1, -1, -1) if not lines[j].strip().startswith("at ")), ""
            )
            for text in (head.strip(), line.strip()):
                if text and text not in errors:
                    errors.append(text)
            continue
        if not _OURS.search(line):
            continue
        if _ERROR.search(line):
            if line.strip() not in errors:
                errors.append(line.strip())
        elif _STARTED.search(line):
            started.append(line.strip())
    return {"started": started, "errors": errors}


# -- docker ---------------------------------------------------------------------------------


def _compose(
    v: Version, *args: str, check: bool = True, timeout: float = 1800
) -> subprocess.CompletedProcess:
    cmd = [*DOCKER, "compose", "-f", str(COMPOSE_FILE), "-p", v.project, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[3:])} failed: {r.stderr.strip()[-2000:]}")
    return r


def _container(v: Version) -> str:
    return f"{v.project}-{v.service}-1"


def _exec(v: Version, script: str, timeout: float = 60) -> str:
    r = subprocess.run(
        [*DOCKER, "exec", _container(v), "sh", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return r.stdout + (r.stderr if r.returncode else "")


def stage_urcap(dist: Path = DIST, stage: Path = STAGE) -> Path:
    jars = sorted(dist.glob("realsense-pilot-ps5-*.urcap"))
    if len(jars) != 1:
        raise RuntimeError(f"expected one realsense-pilot-ps5-*.urcap in {dist}, found {len(jars)}")
    stage.mkdir(parents=True, exist_ok=True)
    for old in stage.glob("*.jar"):
        old.unlink()
    shutil.copy2(jars[0], stage / JAR_NAME)
    return jars[0]


def dashboard(port: int, command: str, host: str = "127.0.0.1", timeout: float = 5.0) -> str:
    """One Dashboard command; the reply line, or '' when nothing answers."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            f = s.makefile("rwb")
            f.readline()  # banner
            f.write(command.encode() + b"\n")
            f.flush()
            return f.readline().decode(errors="replace").strip()
    except OSError:
        return ""


def wait_for_dashboard(v: Version, timeout: float) -> str:
    t0 = time.monotonic()
    reply = ""
    while time.monotonic() - t0 < timeout:
        reply = dashboard(v.dashboard, "robotmode")
        if reply.startswith("Robotmode:") and "NO_CONTROLLER" not in reply:
            return reply
        time.sleep(3)
    raise TimeoutError(f"Dashboard :{v.dashboard} not ready after {timeout:.0f} s (last reply {reply!r})")


def wait_for_urcap(v: Version, timeout: float) -> dict:
    t0 = time.monotonic()
    sig: dict = {"started": [], "errors": []}
    while time.monotonic() - t0 < timeout:
        sig = urcap_signal(_exec(v, "cat /ursim/polyscope.log 2>/dev/null"))
        if sig["errors"] or sig["started"]:
            break
        time.sleep(3)
    return sig


def gateway(v: Version) -> str:
    """The address the container reaches this machine at: its network's gateway."""
    r = subprocess.run(
        [
            *DOCKER,
            "inspect",
            "-f",
            "{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}",
            _container(v),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    gw = r.stdout.split()
    if not gw:
        raise RuntimeError(f"{_container(v)} has no network gateway")
    return gw[0]


# Run inside the container (sh) when a run fails, or always with PS5_MATRIX_COLLECT.
EVIDENCE = r"""
echo '## polyscope.log lines about URCaps'
grep -n -i -E 'urcap|olympus|realsense' /ursim/polyscope.log | grep -v "Adding 'reference" | tail -100
echo '## /ursim/GUI/bundle (ours)'; ls -la /ursim/GUI/bundle | grep -i realsense
echo '## /ursim/.urcaps'; ls -la /ursim/.urcaps 2>&1
echo '## felix-cache bundle.info naming us'
for d in $(grep -l -i realsense /ursim/GUI/felix-cache/bundle*/bundle.info 2>/dev/null); do
  echo "# $d"; cat -A "$d"
done
echo '## felix / shell config'; ls /ursim/GUI/conf 2>&1
grep -r -n -i -E 'shell|telnet|felix' /ursim/GUI/conf 2>/dev/null | head -40
echo '## listening tcp ports (hex)'
awk 'NR>1 && $4=="0A" {print $2}' /proc/net/tcp /proc/net/tcp6 | sort -u
echo '## felix remote shell ps'
python3 - <<'EOF' 2>&1 | tail -250
import socket, time
s = socket.create_connection(("127.0.0.1", 6666), 3)
time.sleep(1)
s.settimeout(3)
try:
    print(s.recv(4096).decode(errors="replace"))
except OSError as exc:
    print("no banner:", exc)
s.sendall(b"ps\n")
time.sleep(2)
print(s.recv(65536).decode(errors="replace"))
EOF
"""


def collect(v: Version, out: Path) -> list[str]:
    out.mkdir(parents=True, exist_ok=True)
    saved = []
    for src in ("/ursim/polyscope.log", "/ursim/URControl.log"):
        dst = out / Path(src).name
        r = subprocess.run(
            [*DOCKER, "cp", f"{_container(v)}:{src}", str(dst)], capture_output=True, check=False
        )
        if r.returncode == 0:
            saved.append(str(dst))
    (out / "evidence.txt").write_text(_exec(v, EVIDENCE, timeout=120), encoding="utf-8")
    saved.append(str(out / "evidence.txt"))
    logs = _compose(v, "logs", "--no-color", v.service, check=False)
    (out / "container.log").write_text(logs.stdout + logs.stderr, encoding="utf-8")
    saved.append(str(out / "container.log"))
    return saved


def down(v: Version) -> None:
    _compose(v, "down", "-v", "--remove-orphans", check=False, timeout=300)


def run_version(v: Version, artifacts: Path, *, boot_timeout: float, urcap_timeout: float) -> dict:
    report: dict = {"version": v.tag, "service": v.service, "ports": asdict(v), "ok": False, "stages": {}}
    stages = report["stages"]
    t0 = time.monotonic()
    try:
        report["urcap"] = stage_urcap().name
        _compose(v, "pull", "--quiet", v.service)
        _compose(v, "up", "-d", v.service)
        stages["dashboard"] = wait_for_dashboard(v, boot_timeout)
        stages["boot_s"] = round(time.monotonic() - t0, 1)
        sig = wait_for_urcap(v, urcap_timeout)
        stages["urcap_signal"] = sig
        if sig["errors"]:
            raise RuntimeError(f"PolyScope logged {len(sig['errors'])} error line(s) about the URCap")
        if not sig["started"] and not os.environ.get("PS5_MATRIX_DISCOVER"):
            raise RuntimeError(
                f"no sign in polyscope.log that the URCap started within {urcap_timeout:.0f} s"
            )
        sys.path.insert(0, str(REPO))
        from urctl import Robot, RobotConfig

        robot = Robot(
            RobotConfig.from_env(host="127.0.0.1", dashboard_port=v.dashboard, primary_port=v.primary)
        )
        up = robot.bring_up()
        stages["bring_up"] = {"ok": up.get("ok"), "robot_mode": up.get("robot_mode")}
        if not up.get("ok"):
            raise RuntimeError(f"bring-up failed: {up}")
        gw = gateway(v)
        stages["reach_back"] = gw
        cmd = [
            sys.executable,
            str(REPO / "urcap" / "pick5_e2e.py"),
            "--host",
            "127.0.0.1",
            "--dash-port",
            str(v.dashboard),
            "--primary-port",
            str(v.primary),
            "--reach-back",
            gw,
            "--bind",
            gw,
            "--port",
            str(v.pick),
        ]
        env = {**os.environ, "UR_RTDE_PORT": str(v.rtde)}
        e2e = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env, check=False)
        stages["pick_e2e"] = {"exit": e2e.returncode, "output": (e2e.stdout + e2e.stderr).splitlines()[-60:]}
        # A late exception (e.g. when the node's classes are touched at program run) still fails.
        late = urcap_signal(_exec(v, "cat /ursim/polyscope.log 2>/dev/null"))["errors"]
        if late:
            stages["urcap_signal"]["errors"] = late
            raise RuntimeError(f"PolyScope logged {len(late)} error line(s) about the URCap during the run")
        if e2e.returncode != 0:
            raise RuntimeError("pick5_e2e failed")
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001 — every failure becomes the report's reason
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out = artifacts / v.minor
        try:
            if not report["ok"] or os.environ.get("PS5_MATRIX_COLLECT"):
                report["artifacts"] = collect(v, out)
        except Exception as exc:  # noqa: BLE001
            report["collect_error"] = str(exc)
        down(v)
        report["took_s"] = round(time.monotonic() - t0, 1)
    return report


def _selected(name: str) -> list[Version]:
    if name == "all":
        return list(MATRIX.values())
    key = ".".join(name.split(".")[:2])
    if key not in MATRIX:
        raise SystemExit(f"unknown version {name!r}; the matrix is {', '.join(MATRIX)} (or all)")
    return [MATRIX[key]]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ps5_matrix", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rn = sub.add_parser("run", help="boot, check the URCap, run the pick e2e, tear down")
    rn.add_argument("--version", default="all", help="5.24 / 5.25.2 / ... or all")
    rn.add_argument("--artifacts", default="ps5-artifacts", help="where logs go on failure")
    rn.add_argument("--report", help="write the JSON summaries here")
    rn.add_argument("--boot-timeout", type=float, default=600.0)
    rn.add_argument("--urcap-timeout", type=float, default=240.0)
    dn = sub.add_parser("down", help="docker compose down -v")
    dn.add_argument("--version", default="all")
    sub.add_parser("ports", help="print the host-port table as JSON")
    sub.add_parser("check-tags", help="compare MATRIX with Docker Hub's tags")
    sub.add_parser("list", help="print the matrix's minors as a JSON list (for CI)")
    args = ap.parse_args(argv)

    if args.cmd == "ports":
        print(json.dumps({m: asdict(v) | {"service": v.service} for m, v in MATRIX.items()}, indent=2))
        return 0
    if args.cmd == "list":
        print(json.dumps(list(MATRIX)))
        return 0
    if args.cmd == "check-tags":
        tags = fetch_tags()
        problems = tag_drift(tags, [v.tag for v in MATRIX.values()])
        print(
            json.dumps(
                {
                    "matrix": [v.tag for v in MATRIX.values()],
                    "newest": newest_minors(tags),
                    "problems": problems,
                }
            )
        )
        for p in problems:
            print(
                f"::error::{p} (urcap/ps5_matrix.py MATRIX, docker-compose.ps5-matrix.yml)", file=sys.stderr
            )
        return 1 if problems else 0
    if args.cmd == "down":
        for v in _selected(args.version):
            down(v)
        return 0

    reports = []
    for v in _selected(args.version):
        rep = run_version(
            v, Path(args.artifacts), boot_timeout=args.boot_timeout, urcap_timeout=args.urcap_timeout
        )
        print(json.dumps(rep, indent=2), flush=True)
        reports.append(rep)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(reports, indent=2), encoding="utf-8")
    return 0 if all(r["ok"] for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
