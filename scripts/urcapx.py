#!/usr/bin/env python3
"""Package, install, list and delete a PolyScope X URCap (``.urcapx``) — stdlib only.

What UR's ``@universal-robots/urcap-utils`` 2.1.2 does with node, done here without
an npm toolchain (the URCap under ``urcap/`` is plain JavaScript, so nothing needs
building):

* ``package SRC [--out DIR]`` — ``SRC`` holds ``manifest.yaml`` and the web-archive
  folder(s) it names. Writes ``DIR/<urcapID>-<version>.urcapx``: a **gzipped tar**
  with ``manifest.yaml`` as the first member (``package-urcap.js`` + ``tar-helper.js``
  in urcap-utils), a ``LICENSE`` (``SRC/LICENSE`` or the repo's) and the folders.
* ``install FILE [--host H] [--port P]`` — the Robot-API's URCap endpoint
  (``urservice-helper.js``): ``GET /universal-robots/robot-api/urcaps/v1/urcaps/``
  to see whether the URCap is already there, then a multipart ``POST`` (new) or
  ``PUT`` (update) of field ``urcapx_file`` to the same path. The PolyScope X
  simulator in this repo listens on ``localhost:8000``. A real robot needs
  External Control / Remote mode for installs from outside; the sim defaults to
  Development Mode only when ``DEVMODE=true`` is in its environment.
* ``list`` / ``delete VENDOR URCAP`` — the same endpoint.

    uv run python scripts/urcapx.py package urcap/realsense-pilot --out target
    uv run python scripts/urcapx.py install target/realsense-pilot-0.1.0.urcapx --port 8000
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path

MANIFEST = "manifest.yaml"
API_PATH = "/universal-robots/robot-api/urcaps/v1/urcaps/"
REPO_ROOT = Path(__file__).resolve().parent.parent


class UrcapError(RuntimeError):
    pass


# -- manifest (the two-level YAML we write; not a YAML parser) -------------------------------


def read_manifest(text: str) -> dict:
    """``{vendorID, urcapID, urcapName, vendorName, version, folders: [...]}`` from
    a ``manifest.yaml`` of the shape the SDK generator writes."""

    def scalar(key: str) -> str | None:
        m = re.search(rf"^\s*{key}:\s*\"?([^\"\n]+?)\"?\s*$", text, re.M)
        return m.group(1).strip() if m else None

    out = {k: scalar(k) for k in ("vendorID", "urcapID", "urcapName", "vendorName", "version")}
    missing = [k for k in ("vendorID", "urcapID", "version") if not out[k]]
    if missing:
        raise UrcapError(f"{MANIFEST} is missing {', '.join(missing)}")
    if not re.fullmatch(r"[a-z][a-z0-9_-]*[a-z0-9]", out["urcapID"]) or not re.fullmatch(
        r"[a-z][a-z0-9_-]*[a-z0-9]", out["vendorID"]
    ):
        raise UrcapError("vendorID/urcapID must match ^[a-z][a-z0-9_-]*[a-z0-9]$ (manifest-spec-19.10.31)")
    out["version"] = re.sub(r"^v", "", out["version"])
    out["folders"] = [m.strip() for m in re.findall(r"^\s*folder:\s*\"?([^\"\n]+?)\"?\s*$", text, re.M)]
    return out


# -- package ------------------------------------------------------------------------------------


def package(src: str | Path, out_dir: str | Path) -> Path:
    src = Path(src)
    manifest_path = src / MANIFEST
    if not manifest_path.is_file():
        raise UrcapError(f"no {MANIFEST} in {src}")
    meta = read_manifest(manifest_path.read_text(encoding="utf-8"))
    for folder in meta["folders"]:
        if not (src / folder).is_dir():
            raise UrcapError(f"web archive folder {folder!r} named in {MANIFEST} is not in {src}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{meta['urcapID']}-{meta['version']}.urcapx"
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "dist"
        stage.mkdir()
        shutil.copyfile(manifest_path, stage / MANIFEST)
        licence = src / "LICENSE" if (src / "LICENSE").is_file() else REPO_ROOT / "LICENSE"
        if licence.is_file():
            shutil.copyfile(licence, stage / "LICENSE")
        for folder in meta["folders"]:
            shutil.copytree(src / folder, stage / folder)
        members = sorted(p.name for p in stage.iterdir() if p.name != MANIFEST)
        with tarfile.open(out, "w:gz") as tar:
            for name in [MANIFEST, *members]:
                tar.add(stage / name, arcname=name)
    return out


# -- the Robot-API URCap endpoint ---------------------------------------------------------------


def _base(host: str, port: int) -> str:
    return f"http://{host}:{port}{API_PATH}"


def _request(
    url: str, *, method: str = "GET", data: bytes | None = None, headers: dict | None = None
) -> dict:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = r.read()
            status = r.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise UrcapError(f"{method} {url}: {exc}") from None
    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        payload = {"raw": body.decode("utf-8", "replace")[:500]}
    return {"status": status, "payload": payload}


def list_urcaps(host: str, port: int) -> list[dict]:
    """The installed URCaps: ``[{id: {vendorID, urcapID}, version, urcapName, …}]``
    (the endpoint wraps the list as a JSON string in ``message``)."""
    res = _request(_base(host, port))
    if res["status"] != 200:
        raise UrcapError(f"list failed: HTTP {res['status']} {res['payload']}")
    message = res["payload"].get("message", "[]") if isinstance(res["payload"], dict) else "[]"
    try:
        items = json.loads(message) if isinstance(message, str) else message
    except json.JSONDecodeError:
        raise UrcapError(f"unexpected list payload: {message[:200]}") from None
    return items if isinstance(items, list) else []


def is_installed(host: str, port: int, vendor: str, urcap: str) -> bool:
    return any(
        (it.get("id") or {}).get("vendorID") == vendor and (it.get("id") or {}).get("urcapID") == urcap
        for it in list_urcaps(host, port)
    )


def _multipart(field: str, filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = f"----urcapx-{uuid.uuid4().hex}"
    buf = io.BytesIO()
    buf.write(f"--{boundary}\r\n".encode())
    buf.write(f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'.encode())
    buf.write(b"Content-Type: application/octet-stream\r\n\r\n")
    buf.write(content)
    buf.write(f"\r\n--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def manifest_from_urcapx(path: str | Path) -> dict:
    with tarfile.open(path, "r:gz") as tar:
        member = tar.extractfile(MANIFEST)
        if member is None:
            raise UrcapError(f"{path} has no {MANIFEST}")
        return read_manifest(member.read().decode("utf-8"))


def install(path: str | Path, host: str, port: int, *, replace: bool = False) -> dict:
    """Install (POST) or update (PUT) ``path``; ``replace`` deletes first."""
    path = Path(path)
    meta = manifest_from_urcapx(path)
    vendor, urcap = meta["vendorID"], meta["urcapID"]
    present = is_installed(host, port, vendor, urcap)
    if present and replace:
        delete(host, port, vendor, urcap)
        present = False
    body, ctype = _multipart("urcapx_file", path.name, path.read_bytes())
    res = _request(
        _base(host, port),
        method="PUT" if present else "POST",
        data=body,
        headers={"Content-Type": ctype, "Content-Length": str(len(body))},
    )
    res.update({"vendorID": vendor, "urcapID": urcap, "version": meta["version"], "updated": present})
    if res["status"] == 403:
        res["hint"] = (
            "403: the robot must be in Remote / External Control mode to accept a URCap from outside "
            "(the sim allows it in Development Mode: DEVMODE=true in its environment)"
        )
    return res


def delete(host: str, port: int, vendor: str, urcap: str) -> dict:
    return _request(f"{_base(host, port)}{vendor}/{urcap}", method="DELETE")


# -- CLI ------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="urcapx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    pk = sub.add_parser("package", help="build SRC into a .urcapx")
    pk.add_argument("src")
    pk.add_argument("--out", default="target", help="output directory (default target/)")
    for name in ("install", "list", "delete"):
        p = sub.add_parser(name)
        p.add_argument("--host", default="localhost")
        p.add_argument("--port", type=int, default=8000, help="Robot-API port (the sim publishes 8000)")
        if name == "install":
            p.add_argument("file")
            p.add_argument("--replace", action="store_true", help="delete an installed copy first")
        if name == "delete":
            p.add_argument("vendor")
            p.add_argument("urcap")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "package":
            out = package(args.src, args.out)
            print(out)
            return 0
        if args.cmd == "list":
            for it in list_urcaps(args.host, args.port):
                ident = it.get("id") or {}
                ident_s = f"{ident.get('vendorID')}/{ident.get('urcapID')}"
                print(f"{ident_s}  {it.get('version')}  {it.get('urcapName')}")
            return 0
        if args.cmd == "install":
            res = install(args.file, args.host, args.port, replace=args.replace)
            print(json.dumps(res, indent=1))
            return 0 if res["status"] in (200, 201) else 1
        if args.cmd == "delete":
            res = delete(args.host, args.port, args.vendor, args.urcap)
            print(json.dumps(res, indent=1))
            return 0 if res["status"] in (200, 204) else 1
    except UrcapError as exc:
        print(f"urcapx: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
