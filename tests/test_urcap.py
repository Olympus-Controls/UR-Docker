"""The PolyScope X URCap (urcap/realsense-pilot): its source tree agrees with itself,
scripts/urcapx.py packages it the way UR's urcap-utils does and installs it the way
the Robot-API expects (against a real HTTP server), the behavior worker speaks the
threads.js protocol (run under node when present), and the cockpit's CORS + colour
endpoint that the URCap page depends on."""

from __future__ import annotations

import email.parser
import json
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from perception.config import PerceptionConfig
from perception.realsense import SyntheticRgbdCamera
from perception.webapp import ViewerApp, ViewerHandler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import urcapx  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URCAP = ROOT / "urcap" / "realsense-pilot"
FRONTEND = URCAP / "realsense-pilot-frontend"
NODE = shutil.which("node")


# -- the source tree ----------------------------------------------------------------------------


def test_manifest_contribution_and_sources_agree():
    meta = urcapx.read_manifest((URCAP / "manifest.yaml").read_text())
    assert meta["vendorID"] == "olympus-controls" and meta["urcapID"] == "realsense-pilot"
    assert meta["folders"] == ["realsense-pilot-frontend"]
    contribution = json.loads((FRONTEND / "contribution.json").read_text())
    (node,) = contribution["applicationNodes"]
    tag = node["componentTagName"]
    assert (FRONTEND / node["presenterURI"]).is_file() and (FRONTEND / node["behaviorURI"]).is_file()
    main_js = (FRONTEND / node["presenterURI"]).read_text()
    worker_js = (FRONTEND / node["behaviorURI"]).read_text()
    assert f'const TAG = "{tag}"' in main_js and f'const NODE_TYPE = "{tag}"' in worker_js
    i18n = json.loads((FRONTEND / node["translationPath"] / "en.json").read_text())
    assert i18n["application"]["nodes"][tag]["title"] == "RealSense Pilot"
    for key in ("programNodes", "smartSkills", "sidebarItems", "operatorScreens"):
        assert contribution[key] == []


def test_manifest_reader_rejects_bad_ids_and_missing_fields():
    with pytest.raises(urcapx.UrcapError, match="missing"):
        urcapx.read_manifest("metadata:\n  urcapID: x\n")
    with pytest.raises(urcapx.UrcapError, match="must match"):
        urcapx.read_manifest('metadata:\n  vendorID: "Bad ID"\n  urcapID: "ok"\n  version: 1.0.0\n')
    meta = urcapx.read_manifest('metadata:\n  vendorID: "vend"\n  urcapID: "urc"\n  version: v2.3.4\n')
    assert meta["version"] == "2.3.4" and meta["folders"] == []


# -- packaging ------------------------------------------------------------------------------------


def test_package_is_a_gzipped_tar_with_the_manifest_first(tmp_path):
    out = urcapx.package(URCAP, tmp_path)
    assert out.name == "realsense-pilot-0.1.0.urcapx"
    with tarfile.open(out, "r:gz") as tar:
        names = tar.getnames()
    assert names[0] == "manifest.yaml"
    assert "LICENSE" in names
    for rel in ("main.js", "realsense-pilot-node.worker.js", "contribution.json", "assets/i18n/en.json"):
        assert f"realsense-pilot-frontend/{rel}" in names
    assert not any(n.startswith("./") or n.startswith("/") for n in names)
    assert urcapx.manifest_from_urcapx(out)["urcapID"] == "realsense-pilot"


def test_package_refuses_a_missing_folder(tmp_path):
    src = tmp_path / "bad"
    src.mkdir()
    (src / "manifest.yaml").write_text(
        'metadata:\n  vendorID: "vend"\n  urcapID: "urc"\n  version: 1.0.0\nartifacts:\n  webArchives:\n'
        '  - id: "f"\n    folder: "f"\n'
    )
    with pytest.raises(urcapx.UrcapError, match="not in"):
        urcapx.package(src, tmp_path / "out")


# -- installing against a urservice look-alike ----------------------------------------------------


