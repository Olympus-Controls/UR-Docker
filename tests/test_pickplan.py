"""perception.pickplan: the approach Nick specified (2026-09-27) — straight down the
base Z axis, fingers across the short side at 1.2x its width, fingertips 25 mm over
the top — and a blended program the controller will accept."""

from __future__ import annotations

import math

import pytest

from perception import pickplan
from urctl.pose import Transform

HOME = [-0.23587, 0.18254, 0.22134, -2.51512, 0.17760, 0.09959]  # the ur3 cell's picture pose
TIP = 0.163


def block(cx, cy, top, major, minor, theta_deg, n=24):
    """The top face of a rectangular block: a grid of points, plus its sides lower down."""
    th = math.radians(theta_deg)
    u, w = (math.cos(th), math.sin(th)), (-math.sin(th), math.cos(th))
    pts = []
    for i in range(n):
        for j in range(n):
            a, b = (i / (n - 1) - 0.5) * major, (j / (n - 1) - 0.5) * minor
            pts.append([cx + a * u[0] + b * w[0], cy + a * u[1] + b * w[1], top])
            pts.append([cx + a * u[0] + b * w[0], cy + a * u[1] + b * w[1], top - 0.02])  # a side
    return pts


@pytest.mark.parametrize("theta", [-60.0, 0.0, 25.0, 89.0])
def test_rectangle_recovers_a_block(theta):
    r = pickplan.rectangle(pickplan.top_face(block(-0.22, 0.38, -0.268, 0.044, 0.027, theta)))
    assert r["centre"] == pytest.approx([-0.22, 0.38, -0.268], abs=1e-6)
    assert r["major_m"] == pytest.approx(0.044, rel=0.1) and r["minor_m"] == pytest.approx(0.027, rel=0.1)
    d = (math.degrees(r["theta"]) - theta + 90) % 180 - 90
    assert abs(d) < 1.0


def test_top_face_ignores_a_few_flying_pixels():
    pts = block(0.3, 0.0, -0.1, 0.04, 0.02, 0.0) + [[0.3, 0.0, 0.2]] * 5
    assert all(abs(p[2] + 0.1) < 0.007 for p in pickplan.top_face(pts))


def test_robotiq_position_matches_the_measured_reading():
    assert pickplan.robotiq_position(0.0276) == pytest.approx(114, abs=1)  # POS 114 ≈ 27.6 mm (09-25)
    assert pickplan.robotiq_position(0.05) == 0 and pickplan.robotiq_position(0.0) == 255
    assert pickplan.robotiq_position(1.0) == 0 and pickplan.robotiq_position(-1.0) == 255


def _tool(pose):
    T = Transform.from_pose(pose)
    return T, T.rotate((0.0, 0.0, 1.0)), T.rotate((0.0, 1.0, 0.0))


@pytest.mark.parametrize("theta", [-45.0, 10.0, 70.0])
@pytest.mark.parametrize("fancy", [False, True])
@pytest.mark.parametrize("pick", [False, True])
def test_the_approach_nick_asked_for(theta, fancy, pick):
    rect = pickplan.rectangle(pickplan.top_face(block(-0.22, 0.38, -0.268, 0.044, 0.024, theta)))
    p = pickplan.plan(rect, HOME, tip_m=TIP, pick=pick, fancy=fancy)
    T, z, fingers = _tool(p["approach"])
    assert z == pytest.approx((0.0, 0.0, -1.0), abs=1e-9)  # inline with the base Z axis
    tip = [p["approach"][i] + TIP * z[i] for i in range(3)]
    assert tip == pytest.approx(
        [rect["centre"][0], rect["centre"][1], -0.268 + 0.025], abs=1e-6
    )  # 25 mm over the top
    minor_dir = (-math.sin(rect["theta"]), math.cos(rect["theta"]))
    assert abs(fingers[0] * minor_dir[0] + fingers[1] * minor_dir[1]) == pytest.approx(
        1.0, abs=1e-6
    )  # across the short side
    assert p["opening_m"] == pytest.approx(1.2 * rect["minor_m"])
    assert p["gripper_position"] == pickplan.robotiq_position(p["opening_m"])
    names = [leg["name"] for leg in p["legs"]]
    assert names[-1 if not pick else -3] == "approach"
    assert ("swing" in names and "flourish" in names) == fancy
    if pick:
        assert names[-2:] == ["grasp", "lift"] and p["legs"][-2]["gripper"] == "close"
        gz = p["legs"][-2]["pose"][2] - TIP  # the fingertips at the grasp
        assert gz == pytest.approx(-0.268 - 0.015, abs=1e-6)
    # every blend is under half its neighbouring segments, and never on the last leg's successor
    path = [HOME] + [leg["pose"] for leg in p["legs"]]
    for i, leg in enumerate(p["legs"]):
        b = leg.get("blend_m", 0.0)
        if b:
            assert b <= 0.45 * math.dist(path[i][:3], path[i + 1][:3]) + 1e-9
            assert b <= 0.45 * math.dist(path[i + 1][:3], path[i + 2][:3]) + 1e-9
    # the last stretch before the hover runs straight down the Z axis
    over = p["legs"][names.index("over")]["pose"]
    assert over[:2] == pytest.approx(p["approach"][:2], abs=1e-9) and over[2] > p["approach"][2]


