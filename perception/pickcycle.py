"""The pick cycle — a heuristic routine that lifts every block in view, no model
in the loop.

``perception pick-cycle`` drives a **running cockpit** (``perception gui``) over
its HTTP API — the cockpit owns the camera and the robot's Primary link, so this
is a client, the same way the MCP tools are. The routine:

1. **Survey** from the current (measuring) pose: white blobs in the wrist
   image, each one's top face as the nearest 3-D plane (points within 5 mm of
   it), its centre and in-plane axes in the base frame through the cockpit's
   hand-eye. The camera must be ≥ 0.25 m from the blocks — the D435 has no
   depth closer than ~0.2 m.
2. For each block, in place: yaw the flange so the fingers close across the
   block's short side, hover with the **fingertips** 40 mm over its top,
   descend to the top edge, then ``grasp_below_mm`` further, close, lift
   ``lift_mm``, set it back down, release, clear. A close that holds nothing
   (Robotiq ``OBJ`` 3) opens and moves on; a protective stop unlocks, lifts and
   moves on.
3. Optionally a **drop pass** (``--drop``): the same grasp, then release from
   ``drop_mm`` up so the pile shuffles.

Every phase is an event with a wall-clock stamp (``events.json``), and
``--record DIR`` saves the three cockpit feeds alongside so
``scripts/pilot/assemble.py`` can cut a subtitled timelapse.

The fingertip length is the one number the routine cannot see: ``tip_m``
(Hand-E 157 mm + the 6 mm bracket adapter by default). The tool axis may be
tilted, so every target is placed for the **tip**, ``flange = tip − R·(0,0,L)``.
"""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from urctl.pose import Transform

from .pngio import load_png

DEFAULT_COCKPIT = "http://127.0.0.1:7621"
DEFAULT_TIP_M = 0.163
WHITE_MIN, WHITE_CHROMA = 180, 60  # foam reads bluish-white under a cool white balance (207,227,251)

# -- image heuristics ----------------------------------------------------------------------


