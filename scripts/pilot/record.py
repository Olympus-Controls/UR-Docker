"""Record the cockpit's three feeds (two webcams as JPEG, wrist colour as PNG) with host timestamps until a stop file appears."""
# ruff: noqa  — scratch script from the 2026-09-25 hardware session (scripts/pilot/README.md)

import os as _os

HERE = _os.path.dirname(_os.path.abspath(__file__))

import json
import os
import struct
import sys
import threading
import time
import urllib.request

BASE = "http://127.0.0.1:7621"
OUT = sys.argv[1]
STOP = OUT + "/STOP"
os.makedirs(OUT, exist_ok=True)
[os.makedirs(f"{OUT}/{d}", exist_ok=True) for d in ("v0", "v1", "c")]
T0 = time.time()
idx = {"v0": [], "v1": [], "c": []}
lock = threading.Lock()


def view(i):
    seq = 0
    while not os.path.exists(STOP):
        try:
            r = urllib.request.urlopen(f"{BASE}/api/view/{i}?after={seq}&timeout_ms=1000", timeout=5)
            s = int(r.headers.get("X-Seq", "0"))
            data = r.read()
            if s == seq:
                continue
            seq = s
            t = time.time() - T0
            name = f"{OUT}/v{i}/{int(t * 1000):08d}.jpg"
            open(name, "wb").write(data)
            with lock:
                idx[f"v{i}"].append((t, name))
            time.sleep(0.15)  # ~5 fps is plenty for a timelapse
        except Exception:
            time.sleep(0.3)


def wrist():
    while not os.path.exists(STOP):
        try:
            b = urllib.request.urlopen(f"{BASE}/api/rgbd", timeout=5).read()
            hl = struct.unpack(">I", b[4:8])[0]
            i = 8 + hl
            pl = struct.unpack(">I", b[i : i + 4])[0]
            png = b[i + 4 : i + 4 + pl]
            t = time.time() - T0
            name = f"{OUT}/c/{int(t * 1000):08d}.png"
            open(name, "wb").write(png)
            with lock:
                idx["c"].append((t, name))
            time.sleep(0.25)
        except Exception:
            time.sleep(0.3)


ths = [
    threading.Thread(target=view, args=(0,), daemon=True),
    threading.Thread(target=view, args=(1,), daemon=True),
    threading.Thread(target=wrist, daemon=True),
]
[t.start() for t in ths]
json.dump({"t0": T0}, open(OUT + "/t0.json", "w"))
while not os.path.exists(STOP):
    time.sleep(0.5)
[t.join(timeout=5) for t in ths]
json.dump(idx, open(OUT + "/index.json", "w"))
print("recorded", {k: len(v) for k, v in idx.items()})