def test_skip_drops_a_fancy_via_and_a_wide_block_does_not_fit():
    rect = pickplan.rectangle(pickplan.top_face(block(-0.22, 0.38, -0.268, 0.06, 0.045, 0.0)))
    p = pickplan.plan(rect, HOME, tip_m=TIP, fancy=True, skip=["flourish"])
    assert p["vias"] == ["swing", "over"]
    assert not p["fits"] and p["opening_m"] == pytest.approx(0.05)  # 45 mm x 1.2 > the 50 mm stroke


def test_slerp_is_the_shortest_arc():
    a, b = [0, 0, 0, 0.0, 0.0, 0.0], [0, 0, 0, 0.0, 0.0, 1.2]
    assert pickplan.slerp_rotvec(a, b, 0.5) == pytest.approx([0.0, 0.0, 0.6])
    assert pickplan.slerp_rotvec(a, b, 0.0) == pytest.approx([0.0, 0.0, 0.0], abs=1e-12)


def test_the_cockpit_plans_and_dry_runs_a_pick(monkeypatch):
    import json
    import threading
    import time
    import urllib.request
    from http.server import ThreadingHTTPServer

    from perception.config import PerceptionConfig
    from perception.posestream import PoseStream
    from perception.realsense import SyntheticRgbdCamera
    from perception.robotlink import RobotLink
    from perception.webapp import ViewerApp, ViewerHandler
    from urctl.config import RobotConfig

    monkeypatch.setenv("PERCEPTION_HOME_POSE", ",".join(map(str, HOME)))
    cfg = RobotConfig(host="127.0.0.1")
    ps = PoseStream(cfg, history_s=60.0)
    down = [-0.2, 0.36, 0.3, math.pi, 0.0, 0.0]  # looking straight down from 0.3 m over base
    app = ViewerApp(
        SyntheticRgbdCamera(width=160, height=120, fps=0),
        config=PerceptionConfig(),
        robot=RobotLink(cfg, dry_run=True),
        pose_stream=ps,
    )
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ViewerHandler)
    srv.daemon_threads = True
    srv.app = app  # type: ignore[attr-defined]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    stop = threading.Event()

    def feed():
        while not stop.is_set():
            ps.add(time.time(), down)
            time.sleep(0.01)

    threading.Thread(target=feed, daemon=True).start()

    def post(path, body):
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        try:
            return json.load(urllib.request.urlopen(req, timeout=10))
        except urllib.error.HTTPError as e:
            return json.load(e)

    try:
        app.start()
        deadline = time.monotonic() + 5
        while app.latest()[1] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert post("/api/robot/pick", {})["ok"] is False  # nothing clicked yet
        seg = post("/api/nearest", {"near_ratio": 1.2})
        assert seg["ok"] and seg["features"]
        # the synthetic disk is 80 mm across: too wide to pick, fine to approach
        wide = post("/api/robot/pick", {"plan_only": True, "pick": True})
        assert wide["ok"] is False and "too wide" in wide["error"]
        plan = post("/api/robot/pick", {"plan_only": True, "fancy": True})
        assert plan["ok"] and plan["plan_only"], plan
        assert [leg["name"] for leg in plan["legs"]] == ["swing", "flourish", "look", "over", "approach"]
        assert plan["opening_mm"] == 50.0  # 1.2 x 80 mm, capped at the stroke
        # the synthetic disks are not white blocks: the close look finds no block by identity
        # and the pick stops at the look instead of grabbing whatever lies at the old pixel
        # (on the UR3e, 2026-09-27, that was a patch of carpet)
        run = post("/api/robot/pick", {})
        assert run["ok"] is False and run["stage"] == "look" and "lost the target" in run["error"], run
        run = post("/api/robot/pick", {"look": False})
        assert run["ok"] and run["dry_run"], run
        home = post("/api/robot/home", {})
        assert home["ok"] and home["pose"] == pytest.approx(HOME)
    finally:
        stop.set()
        srv.shutdown()
        srv.server_close()
        app.stop()


