"""The pick server — what the PolyScope 5 **RealSense Pick** program node talks to.

The node's URScript runs *inside* the robot program the operator plays from the
pendant, so it runs in Local mode, needs no Primary connection and no cockpit robot
link. At the node the controller opens a plain TCP socket to this server (the
cockpit's host, ``--pick-port``, default 7622), sends one line with its own flange
pose, and reads back one parenthesised list of numbers — the shape URScript's
``socket_read_ascii_float`` parses. The controller then does the moving itself.

Requests (one line each, ASCII, ≤ 1 kB; the pose is URScript's ``to_str(pose)``):

``FIND p[x, y, z, rx, ry, rz] [u=<px> v=<px>] [lean=<deg>]``
    Detect the white blocks in a colour+depth frame taken *after* the request
    arrived (the arm has stopped by then), place them in the base frame through the
    hand-eye and the flange pose sent, and pick one: the block nearest pixel
    ``(u, v)`` — the spot the operator tapped when teaching the node — or, without
    one, the block nearest the image centre.

``REFINE p[x, y, z, rx, ry, rz] p[cx, cy, cz, 0, 0, 0] [lean=<deg>]``
    The second, closer look: the block whose top centre lies within 60 mm of
    ``(cx, cy, cz)`` (the FIND answer), seen from the flange pose sent.

``lean`` (0–30°, default 0): the grasp is **straight down** — the work surface is
flat and parallel to the base XY plane (Nick, 2026-09-27) — unless the program's own
IK check found that unsolvable and asks again leaned outward (the pick-cycle's
0 → 12 → 24° ladder, :func:`perception.pickcycle.grasp_rotation`).

Reply: ``(status, cx, cy, cz, x, y, z, rx, ry, rz)``. ``status`` is one of
:data:`STATUS`; on 1, ``c*`` is the block's top-face centre (base, m) and the pose
is the **flange** pose that puts the fingertips (``PERCEPTION_TIP_M`` along flange
+Z) on that centre, tool Z straight down (or leaned), the flange heading kept from the
pose sent and turned so the fingers close across the block's short side. The node
derives hover, grip and lift from it with ``pose_trans`` along the tool axis. On any
other status the numbers are 0.

The server never moves anything and never talks to the robot: it answers questions
about the image. It shares the cockpit's (unauthenticated, trusted-cell) network
exposure — bind it the same way.
"""

from __future__ import annotations

import math
import re
import socketserver
import threading
from collections.abc import Callable, Sequence

from urctl.pose import Transform

from .pickcycle import Block, detect_blocks, grasp_rotation, grasp_yaw_deg, tip_pose

DEFAULT_PICK_PORT = 7622
MAX_LINE = 1024
REFINE_RADIUS_M = 0.06
DEFAULT_STROKE_M = 0.050  # Hand-E
FRESH_FRAMES = 2  # frames after the request's arrival before one is trusted still

STATUS = {
    1: "found",
    0: "no block in view",
    -1: "the block is wider than the gripper's stroke",
    -2: "the block is too close to the robot's base",
    -3: "the cockpit has no hand-eye calibration",
    -4: "no fresh camera frame",
    -5: "the second look did not find the block again",
    -9: "malformed request",
}

_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_POSE_RX = re.compile(
    rf"p\[\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*,\s*({_NUM})\s*\]"
)
_KV_RX = re.compile(r"\b([uv])=(-?\d{1,5})\b")
_LEAN_RX = re.compile(rf"\blean=({_NUM})")
MAX_LEAN_DEG = 30.0


class RequestError(ValueError):
    """The line is not a request this server answers."""


def parse_request(line: str) -> dict:
    """``{"verb", "flange", "near" (REFINE), "pixel" (FIND, optional)}`` or RequestError."""
    text = line.strip()
    if not text or len(text) > MAX_LINE:
        raise RequestError("empty or oversized request")
    verb = text.split(None, 1)[0].upper()
    if verb not in ("FIND", "REFINE"):
        raise RequestError(f"unknown verb {verb[:16]!r}")
    poses = [[float(g) for g in m.groups()] for m in _POSE_RX.finditer(text)]
    if not poses or not all(math.isfinite(v) and abs(v) < 100.0 for p in poses for v in p):
        raise RequestError("no plausible pose p[x, y, z, rx, ry, rz] in the request")
    out: dict = {"verb": verb, "flange": poses[0], "lean": 0.0}
    lean = _LEAN_RX.search(text)
    if lean:
        out["lean"] = float(lean.group(1))
        if not (0.0 <= out["lean"] <= MAX_LEAN_DEG):
            raise RequestError(f"lean must be within 0..{MAX_LEAN_DEG:.0f} deg")
    if verb == "REFINE":
        if len(poses) < 2:
            raise RequestError("REFINE needs the flange pose and the block centre")
        out["near"] = poses[1][:3]
    else:
        kv = {k: int(v) for k, v in _KV_RX.findall(text)}
        if "u" in kv and "v" in kv and kv["u"] >= 0 and kv["v"] >= 0:
            out["pixel"] = (kv["u"], kv["v"])
    return out


