"""The PolyScope 5 (e-Series) RealSense Pilot URCap: the packager (``urcap/urcap5.py``),
the committed ``dist/`` jar, and the Java client's contract — URL rules, JSON, the
located-target text, and real HTTP against the cockpit (``perception.webapp``).

The Java checks need only a JDK (the client classes import nothing from UR); the build
checks need the URCap API jars too (``python3 urcap/urcap5.py sdk``) and skip without.
"""

from __future__ import annotations

import hashlib
import json
import re
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
DIST = ROOT / "urcap" / "dist" / "realsense-pilot-ps5-0.3.0.urcap"
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


def test_the_downloadable_urcap_passes_polyscope_5s_install_checks():
    # PolyScope 5.26's installer (Settings → System → URCaps → +) runs
    # URCapsServiceImpl.isValidFile before it will touch OSGi, in this order: a manifest,
    # Bundle-Category equal to "urcap" ignoring case (else "the BundleCategory attribute in
    # the manifest file must be present and have the value URCap"), the manifest among the
    # first two zip entries, a non-empty Bundle-SymbolicName and Bundle-Version. Read off
    # polyscope-internal-urcap-10.30.21.jar in the ursim_e-series:5.26.0 image. Dropping the
    # jar into /urcaps (the VM's install-urcap) skips all of it — that is how a bundle with
    # no category loaded in the VM and was refused on the UR3e (2026-09-27).
    bundle = urcap5.read_bundle(DIST)
    h = bundle["headers"]
    assert h.get("Bundle-Category", "").lower() == "urcap"
    assert "META-INF/MANIFEST.MF" in bundle["names"][:2]
    assert h.get("Bundle-SymbolicName") and h.get("Bundle-Version")


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
            case "pick": {
                Map<String, Object> o = Json.parseObject(a[1]);
                PickScript s = new PickScript();
                if (o.containsKey("host")) s.host = (String) o.get("host");
                if (o.containsKey("port")) s.port = ((Number) o.get("port")).intValue();
                if (o.containsKey("q")) s.surveyJoints = Cockpit.six(o.get("q"));
                if (o.containsKey("u")) s.tapU = ((Number) o.get("u")).intValue();
                if (o.containsKey("v")) s.tapV = ((Number) o.get("v")).intValue();
                if (o.containsKey("grip")) s.gripBelowTopMm = ((Number) o.get("grip")).doubleValue();
                if (o.containsKey("lift")) s.liftMm = ((Number) o.get("lift")).doubleValue();
                if (o.containsKey("var")) s.foundVariable = (String) o.get("var");
                Map<String, Object> m = new LinkedHashMap<String, Object>();
                m.put("problem", s.problem());
                m.put("script", s.problem() == null ? s.render((String) o.get("children")) : null);
                out = m;
                break;
            }
            case "hostof": out = PickScript.hostOf(a[1]); break;
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
    for name in ("Json.java", "Cockpit.java", "PickScript.java"):
        shutil.copy(JAVA / name, pkg / name)
    classes = root / "classes"
    subprocess.run(
        [
            JAVAC,
            "--release",
            "8",
            "-Xlint:-options",
            "-encoding",
            "UTF-8",
            "-d",
            str(classes),
            *map(str, pkg.glob("*.java")),
        ],
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


# -- the RealSense Pick node's URScript (PickScript), under a JDK ---------------------------

PICK = {"host": "192.168.3.10", "q": [-1.37, -0.49, 1.61, -2.69, -1.57, 0.61], "u": 412, "v": 233}


def _pick(java_client, **kw):
    return java_client("pick", json.dumps({**PICK, **kw}))


def test_pick_script_is_ascii_balanced_and_calls_the_children_at_the_grip(java_client):
    out = _pick(java_client, children="  CHILDREN_HERE()")
    assert out["problem"] is None
    text = out["script"]
    assert text.isascii()  # the controller's parser, a USB stick's codepage: ASCII only
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    opens = sum(1 for line in lines if line.endswith(":") and line.split()[0] in ("if", "while"))
    assert opens == sum(1 for line in lines if line == "end")
    # the order the program runs in: survey, FIND, look, REFINE ladder, hover, grip, children, lift
    order = [
        "set_tcp(p[0, 0, 0, 0, 0, 0])",
        "movej([-1.370000, -0.490000, 1.610000, -2.690000, -1.570000, 0.610000]",
        'socket_open("192.168.3.10", 7622, "rs_pick")',
        '"FIND "',
        " u=412 v=233",
        '"LOOK "',
        "rs_leans = [0, 12, 24]",
        '"REFINE "',
        "movel(rs_hover",
        "movel(rs_grip, a=0.3, v=0.05)",
        "set_tcp(rs_tcp0)",
        "CHILDREN_HERE()",
        "movel(rs_lift",
        "rs_pick_found = True",
        'socket_close("rs_pick")',
    ]
    at = [text.index(s) for s in order]
    assert at == sorted(at), order
    # nothing moves before the controller's own IK has solved hover, grip and lift
    ik = text.index("get_inverse_kin_has_solution(rs_hover")
    assert ik < text.index("movel(rs_hover")
    # the grip is the configured depth below the top, the lift the configured height above it
    assert "pose_trans(rs_top, p[0, 0, 0.0150, 0, 0, 0])" in text
    assert "pose_trans(rs_top, p[0, 0, -0.0600, 0, 0, 0])" in text
    assert text.rstrip().endswith("set_tcp(rs_tcp0)")  # the operator's TCP back, whatever happened


def test_pick_script_without_a_tap_asks_for_any_block(java_client):
    assert " u=-1 v=-1" in _pick(java_client, u=-1, v=-1)["script"]


@pytest.mark.parametrize(
    ("kw", "problem"),
    [
        ({"host": ""}, "cockpit address"),
        ({"host": 'x"); popup("pwned'}, "not an address"),  # a saved field can't inject URScript
        ({"port": 0}, "port"),
        ({"grip": 61}, "grip depth"),
        ({"lift": 2}, "lift"),
        ({"var": "1bad name"}, "variable"),
    ],
)
def test_pick_script_refuses_what_it_cannot_generate_safely(java_client, kw, problem):
    kw = {k: v for k, v in kw.items()}
    if kw.get("q", 0) is None:
        body = {k: v for k, v in PICK.items() if k != "q"}
        out = java_client("pick", json.dumps(body))
    else:
        out = _pick(java_client, **kw)
    assert out["script"] is None and problem in out["problem"]


def test_without_a_survey_position_the_first_look_is_from_where_the_arm_is(java_client):
    body = {k: v for k, v in PICK.items() if k != "q"}
    out = java_client("pick", json.dumps({**body, "children": "  CHILD()"}))
    assert out["problem"] is None
    text = out["script"]
    assert "first look from where the arm is" in text
    # no motion at all before the first FIND: the arm stays where the operator left it
    before = text[: text.index('"FIND "')]
    assert "movej(" not in before and "movel(" not in before


def test_every_stage_is_logged_to_the_server_and_the_log_tab(java_client):
    text = _pick(java_client, children="  CHILD()")["script"]
    for stage in (
        "start",
        "FIND status",
        "LOOK",
        "at the look pose",
        "REFINE status",
        "hover",
        "down to the grip",
        "gripper nodes",
        "lift",
        "picked",
        "no pick - ",
    ):
        assert f'"LOG {stage}' in text, stage
        assert f'textmsg("RealSense Pick: {stage}' in text, stage
    # a reply's fields are read only after the read came back whole (rs_r[0] == 10): a timed-out
    # read must end as "no answer", not as an index error that stops the operator's program
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "rs_r[1]" in line:
            guard = next(lines[j] for j in range(i - 1, -1, -1) if lines[j].strip().startswith("if "))
            assert guard.strip() == "if rs_r[0] == 10:", (line, guard)
    # LOG is only ever sent on the open socket, never before socket_open
    assert text.index("socket_send_line(") > text.index("socket_open(")


def test_a_failed_pick_says_why_in_a_popup_for_every_status_the_server_sends(java_client):
    from perception.picknode import STATUS

    text = _pick(java_client, children="  CHILD()")["script"]
    handled = {int(m) for m in re.findall(r"rs_st == (-?\d+):", text)}
    # 1 is a pick; -6 (no look pose) never reaches rs_st - the program looks from over the block
    assert handled - {1} == (set(STATUS) - {1, -6}) | {-8}  # `if rs_st == 1:` is the success branch
    assert 'popup(str_cat("RealSense Pick: no pick - ", rs_why)' in text
    assert text.index("if rs_pick_found == False:") < text.index('popup(str_cat("RealSense Pick: no pick')


def test_pick_host_comes_from_the_installation_nodes_url(java_client):
    assert java_client("hostof", "http://192.168.3.10:7621") == "192.168.3.10"
    assert java_client("hostof", "not a url at all") == ""


# -- the urcap5-v<version> release ---------------------------------------------------------


def test_release_check_publishes_exactly_the_committed_jar_for_its_version():
    version = urcap5.read_properties((SRC / "bundle.properties").read_text(encoding="utf-8"))[
        "Bundle-Version"
    ]
    out = urcap5.release_check(f"urcap5-v{version}", SRC, DIST.parent)
    assert out["version"] == version and Path(out["path"]) == DIST
    assert out["sha256"] == hashlib.sha256(DIST.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("tag", "why"),
    [
        ("v0.3.0", "not urcap5-v"),  # the Python package's tags are not this release's
        ("urcap5-v0.3", "not urcap5-v"),
        ("urcap5-v0.3.0-rc1", "not urcap5-v"),
        ("urcap5-v../../etc", "not urcap5-v"),
        ("urcap5-v9.9.9", "Bundle-Version="),
    ],
)
def test_release_check_refuses_a_tag_that_is_not_this_version(tag, why):
    with pytest.raises(urcap5.Urcap5Error, match=why):
        urcap5.release_check(tag, SRC, DIST.parent)


def test_release_check_refuses_a_stale_or_missing_jar(tmp_path):
    version = urcap5.read_properties((SRC / "bundle.properties").read_text(encoding="utf-8"))[
        "Bundle-Version"
    ]
    src = tmp_path / "src"
    shutil.copytree(SRC, src)
    with pytest.raises(urcap5.Urcap5Error, match="not committed"):
        urcap5.release_check(f"urcap5-v{version}", src, tmp_path / "empty")
    dist = tmp_path / "dist"
    dist.mkdir()
    shutil.copy(DIST, dist / DIST.name)
    urcap5.release_check(f"urcap5-v{version}", src, dist)  # a faithful copy passes
    java = next((src / "src").rglob("PickScript.java"))
    java.write_text(java.read_text(encoding="utf-8") + "\n// edited after the build\n", encoding="utf-8")
    with pytest.raises(urcap5.Urcap5Error, match="current sources"):
        urcap5.release_check(f"urcap5-v{version}", src, dist)


def test_release_check_cli_prints_json_or_fails_with_the_reason(capsys):
    version = urcap5.read_properties((SRC / "bundle.properties").read_text(encoding="utf-8"))[
        "Bundle-Version"
    ]
    assert (
        urcap5.main(["release-check", f"urcap5-v{version}", "--src", str(SRC), "--dist", str(DIST.parent)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["version"] == version
    assert urcap5.main(["release-check", "urcap5-v9.9.9", "--src", str(SRC), "--dist", str(DIST.parent)]) == 1
    assert "Bundle-Version=" in capsys.readouterr().err
