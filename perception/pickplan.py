"""From a clicked object to one fluid program: sweep in, hover, and (optionally) pick.

The work surface is flat and parallel to the base XY plane (Nick, 2026-09-27), so
an object's **top face** is a horizontal slice of its points in the base frame —
no plane fit, no tilt — and the gripper comes in straight down the base Z axis,
wrist 3 turned so the fingers close across the top face's short side, opened to
``opening_factor`` × that width, fingertips ``hover_m`` over the top.

:func:`top_face` + :func:`rectangle` measure; :func:`plan` builds the legs of a
single ``move_tcp_path`` program (flange poses, run with the TCP at the flange):

- the **sweep** from where the camera took the picture to the hover, blended so
  the arm never stops on the way: one via over the object (the last ``drop_m``
  straight down the Z axis), or with ``fancy`` two more — a rise with a sideways
  swing, and a wrist flourish over the object that unwinds on the way down;
- with ``pick``: down to ``grasp_below_m`` under the top, close, lift ``lift_m``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from urctl.pose import Transform

from .pickcycle import grasp_rotation, grasp_yaw_deg, tip_pose

HOVER_M = 0.025  # fingertips over the top face at the approach (Nick, 2026-09-27)
OPENING_FACTOR = 1.2  # fingers 20 % wider than the object
GRASP_BELOW_M = 0.015
LIFT_M = 0.05
DROP_M = 0.06  # the last stretch of the sweep runs straight down the Z axis
STROKE_M = 0.05  # Hand-E


def top_face(points_base: Sequence[Sequence[float]], band_m: float = 0.006) -> list[Sequence[float]]:
    """The object's top: its points within ``band_m`` of the top height (the 90th
    percentile of z, so a few flying pixels above it don't decide)."""
    if len(points_base) < 10:
        return []
    zs = sorted(p[2] for p in points_base)
    top = zs[min(len(zs) - 1, int(len(zs) * 0.9))]
    return [p for p in points_base if abs(p[2] - top) <= band_m]


def rectangle(points: Sequence[Sequence[float]]) -> dict | None:
    """PCA in XY of a (horizontal) top face → ``{centre [x y z], theta (major-axis
    heading, rad), major_m, minor_m, n}``: the extents a uniform rectangle of that
    spread would have."""
    n = len(points)
    if n < 10:
        return None
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    cz = sorted(p[2] for p in points)[n // 2]
    sxx = sum((p[0] - cx) ** 2 for p in points)
    syy = sum((p[1] - cy) ** 2 for p in points)
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points)
    root = math.hypot(sxx - syy, 2 * sxy)
    lam1 = 0.5 * (sxx + syy) + 0.5 * root
    lam2 = max(0.0, 0.5 * (sxx + syy) - 0.5 * root)
    return {
        "centre": [cx, cy, cz],
        "theta": 0.5 * math.atan2(2 * sxy, sxx - syy),
        "major_m": 2 * math.sqrt(3 * lam1 / n),
        "minor_m": 2 * math.sqrt(3 * lam2 / n),
        "n": n,
    }


def robotiq_position(width_m: float, stroke_m: float = STROKE_M) -> int:
    """Robotiq position (0 = open … 255 = closed) for a finger opening of ``width_m``;
    the inverse of pick-cycle's ``(255 − POS) / 255 × stroke`` (POS 114 ≈ 27.6 mm, measured)."""
    w = max(0.0, min(stroke_m, width_m))
    return max(0, min(255, round(255 * (1.0 - w / stroke_m))))


def _blend(prev: Sequence[float], here: Sequence[float], nxt: Sequence[float], cap: float) -> float:
    """A blend radius the controller accepts: under half of both neighbouring segments."""
    a = math.dist(prev[:3], here[:3])
    b = math.dist(here[:3], nxt[:3])
    return round(max(0.0, min(cap, 0.45 * a, 0.45 * b)), 4)


