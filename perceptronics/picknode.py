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

``LOOK p[x, y, z, rx, ry, rz] p[cx, cy, cz, 0, 0, 0]``
    Where to take the closer look from: the flange pose that puts the camera halfway
    from where it is now to the block's top centre (never nearer than
    :data:`LOOK_MIN_M` — the D435 has no depth closer than ~0.2 m), aimed so the block
    is in the middle of the picture, and backed out along that line until the
    fingertips clear the top by :data:`LOOK_TIP_CLEAR_M`. Status -6 when no such pose
    exists (the node then looks from straight over the block, as before).

``LOG <text>``
    The robot program saying where it is (``start``, ``FIND status 1``, ``hover`` …):
    written to the server's log, printable ASCII only, capped; **no reply**.

``lean`` (0–30°, default 0): the grasp is **straight down** — the work surface is
flat and parallel to the base XY plane (Nick, 2026-09-27) — unless the program's own
IK check found that unsolvable and asks again leaned outward (the pick-cycle's
0 → 12 → 24° ladder, :func:`perceptronics.pickcycle.grasp_rotation`).

Reply: ``(status, cx, cy, cz, x, y, z, rx, ry, rz)``. ``status`` is one of
:data:`STATUS`; on 1, ``c*`` is the block's top-face centre (base, m) and the pose
is the **flange** pose that puts the fingertips (``PERCEPTRONICS_TIP_M`` along flange
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
LOOK_MIN_M = 0.25  # camera to the block's top, the closest the D435 still measures well
LOOK_TIP_CLEAR_M = 0.06  # fingertips above the top at the look pose
LOOK_MAX_TILT_DEG = 60.0  # tool Z from straight down
MAX_LOG_TEXT = 240
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
    -6: "no look pose keeps the camera in range and the fingertips clear",
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
    """``{"verb", "flange", "near" (REFINE, LOOK), "pixel" (FIND, optional)}``,
    ``{"verb": "LOG", "text"}``, or RequestError."""
    text = line.strip()
    if text[:4].upper() in ("LOG", "LOG "):  # the program never reads a reply to LOG: never refuse one
        said = text[3:].strip()[: MAX_LOG_TEXT * 2]
        clean = "".join(c if " " <= c <= "~" else "?" for c in said)[:MAX_LOG_TEXT]
        return {"verb": "LOG", "text": clean}
    if not text or len(text) > MAX_LINE:
        raise RequestError("empty or oversized request")
    verb = text.split(None, 1)[0].upper()
    if verb not in ("FIND", "REFINE", "LOOK"):
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
    if verb in ("REFINE", "LOOK"):
        if len(poses) < 2:
            raise RequestError(f"{verb} needs the flange pose and the block centre")
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


def look_pose(
    flange: Sequence[float],
    top: Sequence[float],
    handeye: Sequence[float],
    tip_m: float,
    *,
    min_m: float = LOOK_MIN_M,
    tip_clear_m: float = LOOK_TIP_CLEAR_M,
    max_tilt_deg: float = LOOK_MAX_TILT_DEG,
) -> list[float] | None:
    """The flange pose for the closer look (see ``LOOK``), or None when none will do.

    The camera goes halfway along the line from where it is to ``top`` (not nearer
    than ``min_m``, never farther than it already is) and turns to aim its optical
    axis at ``top``, keeping its image X as close as it was (the least wrist roll).
    If the fingertips would come within ``tip_clear_m`` of the top's height, the
    camera backs out along the same line; a tool tilted past ``max_tilt_deg`` from
    straight down is refused."""
    T_fc = Transform.from_pose(handeye)
    T_bc = Transform.from_pose(flange).compose(T_fc)
    cam = T_bc.translation
    away = [cam[i] - top[i] for i in range(3)]
    d0 = math.hypot(*away)
    if d0 < 1e-6:
        return None
    unit = [a / d0 for a in away]
    z = [-u for u in unit]  # the optical axis, toward the block
    x_cam = T_bc.rotate((1.0, 0.0, 0.0))
    dot = sum(x_cam[i] * z[i] for i in range(3))
    x = [x_cam[i] - dot * z[i] for i in range(3)]
    if math.hypot(*x) < 1e-6:
        return None
    n = math.hypot(*x)
    x = [v / n for v in x]
    y = [z[1] * x[2] - z[2] * x[1], z[2] * x[0] - z[0] * x[2], z[0] * x[1] - z[1] * x[0]]
    start = min(d0, max(min_m, d0 / 2.0))
    steps = 20
    for k in range(steps + 1):
        d = start + (d0 - start) * k / steps
        origin = [top[i] + unit[i] * d for i in range(3)]
        T_bf = Transform.from_axes(x, y, z, origin).compose(T_fc.inverse())
        tip = T_bf.apply((0.0, 0.0, tip_m))
        tool_z = T_bf.rotate((0.0, 0.0, 1.0))
        if math.degrees(math.acos(max(-1.0, min(1.0, -tool_z[2])))) > max_tilt_deg:
            return None
        if tip[2] >= top[2] + tip_clear_m:
            return T_bf.to_pose()
    return None


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
        if req["verb"] == "LOG":
            self.log(f"robot: {req['text']}", True)
            return ""  # the program does not read a reply to LOG
        status, centre, pose = self._look(req) if req["verb"] == "LOOK" else self._plan(req)
        what = STATUS.get(status, "?")
        where = f" top {[round(c, 3) for c in centre]}" if centre else ""
        self.log(f"pick {req['verb']}: {what}{where}", status == 1)
        return format_reply(status, centre, pose)

    def plan(self, flange: Sequence[float], pixel: tuple[int, int] | None = None, lean: float = 0.0) -> dict:
        """A FIND for a caller that already has the flange pose (the node's teach-time
        check through the cockpit): ``{status, reason, centre, top_pose}``."""
        req = {"verb": "FIND", "flange": [float(v) for v in flange], "lean": float(lean)}
        if pixel is not None:
            req["pixel"] = pixel
        status, centre, pose = self._plan(req)
        return {"status": status, "reason": STATUS.get(status, "?"), "centre": centre, "top_pose": pose}

    def _look(self, req: dict) -> tuple[int, list[float] | None, list[float] | None]:
        he = self.handeye()
        if not he:
            return -3, None, None
        pose = look_pose(req["flange"], req["near"], he, self.tip_m)
        return (1, list(req["near"]), pose) if pose else (-6, list(req["near"]), None)

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


# -- the node's teach screen (the cockpit's routes and the stand-alone pick server's) ---------


def parse_preview_request(payload: dict) -> tuple[tuple[int, int] | None, float, float]:
    """``POST /api/pick/preview``'s body -> ``(pixel, grip_below_mm, hover_mm)``;
    ValueError when a field isn't a number or is out of range."""
    try:
        u, v = int(payload.get("u", -1)), int(payload.get("v", -1))
        grip = float(payload.get("grip_below_mm", 15.0))
        hover = float(payload.get("hover_mm", 40.0))
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError(str(exc)) from None
    if not (0.0 <= grip <= 60.0 and 0.0 <= hover <= 300.0):
        raise ValueError("grip_below_mm must be 0..60 and hover_mm 0..300")
    return ((u, v) if u >= 0 and v >= 0 else None), grip, hover


def preview(
    planner: PickPlanner,
    flange_pose: dict,
    pixel: tuple[int, int] | None,
    *,
    grip_below_mm: float = 15.0,
    hover_mm: float = 40.0,
) -> dict:
    """The node's teach-time check: what the program would do from where the arm is
    now. ``flange_pose`` is a ``get_flange_pose`` result (``flange``, ``tcp_offset``,
    ``tcp_offset_consistent``); one FIND, and the hover and grip poses both as flange
    poses and in the controller's **active** TCP (``polyscope_*``: what PolyScope's
    hold-to-move screen takes). Moves nothing."""
    from urctl.pose import pose_trans

    fp = flange_pose
    if not fp.get("ok") or not fp.get("flange"):
        return {"ok": False, "error": fp.get("error") or "could not read the flange pose", "robot": fp}
    planned = planner.plan(fp["flange"], pixel)
    out: dict = {"ok": planned["status"] == 1, **planned, "flange_pose": fp["flange"]}
    if planned["status"] != 1:
        out["error"] = planned["reason"]
        return out
    top = planned["top_pose"]
    out["hover_pose"] = pose_trans(top, [0.0, 0.0, -hover_mm / 1000.0, 0.0, 0.0, 0.0])
    out["grip_pose"] = pose_trans(top, [0.0, 0.0, grip_below_mm / 1000.0, 0.0, 0.0, 0.0])
    offset = fp.get("tcp_offset")
    if offset is not None and fp.get("tcp_offset_consistent") is not False:
        out["polyscope_hover_pose"] = pose_trans(out["hover_pose"], offset)
        out["polyscope_grip_pose"] = pose_trans(out["grip_pose"], offset)
    else:
        out["polyscope_note"] = "the controller's active TCP offset is unknown or inconsistent"
    return out


def detect_report(frame: tuple, *, pick_port: int | None, handeye: bool, tip_m: float | None) -> dict:
    """What the node's teach screen draws: the blocks the pick server would choose
    among, in image pixels, from one ``(seq, w, h, ch, rgb, depth, scale, K)`` frame
    (camera frame — no robot needed)."""
    seq, w, h, ch, rgb, depth, scale, K = frame
    blocks = detect_blocks(w, h, ch, rgb, depth, scale, K, Transform(), Transform())
    return {
        "ok": True,
        "seq": seq,
        "width": w,
        "height": h,
        "pick_port": pick_port,
        "handeye": handeye,
        "tip_m": tip_m,
        "blocks": [
            {
                "pixel": list(b.pixel),
                "size_mm": [round(b.major_m * 1000), round(b.minor_m * 1000)],
                "distance_m": round(b.centre_base[2], 3),
            }
            for b in blocks
        ],
    }


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
                if raw.lstrip()[:3].upper() == b"LOG":  # never answered, whatever it holds
                    planner.answer(raw.decode("ascii", errors="replace"))
                    continue
                self.wfile.write(format_reply(-9).encode())
                continue
            answer = planner.answer(line)
            if answer:
                self.wfile.write(answer.encode())


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
