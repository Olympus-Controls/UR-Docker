"""The PolyScope 5 (e-Series) RealSense Pilot URCap: the packager (``urcap/urcap5.py``),
the committed ``dist/`` jar, and the Java client's contract — URL rules, JSON, the
located-target text, and real HTTP against the cockpit (``perception.webapp``).

The Java checks need only a JDK (the client classes import nothing from UR); the build
checks need the URCap API jars too (``python3 urcap/urcap5.py sdk``) and skip without.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from perception.config import PerceptionConfig
from perception.realsense import SyntheticRgbdCamera
from perception.webapp import ViewerApp, ViewerHandler

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "urcap"))
import urcap5  # noqa: E402

SRC = ROOT / "urcap" / "realsense-pilot-ps5"
JAVA = SRC / "src" / "com" / "olympuscontrols" / "realsensepilot"
DIST = ROOT / "urcap" / "dist" / "realsense-pilot-ps5-0.1.0.urcap"
JAVAC = shutil.which("javac")
HAS_SDK = all(any(urcap5.SDK_DIR.glob(p + "*.jar")) for p in urcap5.SDK_JARS)


# -- bundle.properties + manifest ----------------------------------------------------------


def test_properties_require_identity_and_a_valid_compatibility():
    good = (SRC / "bundle.properties").read_text(encoding="utf-8")
    props = urcap5.read_properties(good)
    assert props["URCapCompatibility-eSeries"] == "true"
    with pytest.raises(urcap5.Urcap5Error, match="Bundle-Activator"):
        urcap5.read_properties(good.replace("Bundle-Activator=", "#"))
    with pytest.raises(urcap5.Urcap5Error, match="true or false"):
        urcap5.read_properties(
            good.replace("URCapCompatibility-eSeries=true", "URCapCompatibility-eSeries=yes")
        )
    with pytest.raises(urcap5.Urcap5Error, match="neither"):
        urcap5.read_properties(
            good.replace("URCapCompatibility-eSeries=true", "URCapCompatibility-eSeries=false")
        )
    with pytest.raises(urcap5.Urcap5Error, match="key=value"):
        urcap5.read_properties(good + "\nnot a pair\n")


@pytest.mark.parametrize("length", [1, 60, 70, 71, 72, 73, 140, 143, 144, 500])
def test_manifest_lines_wrap_at_72_bytes_and_unwrap_exactly(length):
    value = ("x" * (length - 1)) + "é"  # a multi-byte tail must not be split mid-character badly
    raw = urcap5.manifest_line("Import-Package", value)
    assert raw.endswith(b"\r\n")
    for line in raw.split(b"\r\n")[:-1]:
        assert len(line) <= 72
    # continuation lines start with one space; joining them restores the header
    lines = raw.split(b"\r\n")[:-1]
    joined = lines[0] + b"".join(line[1:] for line in lines[1:])
    assert joined.decode("utf-8", errors="strict") == f"Import-Package: {value}"


def test_import_package_never_imports_java_and_ranges_the_urcap_api():
    got = urcap5.import_package(
        [
            "java.lang",
            "java.util",
            "javax.swing",
            "com.ur.urcap.api.domain",
            "org.osgi.framework",
            "javax.swing",
        ]
    )
    assert got == 'com.ur.urcap.api.domain;version="[1.0.0,2.0.0)",javax.swing,org.osgi.framework'


def test_embedded_pom_names_the_api_version_the_way_polyscope_reads_it():
    # PolyScope 5's PomXMLAPIVersionParser: <dependencies><dependency> groupId com.ur.urcap,
    # artifactId api -> <version>. Parse it the same way.
    props = urcap5.read_properties((SRC / "bundle.properties").read_text(encoding="utf-8"))
    path, body = urcap5.pom_xml(props)
    assert path == "META-INF/maven/com.olympuscontrols/realsensepilot/pom.xml"
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    deps = ET.fromstring(body).findall("m:dependencies/m:dependency", ns)
    versions = [
        d.findtext("m:version", namespaces=ns)
        for d in deps
        if d.findtext("m:groupId", namespaces=ns) == "com.ur.urcap"
        and d.findtext("m:artifactId", namespaces=ns) == "api"
    ]
    assert versions == [props["urcap.api.version"]]


# -- the committed download ----------------------------------------------------------------


def test_the_downloadable_urcap_is_built_from_the_current_sources():
    assert DIST.is_file(), f"{DIST.relative_to(ROOT)} missing — run `make urcap5-package` and commit it"
    bundle = urcap5.read_bundle(DIST)
    assert bundle["sources_sha256"] == urcap5.sources_digest(SRC), (
        f"{DIST.relative_to(ROOT)} is stale — run `make urcap5-package` and commit it"
    )
    props = urcap5.read_properties((SRC / "bundle.properties").read_text(encoding="utf-8"))
    h = bundle["headers"]
    for key in ("Bundle-SymbolicName", "Bundle-Version", "Bundle-Activator"):
        assert h[key] == props[key], key
    assert h["URCapCompatibility-eSeries"] == "true" and h["URCapCompatibility-CB3"] == "false"
    imports = h["Import-Package"].split(",")
    assert not [p for p in imports if p.startswith("java.")]
    assert any(p.startswith("com.ur.urcap.api.domain.userinteraction.robot.movement;") for p in imports)
    assert bundle["names"][:2] == ["META-INF/", "META-INF/MANIFEST.MF"]
    activator = props["Bundle-Activator"].replace(".", "/") + ".class"
    assert activator in bundle["names"]
    assert "<artifactId>api</artifactId>" in bundle["pom"]


@pytest.mark.skipif(not (JAVAC and HAS_SDK), reason="needs a JDK and `urcap5.py sdk`")
def test_package_is_reproducible(tmp_path):
    a = urcap5.package(SRC, tmp_path / "a")
    b = urcap5.package(SRC, tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_package_reports_a_missing_sdk_clearly(tmp_path):
    with pytest.raises(urcap5.Urcap5Error, match="urcap5.py sdk"):
        urcap5.package(SRC, tmp_path, sdk_dir=tmp_path / "no-sdk")


# -- the Java client, under a JDK -----------------------------------------------------------

HARNESS = r"""
package com.olympuscontrols.realsensepilot;