def plan(
    rect: dict,
    start_flange: Sequence[float],
    *,
    tip_m: float,
    pick: bool = False,
    fancy: bool = False,
    hover_m: float = HOVER_M,
    opening_factor: float = OPENING_FACTOR,
    grasp_below_m: float = GRASP_BELOW_M,
    lift_m: float = LIFT_M,
    stroke_m: float = STROKE_M,
    finger_axis: str = "y",
    velocity: float = 0.25,
    acceleration: float = 0.8,
    skip: Sequence[str] = (),
) -> dict:
    """The program for ``rect`` from ``start_flange``: ``{legs, approach, grasp, lift,
    yaw_deg, opening_m, gripper_position, vias}``. Flange poses (TCP at the flange);
    the caller pre-opens the gripper to ``gripper_position`` before running it.
    ``skip`` drops named fancy vias (the ones the controller's IK can't solve)."""
    cx, cy, top = rect["centre"]
    rot = grasp_rotation(start_flange, [cx, cy, top], 0.0)  # straight down, heading kept
    yaw = grasp_yaw_deg(rot, rect["theta"] + math.pi / 2, finger_axis)
    at = lambda h, extra_yaw=0.0: tip_pose([cx, cy, top + h], rot, tip_m, yaw + extra_yaw)  # noqa: E731
    approach = at(hover_m)
    over = at(hover_m + DROP_M)
    vias: list[tuple[str, list[float]]] = []
    if fancy:
        # rise out of the picture pose and swing wide, then a flourish over the target
        mid = [(start_flange[i] + over[i]) / 2 for i in range(3)]
        dx, dy = over[0] - start_flange[0], over[1] - start_flange[1]
        d = math.hypot(dx, dy) or 1.0
        side = [-dy / d * min(0.08, 0.35 * d), dx / d * min(0.08, 0.35 * d)]
        swing = [mid[0] + side[0], mid[1] + side[1], max(start_flange[2], over[2]) + 0.10]
        swing_pose = swing + slerp_rotvec(start_flange, over, 0.5)
        vias.append(("swing", swing_pose))
        vias.append(("flourish", at(hover_m + DROP_M + 0.06, extra_yaw=35.0)))
    vias = [v for v in vias if v[0] not in skip]
    vias.append(("over", over))
    path = [list(start_flange)] + [p for _, p in vias] + [approach]
    legs = []
    for i, (name, pose) in enumerate(vias):
        legs.append(
            {
                "name": name,
                "pose": pose,
                "velocity": velocity,
                "acceleration": acceleration,
                "blend_m": _blend(path[i], path[i + 1], path[i + 2], 0.06 if fancy else 0.04),
            }
        )
    legs.append(
        {
            "name": "approach",
            "pose": approach,
            "velocity": 0.12,
            "acceleration": 0.6,
            "dwell_s": 0.3 if pick else 0.0,
        }
    )
    grasp = at(-grasp_below_m)
    lift = at(hover_m + lift_m)
    if pick:
        legs.append(
            {"name": "grasp", "pose": grasp, "velocity": 0.05, "acceleration": 0.3, "gripper": "close"}
        )
        legs.append({"name": "lift", "pose": lift, "velocity": 0.10, "acceleration": 0.5})
    opening = min(stroke_m, rect["minor_m"] * opening_factor)
    return {
        "legs": legs,
        "approach": approach,
        "grasp": grasp,
        "lift": lift,
        "yaw_deg": yaw,
        "opening_m": opening,
        "gripper_position": robotiq_position(opening, stroke_m),
        "fits": rect["minor_m"] * opening_factor <= stroke_m,
        "vias": [name for name, _ in vias],
    }


def slerp_rotvec(a: Sequence[float], b: Sequence[float], t: float) -> list[float]:
    """The rotation ``t`` of the way from pose ``a``'s orientation to ``b``'s (the
    shortest arc), as a rotation vector."""
    ta, tb = Transform.from_pose([0, 0, 0, *a[3:6]]), Transform.from_pose([0, 0, 0, *b[3:6]])
    rel = ta.inverse().compose(tb).to_pose()[3:]
    return ta.compose(Transform.from_pose([0, 0, 0, *(t * v for v in rel)])).to_pose()[3:]