UR3_HANDEYE = [0.04332, 0.05955, 0.01279, 0.06471, 0.17204, -3.08350]  # flange → colour, the ur3 cell


@pytest.mark.parametrize("fancy", [False, True])
@pytest.mark.parametrize("pick", [False, True])
def test_the_close_look_puts_the_object_on_the_optical_axis(fancy, pick):
    rect = pickplan.rectangle(pickplan.top_face(block(-0.22, 0.38, -0.268, 0.044, 0.024, 30.0)))
    p = pickplan.plan(rect, HOME, tip_m=TIP, pick=pick, fancy=fancy, flange_to_color=UR3_HANDEYE, look_m=0.24)
    look = p["look"]
    assert [leg["name"] for leg in p["sweep"]][-1] == "look" and p["sweep"][-1]["pose"] == look
    cam = Transform.from_pose(look).compose(Transform.from_pose(UR3_HANDEYE))
    q = cam.inverse().apply(rect["centre"])  # the object's top centre in the camera frame
    assert q[0] == pytest.approx(0.0, abs=1e-9) and q[1] == pytest.approx(0.0, abs=1e-9)
    assert q[2] == pytest.approx(0.24, abs=1e-9)
    assert Transform.from_pose(look).rotate((0, 0, 1)) == pytest.approx(
        (0, 0, -1), abs=1e-9
    )  # already square
    names = [leg["name"] for leg in p["final"]]
    assert names == (["over", "approach", "grasp", "lift"] if pick else ["over", "approach"])
    for start, legs in ((HOME, p["sweep"]), (look, p["final"])):
        path = [start] + [leg["pose"] for leg in legs]
        assert "blend_m" not in legs[-1]
        for i, leg in enumerate(legs):
            b = leg.get("blend_m", 0.0)
            assert not b or (
                b
                <= 0.45
                * min(math.dist(path[i][:3], path[i + 1][:3]), math.dist(path[i + 1][:3], path[i + 2][:3]))
                + 1e-9
            )
    # the fingertips at the look stay well clear of the top (camera 0.24 m off it)
    tip_z = look[2] - TIP
    assert tip_z - (-0.268) > 0.04


def test_top_face_ignores_reflections_floating_above_the_part():
    """Nick, 2026-09-27: 'filter out the obvious outlier reflection pixels floating way
    above the surface' — a scattered spray up to 10 cm over the block, 15 % of the points."""
    import random

    random.seed(3)
    pts = block(0.3, 0.0, -0.1, 0.04, 0.02, 0.0)
    spray = [
        [0.3 + random.uniform(-0.03, 0.03), random.uniform(-0.03, 0.03), -0.1 + random.uniform(0.01, 0.10)]
        for _ in range(len(pts) * 15 // 100)
    ]
    top = pickplan.top_face(pts + spray)
    assert top and all(abs(p[2] + 0.1) < 0.007 for p in top)