import java.util.*;

public class Harness {
    public static void main(String[] a) throws Exception {
        Object out;
        switch (a[0]) {
            case "base": {
                List<Object> r = new ArrayList<Object>();
                for (int i = 1; i < a.length; i++) r.add(Cockpit.base(a[i]));
                out = r;
                break;
            }
            case "json": {
                try { out = Json.parse(a[1]); } catch (IllegalArgumentException e) { out = "ERR"; }
                break;
            }
            case "write": out = Json.write(Json.parse(a[1])); break;
            case "target": out = Cockpit.targetText(Json.parseObject(a[1])); break;
            case "six": out = Cockpit.six(Json.parse(a[1])) == null ? null : "ok"; break;
            case "explain": {
                Exception e = a[1].equals("refused") ? new java.net.ConnectException("Connection refused")
                        : a[1].equals("dns") ? new java.net.UnknownHostException("jetson")
                        : a[1].equals("timeout") ? new java.net.SocketTimeoutException("connect timed out")
                        : new java.net.MalformedURLException("no protocol");
                out = Cockpit.explain(e, a[2]);
                break;
            }
            case "color": {
                Cockpit.Frame f = new Cockpit(a[1]).colorPng(Long.parseLong(a[2]), 500);
                Map<String, Object> m = new LinkedHashMap<String, Object>();
                m.put("status", f.status);
                m.put("seq", f.seq);
                m.put("w", f.image == null ? null : f.image.getWidth());
                m.put("h", f.image == null ? null : f.image.getHeight());
                out = m;
                break;
            }
            case "get": out = new Cockpit(a[1]).get(a[2], 3000); break;
            case "post": out = new Cockpit(a[1]).post(a[2], Json.parseObject(a[3]), 5000); break;
            case "error": {
                try { new Cockpit(a[1]).get("/api/info", 1000); out = "no error"; }
                catch (Exception e) { out = Cockpit.explain(e, new Cockpit(a[1]).base); }
                break;
            }
            default: throw new IllegalArgumentException(a[0]);
        }
        // bytes, not print(): the JVM's default charset is cp1252 on Windows
        System.out.write(Json.write(out).getBytes("UTF-8"));
        System.out.flush();
    }
}
"""


@pytest.fixture(scope="module")
def java_client(tmp_path_factory):
    if not JAVAC:
        pytest.skip("javac is not installed")
    root = tmp_path_factory.mktemp("java")
    pkg = root / "src" / "com" / "olympuscontrols" / "realsensepilot"
    pkg.mkdir(parents=True)
    (pkg / "Harness.java").write_text(HARNESS, encoding="utf-8")
    for name in ("Json.java", "Cockpit.java"):
        shutil.copy(JAVA / name, pkg / name)
    classes = root / "classes"
    subprocess.run(
        [JAVAC, "--release", "8", "-Xlint:-options", "-d", str(classes), *map(str, pkg.glob("*.java"))],
        check=True,
        capture_output=True,
        timeout=120,
    )

    def run(*args: str):
        proc = subprocess.run(
            ["java", "-cp", str(classes), "com.olympuscontrols.realsensepilot.Harness", *args],
            capture_output=True,
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
        return json.loads(proc.stdout.decode("utf-8"))

    return run


def test_cockpit_url_rules_match_the_polyscope_x_node(java_client):
    # The PolyScope X node's cockpitBase table, with "this host" = the controller.
    cases = {
        "": "http://127.0.0.1:7621",
        ":7621": "http://127.0.0.1:7621",
        "7621": "http://127.0.0.1:7621",
        " :7622/ ": "http://127.0.0.1:7622",
        "127.0.0.1:7621": "http://127.0.0.1:7621",
        "192.168.3.10": "http://192.168.3.10:7621",
        "jetson.local/": "http://jetson.local:7621",
        "http://127.0.0.1:7621//": "http://127.0.0.1:7621",
        "https://cam.example:8443": "https://cam.example:8443",
    }
    got = java_client("base", *cases)
    assert dict(zip(cases, got, strict=True)) == cases


@pytest.mark.parametrize(
    "text",
    [
        '{"ok": true, "point_m": [0.1, -2e-3, 3.5E1], "s": "a\\"b\\\\c\\u00e9\\n", '
        '"n": null, "e": {}, "l": []}',
        '[1, [2, [3, {"k": [false]}]]]',
        '  {"deep": {"a": {"b": {"c": [1.5, -0.25]}}}}  ',
    ],
)
def test_json_parses_what_python_parses(java_client, text):
    assert java_client("json", text) == json.loads(text)
    # and what it writes, Python reads back to the same value
    assert json.loads(java_client("write", text)) == json.loads(text)


@pytest.mark.parametrize(
    "bad", ['{"a": 1', '{"a" 1}', "[1,,2]", '{"a": tru}', '"\\x"', "{} {}", "", '{"a": "\\u12"}', "-"]
)
def test_json_rejects_malformed_input(java_client, bad):
    assert java_client("json", bad) == "ERR"


def test_six_accepts_only_six_finite_numbers(java_client):
    assert java_client("six", "[1,2,3,4,5,6]") == "ok"
    for bad in ("[1,2,3,4,5]", "[1,2,3,4,5,6,7]", '[1,2,3,4,5,"6"]', "null", "{}"):
        assert java_client("six", bad) is None, bad


def test_target_text_says_which_check_judged_reach(java_client):
    loc = {
        "point_base_m": [-0.224, 0.353, -0.277],
        "point_distance_m": 0.5,
        "reference": "flange",
        "flange_target_pose": [-0.232, 0.31, -0.216, -2.583, 0.566, 0.001],
        "standoff_m": 0.075,
        "reachable": True,
        "reach_check": "controller_ik",
    }
    tips = dict(loc, reference="fingertip", tip_m=0.163, approach_pose=[-0.2, 0.3, -0.2, 0, 3.14, 0])
    assert java_client("target", json.dumps(tips)).splitlines()[1] == (
        "fingertips -0.200, 0.300, -0.200, 0.000, 3.140, 0.000  0.075 m above the object (tool 0.163 m)"
    )
    text = java_client("target", json.dumps(loc))
    assert text.splitlines()[1].startswith("flange   -0.232, 0.310, -0.216")
    assert text.endswith("reachable  (the controller's inverse kinematics)")
    sphere = dict(
        loc,
        reachable=False,
        reach_check="sphere",
        max_reach_m=0.5,
        model="UR3E",
        reference="tcp",
        approach_pose=[0.1, 0.2, 0.3, 0, 3.14, 0],
    )
    text = java_client("target", json.dumps(sphere))
    assert "approach 0.100, 0.200, 0.300" in text
    assert text.endswith("OUT OF REACH  (0.50 m datasheet radius, UR3E — no IK answer)")


def test_failures_explain_themselves(java_client):
    refused = java_client("explain", "refused", "http://127.0.0.1:7621")
    assert "connection refused" in refused and "the robot controller itself" in refused
    remote = java_client("explain", "refused", "http://192.168.3.10:7621")
    assert "--bind 0.0.0.0" in remote
    assert "resolve" in java_client("explain", "dns", "http://jetson:7621")
    assert "firewall" in java_client("explain", "timeout", "http://10.0.0.9:7621")
    assert "is not a URL" in java_client("explain", "url", "ht!tp://x")
    # a real refused connection (nothing listens on port 1) takes the same path
    assert "connection refused" in java_client("error", "127.0.0.1:1")


# -- the Java client against the real cockpit HTTP server ----------------------------------


@pytest.fixture
def cockpit():
    app = ViewerApp(SyntheticRgbdCamera(width=64, height=48, fps=0), config=PerceptionConfig())
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.start()
    deadline = time.monotonic() + 5
    while app.latest()[1] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()
    app.stop()


def test_colour_feed_decodes_and_advances_the_sequence(java_client, cockpit):
    first = java_client("color", cockpit, "0")
    assert first["status"] == 200 and (first["w"], first["h"]) == (64, 48) and first["seq"] > 0
    nxt = java_client("color", cockpit, str(first["seq"]))
    assert nxt["status"] == 200 and nxt["seq"] >= first["seq"]


def test_segment_and_errors_round_trip_as_json(java_client, cockpit):
    seg = java_client("post", cockpit, "/api/segment", json.dumps({"x": 32, "y": 24}))
    assert seg["ok"] is True and "features" in seg
    bad = java_client("post", cockpit, "/api/segment", json.dumps({"x": 9999, "y": 24}))
    assert bad["ok"] is False and bad.get("error")
    # no robot link on this cockpit: locate answers a JSON error, never a crash
    loc = java_client("post", cockpit, "/api/robot/locate", json.dumps({"point_m": [0, 0, 0.4]}))
    assert loc["ok"] is False
    point = java_client("get", cockpit, "/api/point?x=10&y=10")
    assert "ok" in point