class UrserviceStub(BaseHTTPRequestHandler):
    """What the 10.13.0 sim's /universal-robots/urservice/api/v1/urcaps did on
    2026-09-26: a plain JSON array on GET, 201 on a multipart POST of urcapxFile,
    409 already_installed on a duplicate, 200 on DELETE /<vendor>/<urcap>."""

    installed: list[dict] = []
    requests: list[dict] = []
    status_override: int | None = None

    def log_message(self, *a):
        pass

    def _reply(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        UrserviceStub.requests.append({"method": "GET", "path": self.path})
        self._reply(UrserviceStub.installed)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        msg = email.parser.BytesParser().parsebytes(
            f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + raw
        )
        parts = [p for p in msg.walk() if p.get_filename()]
        fields = [(p.get_param("name", header="content-disposition"), p.get_filename()) for p in parts]
        UrserviceStub.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "fields": fields,
                "size": sum(len(p.get_payload(decode=True)) for p in parts),
            }
        )
        if UrserviceStub.status_override:
            self._reply(
                {"errors": [{"code": "forced", "message": "forced"}]}, status=UrserviceStub.status_override
            )
            return
        if not fields or fields[0][0] != "urcapxFile":
            self._reply(
                {
                    "errors": [
                        {
                            "code": "internal_error",
                            "message": "did not find 'urcapxFile' in multipart form request",
                        }
                    ]
                },
                status=500,
            )
            return
        ident = {"vendorID": "olympus-controls", "urcapID": "realsense-pilot"}
        if any(it["id"] == ident for it in UrserviceStub.installed):
            self._reply(
                {"errors": [{"code": "already_installed", "message": "urcap already installed"}]}, status=409
            )
            return
        UrserviceStub.installed.append({"id": ident, "version": "0.1.0", "urcapName": "RealSense Pilot"})
        self._reply({"metadata": {"id": ident}}, status=201)

    def do_DELETE(self):  # noqa: N802
        UrserviceStub.requests.append({"method": "DELETE", "path": self.path})
        vendor, urcap = self.path.rstrip("/").split("/")[-2:]
        before = len(UrserviceStub.installed)
        UrserviceStub.installed = [
            it
            for it in UrserviceStub.installed
            if (it["id"]["vendorID"], it["id"]["urcapID"]) != (vendor, urcap)
        ]
        self._reply(
            {"statusCode": 200} if len(UrserviceStub.installed) < before else {"errors": []}, status=200
        )


@pytest.fixture
def urservice():
    UrserviceStub.installed = []
    UrserviceStub.requests = []
    UrserviceStub.status_override = None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), UrserviceStub)
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "127.0.0.1", srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def test_install_posts_urcapxfile_then_409_then_replace_deletes_first(tmp_path, urservice):
    host, port = urservice
    out = urcapx.package(URCAP, tmp_path)
    res = urcapx.install(out, host, port)
    assert res["ok"] and res["status"] == 201 and res["replaced"] is False
    upload = [r for r in UrserviceStub.requests if r["method"] == "POST"][-1]
    assert upload["path"] == urcapx.API_PATH
    assert upload["fields"] == [("urcapxFile", out.name)] and upload["size"] == out.stat().st_size
    assert urcapx.is_installed(host, port, "olympus-controls", "realsense-pilot")
    # a second install is a 409 with the --replace hint, and nothing changed
    res = urcapx.install(out, host, port)
    assert not res["ok"] and res["status"] == 409 and "--replace" in res["hint"]
    # --replace: GET, DELETE, POST
    res = urcapx.install(out, host, port, replace=True)
    assert res["ok"] and res["replaced"] is True
    assert [r["method"] for r in UrserviceStub.requests[-3:]] == ["GET", "DELETE", "POST"]
    assert UrserviceStub.requests[-2]["path"].endswith("/olympus-controls/realsense-pilot")
    assert len(UrserviceStub.installed) == 1