def format_reply(
    status: int, centre: Sequence[float] | None = None, pose: Sequence[float] | None = None
) -> str:
    """One ``socket_read_ascii_float`` line: ``(status, cx, cy, cz, x, …, rz)``."""
    vals = (
        [float(status)]
        + [float(v) for v in (centre or (0.0,) * 3)]
        + [float(v) for v in (pose or (0.0,) * 6)]
    )
    return "(" + ",".join(f"{v:.6f}" for v in vals) + ")\n"


def choose(blocks: list[Block], pixel: tuple[int, int] | None, width: int, height: int) -> Block | None:
    """The block nearest ``pixel`` (the taught tap), else nearest the image centre."""
    if not blocks:
        return None
    u, v = pixel if pixel is not None else (width / 2.0, height / 2.0)
    return min(blocks, key=lambda b: (b.pixel[0] - u) ** 2 + (b.pixel[1] - v) ** 2)


class PickPlanner:
    """Frames in, one answer out. ``frame_source(after_seq)`` returns
    ``(seq, w, h, ch, rgb, depth, depth_scale_m, K)`` for a frame newer than
    ``after_seq`` (or None); ``latest_seq()`` the newest sequence number;
    ``handeye()`` the flange → colour-camera pose (or None)."""

    def __init__(
        self,
        frame_source: Callable[[int], tuple | None],
        latest_seq: Callable[[], int],
        handeye: Callable[[], Sequence[float] | None],
        *,
        tip_m: float,
        stroke_m: float = DEFAULT_STROKE_M,
        min_radius_m: float = 0.2,
        finger_axis: str = "y",
        log: Callable[[str, bool], None] | None = None,
    ):
        self.frame_source, self.latest_seq, self.handeye = frame_source, latest_seq, handeye
        self.tip_m, self.stroke_m, self.min_radius_m, self.finger_axis = (
            tip_m,
            stroke_m,
            min_radius_m,
            finger_axis,
        )
        self.log = log or (lambda text, ok: None)

    def answer(self, line: str) -> str:
        try:
            req = parse_request(line)
        except RequestError as exc:
            self.log(f"pick request refused: {exc}", False)
            return format_reply(-9)
        status, centre, pose = self._plan(req)
        what = STATUS.get(status, "?")
        where = f" top {[round(c, 3) for c in centre]}" if centre else ""
        self.log(f"pick {req['verb']}: {what}{where}", status == 1)
        return format_reply(status, centre, pose)

    def _plan(self, req: dict) -> tuple[int, list[float] | None, list[float] | None]:
        he = self.handeye()
        if not he:
            return -3, None, None
        arrived = self.latest_seq()
        frame = self.frame_source(arrived + FRESH_FRAMES - 1)
        if frame is None:
            return -4, None, None
        _, w, h, ch, rgb, depth, scale, K = frame
        flange = req["flange"]
        blocks = detect_blocks(
            w, h, ch, rgb, depth, scale, K, Transform.from_pose(he), Transform.from_pose(flange)
        )
        if req["verb"] == "REFINE":
            near = req["near"]
            close = [b for b in blocks if math.dist(b.centre_base[:2], near[:2]) < REFINE_RADIUS_M]
            if not close:
                return -5, None, None
            blk = min(close, key=lambda b: math.dist(b.centre_base[:2], near[:2]))
        else:
            blk = choose(blocks, req.get("pixel"), w, h)
            if blk is None:
                return 0, None, None
        if blk.minor_m > self.stroke_m - 0.006:
            return -1, blk.centre_base, None
        if self.min_radius_m and math.hypot(blk.centre_base[0], blk.centre_base[1]) < self.min_radius_m:
            return -2, blk.centre_base, None
        rot = grasp_rotation(flange, blk.centre_base, req["lean"])
        yaw = grasp_yaw_deg(rot, blk.theta + math.pi / 2, self.finger_axis)
        return 1, list(blk.centre_base), tip_pose(blk.centre_base, rot, self.tip_m, yaw)


class _Handler(socketserver.StreamRequestHandler):
    timeout = 30.0  # a controller that connects and goes quiet does not hold a thread

    def handle(self) -> None:
        planner: PickPlanner = self.server.planner  # type: ignore[attr-defined]
        while True:
            try:
                raw = self.rfile.readline(MAX_LINE + 1)
            except OSError:
                return
            if not raw:
                return
            if len(raw) > MAX_LINE:
                self.wfile.write(format_reply(-9).encode())
                return  # a line that long is not a controller; drop the connection
            try:
                line = raw.decode("ascii")
            except UnicodeDecodeError:
                self.wfile.write(format_reply(-9).encode())
                continue
            self.wfile.write(planner.answer(line).encode())


class PickServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, bind: str, port: int, planner: PickPlanner):
        super().__init__((bind, port), _Handler)
        self.planner = planner
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="pick-server", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
