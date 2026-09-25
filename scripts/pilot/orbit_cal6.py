# ruff: noqa  — scratch script from the 2026-09-25 hardware session (scripts/pilot/README.md)
import os as _os

HERE = _os.path.dirname(_os.path.abspath(__file__))
import json
import math
import time

src = open(HERE + "/orbit_cal4.py").read()
exec(src.split("track = Transform")[0])  # helpers: post/get/frame/white_blobs/orbit_pose/top_face_click/K
track = Transform.from_pose(
    [0.005524, 0.084045, -0.011155, -0.046562, 0.309993, -3.10718]
)  # run-5 solve (flange→depth≈colour)
mark = [-0.20726, 0.23342, -0.25120]
R_home = Transform.from_pose([-0.2325, 0.2776, -0.040, -2.9309, 0.151, -0.1047])


def centred_pose(rng):
    """flange pose (home orientation) that puts the mark on the optical axis at range `rng`"""
    cam_pt = track.apply((0.0, 0.0, rng))  # mark in the flange frame
    t = [mark[i] - R_home.rotate(cam_pt)[i] for i in range(3)]  # t_bf = mark − R_bf·p_flange
    return [*t, *R_home.to_pose()[3:]]


def predict(flange_pose):
    p_cam = track.inverse().apply(Transform.from_pose(flange_pose).inverse().apply(mark))
    return (
        None
        if p_cam[2] <= 0
        else (K["ppx"] + K["fx"] * p_cam[0] / p_cam[2], K["ppy"] + K["fy"] * p_cam[1] / p_cam[2], p_cam)
    )


plan = []
for rng, tilt, yaw, sh in ((0.21, 12, 25, 0.03), (0.30, 15, 30, 0.05), (0.40, 15, 30, 0.07)):
    h = centred_pose(rng)
    plan.append((f"c@{rng}", h))
    for name, axis, deg in (
        ("X+", (1, 0, 0), tilt),
        ("X-", (1, 0, 0), -tilt),
        ("Y+", (0, 1, 0), tilt),
        ("Y-", (0, 1, 0), -tilt),
        ("Z+", (0, 0, 1), yaw),
        ("Z-", (0, 0, 1), -yaw),
        ("XZ", (0.55, 0, 0.83), 22),
        ("YZ", (0, -0.55, 0.83), 22),
    ):
        plan.append((f"{name}@{rng}", orbit_pose(h, mark, axis, deg)))
    for name, sx, sy in (("sX+", sh, 0), ("sX-", -sh, 0), ("sY+", 0, sh), ("sY-", 0, -sh)):
        plan.append((f"{name}@{rng}", [h[0] + sx, h[1] + sy, *h[2:]]))
print("reset:", post("/api/cal/reset", {}).get("ok"), "planned:", len(plan))
kept = 0
for name, pose in plan:
    pr = predict(pose)
    if pr is None or not (60 < pr[0] < 788 and 60 < pr[1] < 420):
        print(f"[{name}] predicted {None if pr is None else (round(pr[0]), round(pr[1]))} — outside, skipped")
        continue
    if math.hypot(pose[0], pose[1], pose[2]) > 0.48:
        print(f"[{name}] beyond reach — skipped")
        continue
    r = post("/api/robot/move", {"pose": pose, "velocity": 0.08, "tcp": [0] * 6})
    if not r.get("ok"):
        print(
            f"[{name}] move failed: {r.get('error')} pstop={r.get('protective_stop')} viol={(r.get('safety') or {}).get('violations')}"
        )
        if r.get("protective_stop"):
            post("/api/robot/bring_up", {})
            time.sleep(1)
        continue
    time.sleep(0.7)
    hdr, w, h, ch, rgb, depth = frame()
    blobs = [b for b in white_blobs(w, h, ch, rgb) if b["px"] > 400]
    cands = []
    for b in blobs:
        x0, y0, x1, y1 = b["bbox"]
        if x0 <= 6 or y0 <= 6 or x1 >= w - 6 or y1 >= h - 6:
            continue
        tf = top_face_click(w, h, ch, rgb, depth, b["bbox"], hdr["depth_scale_m"])
        if tf is None:
            continue
        tx, ty, c3, nsel, nall = tf
        d3 = math.hypot(*(c3[i] - pr[2][i] for i in range(3)))
        cands.append((d3, b, tf))
    if not cands:
        print(f"[{name}] no candidate block (pred {round(pr[0])},{round(pr[1])})")
        continue
    d3, b, (tx, ty, c3, nsel, nall) = min(cands, key=lambda c: c[0])
    if d3 > 0.04:
        print(f"[{name}] nearest top face is {d3 * 1000:.0f} mm from the predicted mark — skipped")
        continue
    v = post("/api/cal/view", {"x": tx, "y": ty})
    kept += 1 if v.get("ok") else 0
    print(
        f"[{name}] pred ({pr[0]:.0f},{pr[1]:.0f}) click ({tx},{ty}) d3={d3 * 1000:.0f}mm sel {nsel}/{nall} ok={v.get('ok')}"
    )
print("views kept:", kept)
res = post("/api/cal/solve", {})
print(
    "solve:",
    json.dumps(
        {
            k: res.get(k)
            for k in (
                "ok",
                "error",
                "rms_m",
                "views",
                "rotation_diversity_deg",
                "mark_base",
                "warnings",
                "iterations",
            )
        },
        default=str,
    ),
)
print("per-view residual mm:", [round(v * 1000, 1) for v in res.get("per_view_residual_m") or []])
print("flange_to_depth:", [round(v, 4) for v in res.get("flange_to_depth_pose") or []])
print("env_line:", res.get("env_line"))
json.dump(get("/api/cal")["views"], open(HERE + "/cal_views_run6.json", "w"))