def white_blobs(
    width: int, height: int, channels: int, rgb: bytes, *, step: int = 4, min_px: int = 400
) -> list[dict]:
    """Connected components of bright, low-chroma pixels on a ``step`` grid:
    ``[{px, cx, cy, bbox}]`` largest first."""
    cells: set[tuple[int, int]] = set()
    for y in range(0, height, step):
        for x in range(0, width, step):
            i = (y * width + x) * channels
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if min(r, g, b) > WHITE_MIN and max(r, g, b) - min(r, g, b) < WHITE_CHROMA:
                cells.add((x // step, y // step))
    seen: set[tuple[int, int]] = set()
    out = []
    for c in cells:
        if c in seen:
            continue
        stack, comp = [c], []
        seen.add(c)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for n in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    stack.append(n)
        px = len(comp) * step * step
        if px < min_px:
            continue
        xs = [q[0] * step for q in comp]
        ys = [q[1] * step for q in comp]
        out.append(
            {
                "px": px,
                "cx": sum(xs) // len(xs),
                "cy": sum(ys) // len(ys),
                "bbox": (min(xs), min(ys), max(xs) + step, max(ys) + step),
            }
        )
    return sorted(out, key=lambda b: -b["px"])


def top_face(
    width: int, height: int, channels: int, rgb: bytes, depth: bytes, scale: float, K: dict, bbox
) -> dict | None:
    """The block's top face inside ``bbox``: 3-D points of its white pixels, seeded
    by the nearest 12 mm, refined twice to the plane through them (±5 mm).
    Returns ``{centre (camera m), normal, points, n}`` or None."""
    x0, y0, x1, y1 = bbox
    pts = []
    for y in range(y0, min(y1, height)):
        for x in range(x0, min(x1, width)):
            i = (y * width + x) * channels
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if min(r, g, b) > WHITE_MIN and max(r, g, b) - min(r, g, b) < WHITE_CHROMA:
                d = depth[2 * (y * width + x)] | (depth[2 * (y * width + x) + 1] << 8)
                if d:
                    z = d * scale
                    pts.append(((x - K["ppx"]) * z / K["fx"], (y - K["ppy"]) * z / K["fy"], z))
    if len(pts) < 60:
        return None
    zmin = min(p[2] for p in pts)
    sel = [p for p in pts if p[2] <= zmin + 0.015]
    normal = [0.0, 0.0, -1.0]
    for _ in range(3):
        c = [sum(p[k] for p in sel) / len(sel) for k in range(3)]
        cov = [[sum((p[a] - c[a]) * (p[b] - c[b]) for p in sel) for b in range(3)] for a in range(3)]
        ex, ey = cov[0], cov[1]
        n = (
            ex[1] * ey[2] - ex[2] * ey[1],
            ex[2] * ey[0] - ex[0] * ey[2],
            ex[0] * ey[1] - ex[1] * ey[0],
        )
        nn = math.sqrt(sum(v * v for v in n)) or 1.0
        normal = [v / nn for v in n]
        sel = [p for p in pts if abs(sum((p[k] - c[k]) * normal[k] for k in range(3))) < 0.007]
        if len(sel) < 40:
            return None
    c = [sum(p[k] for p in sel) / len(sel) for k in range(3)]
    return {"centre": c, "normal": normal, "points": sel, "n": len(sel)}


def plane_axes_xy(points: Sequence[Sequence[float]]) -> dict:
    """PCA of base-frame points in XY: major-axis heading (rad), and the extents
    a uniform rectangle of that spread would have."""
    n = len(points)
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    sxx = sum((p[0] - cx) ** 2 for p in points)
    syy = sum((p[1] - cy) ** 2 for p in points)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points)
    theta = 0.5 * math.atan2(2 * sxy, sxx - syy)
    root = math.hypot(sxx - syy, 2 * sxy)
    lam1 = 0.5 * (sxx + syy) + 0.5 * root
    lam2 = max(0.0, 0.5 * (sxx + syy) - 0.5 * root)
    return {
        "theta": theta,
        "major_m": 2 * math.sqrt(3 * lam1 / n),
        "minor_m": 2 * math.sqrt(3 * lam2 / n),
        "centre_xy": (cx, cy),
    }


def grasp_yaw_deg(flange_pose: Sequence[float], minor_heading: float) -> float:
    """The smallest rotation about the flange's own Z (degrees) that lines flange X
    up with ``minor_heading`` (base XY, rad) in either sense — the fingers open
    along flange X on the Hand-E as mounted here."""
    fx = Transform.from_pose(flange_pose).rotate((1.0, 0.0, 0.0))
    heading = math.atan2(fx[1], fx[0])
    yaw = math.degrees(minor_heading - heading)
    return (yaw + 90.0) % 180.0 - 90.0


def tip_pose(
    tip: Sequence[float], rotation_pose: Sequence[float], tip_m: float, yaw_deg: float = 0.0
) -> list[float]:
    """Flange pose that puts the fingertips at ``tip`` (base m) with the flange
    orientation of ``rotation_pose`` yawed by ``yaw_deg`` about its own Z."""
    base = Transform.from_pose(rotation_pose)
    yawed = Transform(base.rotation, (0.0, 0.0, 0.0)).compose(
        Transform.from_pose([0, 0, 0, 0, 0, math.radians(yaw_deg)])
    )
    zax = yawed.rotate((0.0, 0.0, 1.0))
    return [tip[i] - zax[i] * tip_m for i in range(3)] + list(yawed.to_pose()[3:])


# -- the cockpit client ---------------------------------------------------------------------


class CockpitError(RuntimeError):
    pass


@dataclass
class Cockpit:
    base: str = DEFAULT_COCKPIT
    timeout_s: float = 120.0

    def post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            self.base + path,
            data=json.dumps(body or {}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            return json.load(urllib.request.urlopen(req, timeout=self.timeout_s))
        except urllib.error.HTTPError as exc:
            try:
                return json.load(exc)
            except Exception:
                return {"ok": False, "error": f"HTTP {exc.code}"}

    def get(self, path: str) -> dict:
        return json.load(urllib.request.urlopen(self.base + path, timeout=self.timeout_s))

    def frame(self) -> tuple[dict, int, int, int, bytes, bytes]:
        b = urllib.request.urlopen(self.base + "/api/rgbd", timeout=self.timeout_s).read()
        if b[:4] != b"RGBD":
            raise CockpitError("no RGB-D frame from the cockpit")
        hl = struct.unpack(">I", b[4:8])[0]
        hdr = json.loads(b[8 : 8 + hl])
        i = 8 + hl
        pl = struct.unpack(">I", b[i : i + 4])[0]
        png = b[i + 4 : i + 4 + pl]
        i += 4 + pl
        dl = struct.unpack(">I", b[i : i + 4])[0]
        depth = zlib.decompress(b[i + 4 : i + 4 + dl])
        w, h, ch, rgb = load_png_bytes(png)
        return hdr, w, h, ch, rgb, depth


def load_png_bytes(png: bytes):
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        f.write(png)
        name = f.name
    try:
        return load_png(name)
    finally:
        os.unlink(name)


# -- the routine --------------------------------------------------------------------------


@dataclass
class Block:
    index: int
    centre_base: list[float]  # top-face centre, base frame (m)
    theta: float  # major-axis heading, base XY (rad)
    major_m: float
    minor_m: float
    pixel: tuple[int, int]
    n: int


@dataclass
class PickCycle:
    cockpit: Cockpit = field(default_factory=Cockpit)
    tip_m: float = DEFAULT_TIP_M
    hover_mm: float = 40.0
    grasp_below_mm: float = 15.0
    lift_mm: float = 25.4
    clear_mm: float = 60.0
    drop_mm: float = 120.0
    velocity: float = 0.06
    force: int = 80
    dry_run: bool = False
    log: list[dict] = field(default_factory=list)
    t0: float = field(default_factory=time.time)
    say_fn: object = None

    # -- bookkeeping ------------------------------------------------------------------
    def say(self, text: str) -> None:
        ev = {"t": round(time.time() - self.t0, 2), "text": text}
        self.log.append(ev)
        if callable(self.say_fn):
            self.say_fn(ev)
        else:
            print(f"[{ev['t']:6.1f}s] {text}", flush=True)

    def _state(self) -> dict:
        return self.cockpit.post("/api/robot/state")

    def _flange(self) -> list[float]:
        r = self.cockpit.post(
            "/api/robot/locate", {"standoff_m": 0.2, "reference": "flange", "point_m": [0, 0, 0.3]}
        )
        if not r.get("flange_pose"):
            raise CockpitError(r.get("error") or "no flange pose")
        return r["flange_pose"]

    def _move(self, pose: Sequence[float], v: float | None = None) -> dict:
        if self.dry_run:
            return {"ok": True, "landed": list(pose), "dry_run": True}
        r = self.cockpit.post(
            "/api/robot/move", {"pose": list(pose), "velocity": v or self.velocity, "tcp": [0] * 6}
        )
        return r

    def _gripper(self, action: str) -> dict:
        if self.dry_run:
            return {"ok": True, "object_detected": action == "close", "status": {}}
        r = self.cockpit.post("/api/robot/gripper", {"action": action})
        if r.get("ok") or "no route" not in (r.get("error") or ""):
            return r
        # older cockpit without the gripper route: the CLI from a second process
        host = (self.cockpit.get("/api/robot").get("robot") or {}).get("host", "")
        out = subprocess.run(
            ["uv", "run", "urctl", "gripper", action, "--force", str(self.force)],
            capture_output=True,
            text=True,
            env={**os.environ, "UR_HOST": host},
        )
        try:
            return json.loads(out.stdout)
        except Exception:
            return {"ok": False, "error": out.stderr[-200:]}

    def _recover(self, what: str) -> None:
        self.say(f"Protective stop during {what}: unlocking and lifting clear")
        self.cockpit.post("/api/robot/bring_up")
        time.sleep(2.0)
        fl = self._flange()
        self._move([fl[0], fl[1], fl[2] + self.clear_mm / 1000.0, *fl[3:]], 0.04)

    def _ok_after(self, r: dict, what: str) -> bool:
        if r.get("ok"):
            return True
        if r.get("protective_stop") or "PROTECTIVE" in (self._state().get("safety_mode") or ""):
            self._recover(what)
        else:
            self.say(f"{what} refused: {r.get('error') or (r.get('safety') or {}).get('violations')}")
        return False

    # -- survey -------------------------------------------------------------------------
    def survey(self) -> list[Block]:
        rob = self.cockpit.get("/api/robot").get("robot") or {}
        he = (rob.get("handeye") or {}).get("flange_to_color_pose")
        if not he:
            raise CockpitError("the cockpit has no hand-eye (calibrate first)")
        T_fc = Transform.from_pose(he)
        T_bf = Transform.from_pose(self._flange())
        hdr, w, h, ch, rgb, depth = self.cockpit.frame()
        K = hdr["intrinsics"]
        blocks: list[Block] = []
        for b in white_blobs(w, h, ch, rgb):
            x0, y0, x1, y1 = b["bbox"]
            if x0 <= 6 or y0 <= 6 or x1 >= w - 6 or y1 >= h - 6:
                continue  # clipped at the frame edge
            tf = top_face(w, h, ch, rgb, depth, hdr["depth_scale_m"], K, b["bbox"])
            if tf is None:
                continue
            pts_base = [T_bf.apply(T_fc.apply(p)) for p in tf["points"]]
            ax = plane_axes_xy(pts_base)
            centre = T_bf.apply(T_fc.apply(tf["centre"]))
            if ax["major_m"] > 0.07 or ax["minor_m"] > 0.06 or ax["minor_m"] < 0.010:
                continue  # not a block: a velcro strap (100 x 15 mm), the rail, a speck
            blocks.append(
                Block(
                    len(blocks),
                    list(centre),
                    ax["theta"],
                    ax["major_m"],
                    ax["minor_m"],
                    (b["cx"], b["cy"]),
                    tf["n"],
                )
            )
        return blocks

    # -- one block --------------------------------------------------------------------
    def cycle_block(self, blk: Block, *, drop: bool = False) -> dict:
        label = f"block {blk.index + 1}"
        fl = self._flange()
        yaw = grasp_yaw_deg(fl, blk.theta + math.pi / 2)
        top = blk.centre_base
        self.say(
            f"{label}: top at {[round(v, 3) for v in top]} m, "
            f"{blk.major_m * 1000:.0f} x {blk.minor_m * 1000:.0f} mm; "
            f"turning the fingers {yaw:+.0f} deg to close across the short side"
        )
        hover = tip_pose([top[0], top[1], top[2] + self.hover_mm / 1000.0], fl, self.tip_m, yaw)
        if not self._ok_after(self._move(hover), f"{label} hover"):
            return {"block": blk.index, "ok": False, "stage": "hover"}
        self.say(f"{label}: fingertips {self.hover_mm:.0f} mm above the top, straight down to the edge")
        edge = tip_pose(top, hover, self.tip_m)
        if not self._ok_after(self._move(edge, 0.03), f"{label} descent"):
            return {"block": blk.index, "ok": False, "stage": "edge"}
        grasp = tip_pose([top[0], top[1], top[2] - self.grasp_below_mm / 1000.0], hover, self.tip_m)
        if not self._ok_after(self._move(grasp, 0.03), f"{label} grasp descent"):
            return {"block": blk.index, "ok": False, "stage": "grasp"}
        self.say(f"{label}: {self.grasp_below_mm:.0f} mm into the grasp, closing")
        g = self._gripper("close")
        if not g.get("object_detected"):
            self.say(
                f"{label}: closed on nothing (POS {(g.get('status') or {}).get('POS')}) — opening, moving on"
            )
            self._gripper("open")
            self._move([grasp[0], grasp[1], grasp[2] + self.clear_mm / 1000.0, *grasp[3:]])
            return {"block": blk.index, "ok": False, "stage": "close", "gripper": g}
        pos = (g.get("status") or {}).get("POS")
        self.say(f"{label}: held (Robotiq POS {pos})")
        if drop:
            up = [grasp[0], grasp[1], grasp[2] + self.drop_mm / 1000.0, *grasp[3:]]
            if not self._ok_after(self._move(up), f"{label} lift"):
                self._gripper("open")
                return {"block": blk.index, "ok": False, "stage": "lift"}
            self.say(f"{label}: {self.drop_mm:.0f} mm up — dropping it to shuffle")
            self._gripper("open")
            time.sleep(0.5)
            return {"block": blk.index, "ok": True, "stage": "dropped", "pos": pos}
        up = [grasp[0], grasp[1], grasp[2] + self.lift_mm / 1000.0, *grasp[3:]]
        if not self._ok_after(self._move(up, 0.03), f"{label} lift"):
            self._gripper("open")
            return {"block": blk.index, "ok": False, "stage": "lift"}
        self.say(f"{label}: lifted {self.lift_mm:.0f} mm, setting it back down")
        time.sleep(0.8)
        self._move(grasp, 0.03)
        self._gripper("open")
        self.say(f"{label}: released, clearing")
        self._move([grasp[0], grasp[1], grasp[2] + self.clear_mm / 1000.0, *grasp[3:]])
        return {"block": blk.index, "ok": True, "stage": "replaced", "pos": pos}

    # -- the whole run ----------------------------------------------------------------
    def survey_from(self, poses: Sequence[Sequence[float]] | None) -> list[Block]:
        """Survey from each pose in turn (None = from here) and merge blocks seen
        twice (top centres within 30 mm), keeping the better-supported view."""
        if not poses:
            return self.survey()
        merged: list[Block] = []
        for pose in poses:
            if not self._ok_after(self._move(list(pose)), "survey move"):
                continue
            time.sleep(0.8)
            for b in self.survey():
                twin = None
                for m in merged:
                    if (
                        math.hypot(m.centre_base[0] - b.centre_base[0], m.centre_base[1] - b.centre_base[1])
                        < 0.03
                    ):
                        twin = m
                        break
                if twin is None:
                    b.index = len(merged)
                    merged.append(b)
                elif b.n > twin.n:
                    b.index = twin.index
                    merged[twin.index] = b
        return merged

    def run(
        self,
        *,
        drop: bool = False,
        max_blocks: int = 8,
        survey_poses: Sequence[Sequence[float]] | None = None,
    ) -> dict:
        start = list(survey_poses[0]) if survey_poses else self._flange()
        st = self._state()
        if "REMOTE" not in (st.get("control_mode") or "REMOTE"):
            raise CockpitError("robot is in Local control — motion needs Remote")
        self.say(
            "Survey: white blocks in the wrist camera, each top face fitted as a plane "
            "and placed in the base frame"
        )
        blocks = self.survey_from(survey_poses)
        self.say(f"Found {len(blocks)} block(s)")
        results = []
        for blk in blocks[:max_blocks]:
            results.append(self.cycle_block(blk))
        self.say(
            f"Pass 1 done: {sum(1 for r in results if r['ok'])} of {len(results)} lifted and replaced. "
            "Back to the survey pose"
        )
        self._move(start)
        if drop:
            time.sleep(0.8)
            blocks = self.survey_from(survey_poses)
            self.say(f"Drop pass: {len(blocks)} block(s) — each one up {self.drop_mm:.0f} mm and let go")
            for blk in blocks[:max_blocks]:
                results.append(self.cycle_block(blk, drop=True))
                self._move(start)
                time.sleep(0.5)
        self.say("Done")
        return {"ok": True, "results": results, "events": self.log}


# -- recording (the three cockpit feeds with host stamps) -------------------------------


class Recorder:
    """Save ``/api/view/0``, ``/api/view/1`` (JPEG) and ``/api/rgbd`` (PNG) to
    ``out`` with timestamps relative to ``t0``; ``index.json`` + ``t0.json`` are
    what ``scripts/pilot/assemble.py`` reads."""

    def __init__(self, cockpit: Cockpit, out: str, t0: float):
        self.cockpit, self.out, self.t0 = cockpit, out, t0
        self.idx: dict[str, list] = {"v0": [], "v1": [], "c": []}
        self.stop = threading.Event()
        self.lock = threading.Lock()
        for d in ("v0", "v1", "c"):
            os.makedirs(f"{out}/{d}", exist_ok=True)
        json.dump({"t0": t0}, open(f"{out}/t0.json", "w"))
        self.threads = [
            threading.Thread(target=self._view, args=(0,), daemon=True),
            threading.Thread(target=self._view, args=(1,), daemon=True),
            threading.Thread(target=self._wrist, daemon=True),
        ]

    def start(self) -> None:
        for t in self.threads:
            t.start()

    def _view(self, i: int) -> None:
        seq = 0
        while not self.stop.is_set():
            try:
                r = urllib.request.urlopen(
                    f"{self.cockpit.base}/api/view/{i}?after={seq}&timeout_ms=1000", timeout=5
                )
                s = int(r.headers.get("X-Seq", "0"))
                data = r.read()
                if s == seq:
                    continue
                seq = s
                t = time.time() - self.t0
                name = f"{self.out}/v{i}/{int(t * 1000):08d}.jpg"
                open(name, "wb").write(data)
                with self.lock:
                    self.idx[f"v{i}"].append((t, name))
                time.sleep(0.15)
            except Exception:
                time.sleep(0.3)

    def _wrist(self) -> None:
        while not self.stop.is_set():
            try:
                b = urllib.request.urlopen(f"{self.cockpit.base}/api/rgbd", timeout=5).read()
                hl = struct.unpack(">I", b[4:8])[0]
                i = 8 + hl
                pl = struct.unpack(">I", b[i : i + 4])[0]
                png = b[i + 4 : i + 4 + pl]
                t = time.time() - self.t0
                name = f"{self.out}/c/{int(t * 1000):08d}.png"
                open(name, "wb").write(png)
                with self.lock:
                    self.idx["c"].append((t, name))
                time.sleep(0.25)
            except Exception:
                time.sleep(0.3)

    def finish(self) -> dict:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)
        json.dump(self.idx, open(f"{self.out}/index.json", "w"))
        return {k: len(v) for k, v in self.idx.items()}


# -- CLI --------------------------------------------------------------------------------


def add_pick_cycle_args(ap) -> None:
    ap.add_argument(
        "--cockpit", default=DEFAULT_COCKPIT, help=f"the running cockpit (default {DEFAULT_COCKPIT})"
    )
    ap.add_argument(
        "--drop",
        action="store_true",
        help="after the in-place pass, drop each block from --drop-mm to shuffle",
    )
    ap.add_argument(
        "--tip-m",
        type=float,
        default=DEFAULT_TIP_M,
        help="flange-to-fingertip length (default 0.163: Hand-E + adapter)",
    )
    ap.add_argument("--lift-mm", type=float, default=25.4, help="in-place lift (default 25.4 = one inch)")
    ap.add_argument("--drop-mm", type=float, default=120.0, help="release height for the drop pass")
    ap.add_argument(
        "--grasp-below-mm", type=float, default=15.0, help="how far below the top face the pads close"
    )
    ap.add_argument(
        "--record",
        default=None,
        metavar="DIR",
        help="record the three cockpit feeds + events for a timelapse",
    )
    ap.add_argument(
        "--survey-pose",
        type=float,
        nargs=6,
        action="append",
        default=None,
        metavar="V",
        help="a flange pose [x y z rx ry rz] overlooking the blocks from >= 0.25 m; repeat for several "
        "views, merged by position (default: survey from where the arm is)",
    )
    ap.add_argument("--dry-run", action="store_true", help="survey and plan, send no motion")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")


def run_pick_cycle(args) -> int:
    cockpit = Cockpit(args.cockpit)
    cycle = PickCycle(
        cockpit,
        tip_m=args.tip_m,
        lift_mm=args.lift_mm,
        drop_mm=args.drop_mm,
        grasp_below_mm=args.grasp_below_mm,
        dry_run=args.dry_run,
    )
    rec = None
    if args.record:
        rec = Recorder(cockpit, args.record, cycle.t0)
        rec.start()
        time.sleep(1.0)
    try:
        if args.dry_run:
            blocks = cycle.survey_from(args.survey_pose)
            out = {"ok": True, "dry_run": True, "blocks": [b.__dict__ for b in blocks]}
            for b in blocks:
                cycle.say(
                    f"block {b.index + 1}: top {[round(v, 3) for v in b.centre_base]} m, "
                    f"{b.major_m * 1000:.0f} x {b.minor_m * 1000:.0f} mm at {math.degrees(b.theta):.0f} deg"
                )
        else:
            out = cycle.run(drop=args.drop, survey_poses=args.survey_pose)
    except (CockpitError, urllib.error.URLError) as exc:
        print(f"pick-cycle: {exc}", file=sys.stderr)
        return 1
    finally:
        if rec is not None:
            time.sleep(2.0)
            counts = rec.finish()
            json.dump(cycle.log, open(f"{args.record}/events.json", "w"), indent=1)
            print(f"recorded {counts} → {args.record}", file=sys.stderr)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    return 0 if out.get("ok") else 1
