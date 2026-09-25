# ruff: noqa  — scratch script from the 2026-09-25 hardware session (scripts/pilot/README.md)
import os as _os

HERE = _os.path.dirname(_os.path.abspath(__file__))
import json
import math
import time
import urllib.request

exec(open(HERE + "/orbit_cal.py").read().split("home = [")[0])


def get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=30))


K = get("/api/info")["camera"]["intrinsics"]["color"]


def top_face_click(w, h, ch, rgb, depth, bbox, scale):
    """3-D centroid of the block's top face: white pixels → 3-D points; seed = nearest 12 mm;
    fit a plane to the seed, keep points within 5 mm of it, refit twice; return the centroid's pixel."""
    x0, y0, x1, y1 = bbox
    pts = []
    for y in range(y0, min(y1, h)):
        for x in range(x0, min(x1, w)):
            i = (y * w + x) * ch
            r, g, b = rgb[i], rgb[i + 1], rgb[i + 2]
            if min(r, g, b) > 185 and max(r, g, b) - min(r, g, b) < 30:
                d = depth[2 * (y * w + x)] | (depth[2 * (y * w + x) + 1] << 8)
                if d:
                    z = d * scale
                    pts.append(((x - K["ppx"]) * z / K["fx"], (y - K["ppy"]) * z / K["fy"], z))
    if len(pts) < 60:
        return None
    zmin = min(p[2] for p in pts)
    sel = [p for p in pts if p[2] <= zmin + 0.012]
    for _ in range(3):
        c = [sum(p[k] for p in sel) / len(sel) for k in range(3)]
        # plane normal = smallest-eigenvector of the covariance (power iteration on the inverse is overkill: use cross products of spread axes)
        cov = [[sum((p[a] - c[a]) * (p[b] - c[b]) for p in sel) for b in range(3)] for a in range(3)]
        # normal via the cross product of the two largest spread directions (approx.: x and y spread dominate a face seen from above)
        ex = (cov[0][0], cov[0][1], cov[0][2])
        ey = (cov[1][0], cov[1][1], cov[1][2])
        n = (ex[1] * ey[2] - ex[2] * ey[1], ex[2] * ey[0] - ex[0] * ey[2], ex[0] * ey[1] - ex[1] * ey[0])
        nn = math.sqrt(sum(v * v for v in n)) or 1.0
        n = [v / nn for v in n]
        sel = [p for p in pts if abs(sum((p[k] - c[k]) * n[k] for k in range(3))) < 0.005]
        if len(sel) < 40:
            return None
    c = [sum(p[k] for p in sel) / len(sel) for k in range(3)]
    return (
        int(round(K["ppx"] + K["fx"] * c[0] / c[2])),
        int(round(K["ppy"] + K["fy"] * c[1] / c[2])),
        c,
        len(sel),
        len(pts),
    )


track = Transform.from_pose([0.009, 0.0784, -0.0012, -0.0087, -0.2842, 3.0495])
mark = [-0.2078, 0.2381, -0.2595]


def predict_pixel(flange_pose):
    p_cam = track.inverse().apply(Transform.from_pose(flange_pose).inverse().apply(mark))
    return (
        None
        if p_cam[2] <= 0
        else (K["ppx"] + K["fx"] * p_cam[0] / p_cam[2], K["ppy"] + K["fy"] * p_cam[1] / p_cam[2])
    )


home = [-0.2325, 0.2776, -0.040, -2.9309, 0.151, -0.1047]
plan = []
for z, tilts, yaws, shifts in ((-0.04, 12, 0, 0.0), (0.06, 15, 30, 0.04), (0.16, 15, 30, 0.06)):
    plan.append(("home", None, 0, z, (0, 0)))
    plan += [
        ("X+", (1, 0, 0), tilts, z, (0, 0)),
        ("X-", (1, 0, 0), -tilts, z, (0, 0)),
        ("Y+", (0, 1, 0), tilts, z, (0, 0)),
        ("Y-", (0, 1, 0), -tilts, z, (0, 0)),
    ]
    if yaws:
        plan += [
            ("Z+", (0, 0, 1), yaws, z, (0, 0)),
            ("Z-", (0, 0, 1), -yaws, z, (0, 0)),
            ("XZ", (0.55, 0, 0.83), 25, z, (0, 0)),
            ("YZ", (0, -0.55, 0.83), 25, z, (0, 0)),
        ]
    if shifts:
        plan += [
            ("sX+", None, 0, z, (shifts, 0)),
            ("sX-", None, 0, z, (-shifts, 0)),
            ("sY+", None, 0, z, (0, shifts)),
            ("sY-", None, 0, z, (0, -shifts)),
        ]
print("reset:", post("/api/cal/reset", {}).get("ok"), "planned views:", len(plan))
kept = []
for name, axis, deg, z, (sx, sy) in plan:
    bp = [home[0] + sx, home[1] + sy, z, *home[3:]]
    pose = bp if axis is None else orbit_pose(bp, mark, axis, deg)
    pred = predict_pixel(pose)
    if pred is None or not (40 < pred[0] < 808 and 40 < pred[1] < 440):
        print(
            f"[{name}@{z:+.2f}] mark predicted at {None if pred is None else (round(pred[0]), round(pred[1]))} — outside, skipped"
        )
        continue
    r = post("/api/robot/move", {"pose": pose, "velocity": 0.08, "tcp": [0] * 6})
    if not r.get("ok"):
        print(
            f"[{name}@{z:+.2f}] move failed: {r.get('error')} pstop={r.get('protective_stop')} viol={(r.get('safety') or {}).get('violations')}"
        )
        if r.get("protective_stop"):
            post("/api/robot/bring_up", {})
            time.sleep(1)
        continue
    time.sleep(0.7)
    hdr, w, h, ch, rgb, depth = frame()
    blobs = [b for b in white_blobs(w, h, ch, rgb) if b["px"] > 500]
    if not blobs:
        print(f"[{name}@{z:+.2f}] no white blob")
        continue
    b = min(blobs, key=lambda b: math.hypot(b["cx"] - pred[0], b["cy"] - pred[1]))
    dist = math.hypot(b["cx"] - pred[0], b["cy"] - pred[1])
    x0, y0, x1, y1 = b["bbox"]
    if dist > 100 or x0 <= 8 or y0 <= 8 or x1 >= w - 8 or y1 >= h - 8:
        print(
            f"[{name}@{z:+.2f}] pred ({pred[0]:.0f},{pred[1]:.0f}) nearest blob {(b['cx'], b['cy'])} d={dist:.0f} bbox={b['bbox']} — skipped"
        )
        continue
    tf = top_face_click(w, h, ch, rgb, depth, b["bbox"], hdr["depth_scale_m"])
    if tf is None:
        print(f"[{name}@{z:+.2f}] top face not fit")
        continue
    tx, ty, c3, nsel, nall = tf
    v = post("/api/cal/view", {"x": tx, "y": ty})
    pc = (v.get("views") or [{}])[-1].get("point_cam") if v.get("ok") else None
    if v.get("ok"):
        kept.append(name)
    print(
        f"[{name}@{z:+.2f}] pred ({pred[0]:.0f},{pred[1]:.0f}) blob {(b['cx'], b['cy'])} top-face centre px ({tx},{ty}) 3D {[round(q, 3) for q in c3]} sel {nsel}/{nall} → ok={v.get('ok')} cam={[round(q, 4) for q in pc] if pc else v.get('error')}"
    )
print("views kept:", len(kept))
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
json.dump(get("/api/cal")["views"], open(HERE + "/cal_views_run5.json", "w"))