def test_install_names_the_remote_mode_gate_on_403(tmp_path, urservice):
    host, port = urservice
    UrserviceStub.status_override = 403
    out = urcapx.package(URCAP, tmp_path)
    res = urcapx.install(out, host, port)
    assert not res["ok"] and res["status"] == 403 and "Remote" in res["hint"]


def test_install_reports_an_unreachable_robot(tmp_path):
    out = urcapx.package(URCAP, tmp_path)
    with pytest.raises(urcapx.UrcapError, match="POST http|GET http"):
        urcapx.install(out, "127.0.0.1", 9)  # discard port: nothing listens


def test_list_accepts_the_robot_api_wrapping_too(monkeypatch):
    monkeypatch.setattr(
        urcapx,
        "_request",
        lambda url, **kw: {
            "status": 200,
            "payload": {"message": json.dumps([{"id": {"vendorID": "v", "urcapID": "u"}}])},
        },
    )
    assert urcapx.list_urcaps("h", 1) == [{"id": {"vendorID": "v", "urcapID": "u"}}]


def test_cli_list_and_package(tmp_path, urservice, capsys):
    host, port = urservice
    UrserviceStub.installed = [
        {
            "id": {"vendorID": "universal-robots", "urcapID": "web-frontend-app"},
            "version": "1.2.0",
            "urcapName": "Web",
        }
    ]
    assert urcapx.main(["list", "--host", host, "--port", str(port)]) == 0
    assert "universal-robots/web-frontend-app  1.2.0" in capsys.readouterr().out
    assert urcapx.main(["package", str(URCAP), "--out", str(tmp_path)]) == 0
    assert (tmp_path / "realsense-pilot-0.1.0.urcapx").is_file()
    assert (
        urcapx.main(
            ["install", str(tmp_path / "realsense-pilot-0.1.0.urcapx"), "--host", host, "--port", str(port)]
        )
        == 0
    )
    assert (
        urcapx.main(["delete", "olympus-controls", "realsense-pilot", "--host", host, "--port", str(port)])
        == 0
    )
    assert UrserviceStub.installed == [UrserviceStub.installed[0]] and len(UrserviceStub.installed) == 1


# -- the worker under node ----------------------------------------------------------------------

WORKER_HARNESS = r"""
const out = [];
const listeners = [];
globalThis.self = {
  postMessage: (m) => out.push(m),
  addEventListener: (type, fn) => { if (type === "message") listeners.push(fn); },
};
require(process.argv[2]);
const send = (m) => listeners.forEach((fn) => fn({ data: m }));
(async () => {
  send({ type: "run", uid: "u1", method: "factory", args: [] });
  const loaded = { type: "x", version: "0.0.1", cockpitUrl: "http://j:7621" };
  const fresh = { type: "x", version: "1.0.0", cockpitUrl: "" };
  send({ type: "run", uid: "u2", method: "upgradeNode", args: [loaded, fresh, {}] });
  send({ type: "run", uid: "u3", method: "nope", args: [] });
  send({ type: "not-a-run" });
  await new Promise((r) => setTimeout(r, 20));
  process.stdout.write(JSON.stringify(out));
})();
"""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_worker_speaks_the_threads_protocol(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(WORKER_HARNESS)
    proc = subprocess.run(
        [NODE, str(harness), str(FRONTEND / "realsense-pilot-node.worker.js")],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    msgs = json.loads(proc.stdout)
    assert msgs[0] == {
        "type": "init",
        "exposed": {"type": "module", "methods": ["factory", "upgradeNode", "downgradeNode"]},
    }
    by_uid = {}
    for m in msgs[1:]:
        by_uid.setdefault(m["uid"], []).append(m)
    assert [m["type"] for m in by_uid["u1"]] == ["running", "result"]
    assert by_uid["u1"][1]["complete"] is True
    assert by_uid["u1"][1]["payload"] == {
        "type": "olympus-realsense-pilot",
        "version": "1.0.0",
        "cockpitUrl": "",
    }
    assert by_uid["u2"][1]["payload"] == {"type": "x", "version": "1.0.0", "cockpitUrl": "http://j:7621"}
    assert by_uid["u3"][0]["type"] == "error" and by_uid["u3"][0]["error"]["__error_marker"] == "$$error"
    assert "nope" in by_uid["u3"][0]["error"]["message"]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_presenter_parses_and_defines_the_element():
    subprocess.run([NODE, "--check", str(FRONTEND / "main.js")], check=True, timeout=30)
    src = (FRONTEND / "main.js").read_text()
    assert re.search(r"customElements\.define\(TAG, RealSensePilot\)", src)
    for prop in ("applicationNode", "applicationAPI", "robotSettings", "robotContext"):
        assert f"set {prop}(" in src, prop
    for route in (
        "/api/color.png",
        "/api/point",
        "/api/segment",
        "/api/robot/locate",
        "/api/robot/move",
        "/api/robot/stop",
    ):
        assert route in src, route


# -- the cockpit side the page depends on ---------------------------------------------------------


@pytest.fixture
def cockpit():
    app = ViewerApp(
        SyntheticRgbdCamera(width=64, height=48, fps=0),
        config=PerceptionConfig(),
        cors=["http://localhost:8000"],
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    app.start()
    import time

    deadline = time.monotonic() + 5
    while app.latest()[1] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    yield f"http://127.0.0.1:{srv.server_address[1]}", app
    srv.shutdown()
    srv.server_close()
    app.stop()


def _fetch(url, *, method="GET", origin=None, body=None):
    headers = {}
    if origin:
        headers["Origin"] = origin
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_color_png_is_a_png_with_a_sequence_header(cockpit):
    base, app = cockpit
    status, headers, body = _fetch(base + "/api/color.png")
    assert status == 200 and headers["Content-Type"] == "image/png" and body[:8] == b"\x89PNG\r\n\x1a\n"
    assert int(headers["X-Seq"]) >= 1 and "X-Fps" in headers
    status, _, body = _fetch(base + "/api/color.png?after=x")
    assert status == 400


def test_cors_headers_only_for_allowed_origins(cockpit):
    base, app = cockpit
    ok = _fetch(base + "/api/info", origin="http://localhost:8000")[1]
    assert ok["Access-Control-Allow-Origin"] == "http://localhost:8000" and ok["Vary"] == "Origin"
    assert "X-Seq" in ok["Access-Control-Expose-Headers"]
    other = _fetch(base + "/api/info", origin="http://evil.example")[1]
    assert "Access-Control-Allow-Origin" not in other
    none = _fetch(base + "/api/info")[1]
    assert "Access-Control-Allow-Origin" not in none
    # the colour feed and a POST carry them too, and the preflight answers 204
    png = _fetch(base + "/api/color.png", origin="http://localhost:8000")[1]
    assert png["Access-Control-Allow-Origin"] == "http://localhost:8000"
    status, headers, _ = _fetch(base + "/api/clear", method="OPTIONS", origin="http://localhost:8000")
    assert status == 204 and "POST" in headers["Access-Control-Allow-Methods"]
    status, headers, _ = _fetch(
        base + "/api/clear", method="POST", origin="http://localhost:8000", body=b"{}"
    )
    assert status == 200 and headers["Access-Control-Allow-Origin"] == "http://localhost:8000"


def test_cors_star_and_off():
    app = ViewerApp(SyntheticRgbdCamera(width=8, height=8, fps=0), config=PerceptionConfig(), cors=["*"])
    assert app.cors_origins == ["*"]
    app = ViewerApp(SyntheticRgbdCamera(width=8, height=8, fps=0), config=PerceptionConfig())
    assert app.cors_origins == []


def test_cors_from_args_reads_flag_then_env(monkeypatch):
    from perception.webapp import cors_from_args

    class A:
        cors = None

    monkeypatch.delenv("PERCEPTION_CORS", raising=False)
    assert cors_from_args(A()) == []
    monkeypatch.setenv("PERCEPTION_CORS", "http://a:1, http://b:2")
    assert cors_from_args(A()) == ["http://a:1", "http://b:2"]
    A.cors = "*"
    assert cors_from_args(A()) == ["*"]
