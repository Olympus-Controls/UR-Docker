"""The cockpit's bridge to a UR controller.

Every robot action goes through :func:`urctl.tools.call_tool` — the same
schema-validated, safety-enveloped, audited path the ``urctl`` CLI, the MCP
server and ``urctl gui`` use — so "send this point to the robot" from the
perception cockpit is one more tool caller, not a side door. Two calls:

* :meth:`RobotLink.locate` — read the flange pose (``flange_pose``) and
  turn the segmented object's camera-frame point into a base-frame point and
  an approach pose (:func:`perception.handeye.locate`). No motion.
* :meth:`RobotLink.move` — ``move_tcp`` to an absolute base-frame pose (the
  approach pose the operator just saw). Validated by the safety envelope;
  refused in a non-RUNNING state, over the speed caps, or outside reach.

Connection target is ``RobotConfig.from_env()`` (``UR_HOST`` etc.); nothing
connects until the first call. ``dry_run`` validates and audits without
sending, and ``flange_pose`` then returns a stand-in pose so the whole
flow can be exercised on the synthetic camera without a controller.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence

from urctl.config import RobotConfig
from urctl.pose import pose_inv, pose_trans
from urctl.robot import Robot
from urctl.tools import ToolError, call_tool

from .calibrate import CalibrationSession
from .handeye import (
    APPROACH_REFERENCES,
    DEFAULT_APPROACH_REFERENCE,
    DEFAULT_STANDOFF_M,
    ENV_APPROACH_REFERENCE,
    ENV_STANDOFF_M,
    HandEye,
    default_handeye_path,
    fingertip_tcp,
    locate,
    tip_m_from_env,
)

DEFAULT_APPROACH_VELOCITY = 0.1  # m/s — slow; this move follows a single click
DEFAULT_APPROACH_ACCELERATION = 0.3  # m/s^2
# A jog is one button press: cap the step so a mistyped unit can't send the arm
# across the cell (the safety envelope's own relative cap is 1.0 m).
MAX_JOG_STEP_M = 0.05
MAX_JOG_STEP_RAD = 0.35
DEFAULT_JOG_VELOCITY = 0.05
# The approach *cycle* (cockpit "Approach" button): over the object at
# clearance, down to the standoff, hold, back up, back to where the picture
# was taken. Quicker than the single Move — it's a repeatable test loop.
DEFAULT_CYCLE_VELOCITY = 0.15  # m/s
DEFAULT_CYCLE_ACCELERATION = 0.5  # m/s^2
DEFAULT_CYCLE_CLEARANCE_M = 0.10
DEFAULT_CYCLE_HOLD_S = 1.0
FLANGE_TCP = [0.0] * 6


def reach_note(loc: Mapping) -> str:
    """Why a located target is out of reach, in the words of whichever check judged it."""
    dist = loc.get("commanded_distance_m")
    where = f"approach {dist:.3f} m from base" if isinstance(dist, int | float) else "the approach"
    if loc.get("reach_check") == "controller_ik":
        return (
            f"the controller has no inverse-kinematics solution for {where} "
            "(raise the standoff, or move the part closer)"
        )
    reach = loc.get("max_reach_m")
    cap = f"reaches {reach:.2f} m" if isinstance(reach, int | float) else "cannot reach it"
    return f"{where}, {loc.get('model') or 'arm'} {cap}; move the part closer"


class RobotLink:
    def __init__(
        self,
        config: RobotConfig | None = None,
        *,
        dry_run: bool = False,
        handeye: HandEye | None = None,
        robot: Robot | None = None,
    ):
        self.config = config or RobotConfig.from_env()
        self.dry_run = dry_run
        self.robot = robot if robot is not None else Robot(self.config, dry_run=dry_run)
        self.handeye = handeye or HandEye.from_env()
        self.calibration = CalibrationSession(seed=self.handeye)
        self._extrinsics: Mapping | None = None
        # Cell-level approach defaults (the cell file sets them): what the
        # standoff is measured from and how big it is when a call doesn't say.
        self.approach_reference = (
            os.environ.get(ENV_APPROACH_REFERENCE) or DEFAULT_APPROACH_REFERENCE
        ).lower()
        if self.approach_reference not in APPROACH_REFERENCES:
            raise ValueError(f"{ENV_APPROACH_REFERENCE} must be one of {APPROACH_REFERENCES}")
        raw = os.environ.get(ENV_STANDOFF_M, "").strip()
        self.standoff_m = float(raw) if raw else DEFAULT_STANDOFF_M
        if not (0.0 <= self.standoff_m <= 1.0):
            raise ValueError(f"{ENV_STANDOFF_M} must be within 0..1 m")
        self.tip_m = tip_m_from_env()

    def reference_tcp(self, reference: str | None = None) -> list[float] | None:
        """The TCP every move of this approach reference runs with: the fingertips,
        the flange, or ``None`` (the controller's active TCP, ``"tcp"`` only)."""
        ref = (reference or self.approach_reference).lower()
        if ref == "fingertip":
            return fingertip_tcp(self.tip_m)
        if ref == "flange":
            return list(FLANGE_TCP)
        return None

    def attach_camera(self, camera_description: Mapping) -> None:
        """Take the depth→colour extrinsics from ``camera.describe()``."""
        self._extrinsics = camera_description.get("extrinsics_depth_to_color")
        self.handeye = self.handeye.with_extrinsics(self._extrinsics)
        self.calibration.extrinsics = self._extrinsics
        self.calibration.seed = self.handeye

    def describe(self) -> dict:
        return {
            "host": self.config.host,
            "platform": self.config.platform,
            "dry_run": self.dry_run,
            "handeye": self.handeye.as_dict(),
            "approach": {
                "standoff_m": self.standoff_m,
                "reference": self.approach_reference,
                "tip_m": self.tip_m,
                "tcp": self.reference_tcp(),
                "velocity": DEFAULT_APPROACH_VELOCITY,
                "acceleration": DEFAULT_APPROACH_ACCELERATION,
            },
        }

    def state(self) -> dict:
        return self._tool("get_state")

    def flange_pose(self) -> dict:
        return self._tool("flange_pose")

    def locate(
        self,
        point_cam: Sequence[float],
        *,
        standoff_m: float | None = None,
        reference: str | None = None,
    ) -> dict:
        """Camera point → base point + approach pose, using the live flange pose.
        ``standoff_m`` / ``reference`` default to the cell's (``PERCEPTION_STANDOFF_M``,
        ``PERCEPTION_APPROACH_REFERENCE``)."""
        fp = self.flange_pose()
        if not fp.get("ok") or not fp.get("flange") or not fp.get("tcp"):
            return {"ok": False, "error": fp.get("error") or "could not read the flange pose", "robot": fp}
        result = locate(
            self.handeye,
            fp["flange"],
            point_cam,
            tcp_pose=fp["tcp"],
            standoff_m=self.standoff_m if standoff_m is None else standoff_m,
            max_reach_m=self.robot.max_reach(),
            reference=(reference or self.approach_reference).lower(),
            tcp_offset=fp.get("tcp_offset"),
            tip_m=self.tip_m,
        )
        result["ok"] = True
        result["tcp"] = self.reference_tcp(result["reference"])
        self._controller_reach(result)
        result["model"] = self.robot.safety.model or None
        result["robot"] = {
            k: fp.get(k)
            for k in ("host", "dry_run", "tcp_offset", "flange_reported", "host_controller_mismatch_m", "ts")
        }
        return result

    def move(
        self,
        pose: Sequence[float],
        *,
        velocity: float = DEFAULT_APPROACH_VELOCITY,
        acceleration: float = DEFAULT_APPROACH_ACCELERATION,
        tcp: Sequence[float] | None = None,
    ) -> dict:
        """``movel`` to an absolute base-frame pose (safety-validated, audited).
        ``tcp`` overrides the active TCP for the move (``[0]*6`` = the flange);
        without one the cell's approach reference decides — the fingertip TCP by
        default — so a pose from :meth:`locate` lands where it was computed for.
        Only the ``"tcp"`` reference moves the controller's active TCP."""
        vals = [float(v) for v in pose]
        if len(vals) != 6 or not all(math.isfinite(v) for v in vals):
            raise ValueError("pose must be 6 finite numbers [x, y, z, rx, ry, rz]")
        if not (0.0 < velocity <= 1.0) or not (0.0 < acceleration <= 5.0):
            raise ValueError("velocity must be within (0, 1] m/s and acceleration within (0, 5] m/s^2")
        params = {
            "pose": vals,
            "relative": False,
            "velocity": float(velocity),
            "acceleration": float(acceleration),
        }
        if tcp is None:
            tcp = self.reference_tcp()
        if tcp is not None:
            t = [float(v) for v in tcp]
            if len(t) != 6 or not all(math.isfinite(v) for v in t):
                raise ValueError("tcp must be 6 finite numbers [x, y, z, rx, ry, rz]")
            params["tcp"] = t
        return self._tool("move_tcp", params)

    def move_relative(
        self,
        delta: Sequence[float],
        *,
        velocity: float = DEFAULT_APPROACH_VELOCITY,
        acceleration: float = DEFAULT_APPROACH_ACCELERATION,
    ) -> dict:
        """One relative base-frame ``movel`` (``[dx, dy, dz, drx, dry, drz]``) —
        a sweep. Bigger than a jog, still capped by the envelope's
        relative-step limit; blocks until the move confirms."""
        vals = [float(v) for v in delta]
        if len(vals) != 6 or not all(math.isfinite(v) for v in vals):
            raise ValueError("delta must be 6 finite numbers [dx, dy, dz, drx, dry, drz]")
        if not (0.0 < velocity <= 1.0) or not (0.0 < acceleration <= 5.0):
            raise ValueError("velocity must be within (0, 1] m/s and acceleration within (0, 5] m/s^2")
        return self._tool(
            "move_tcp",
            {
                "pose": vals,
                "relative": True,
                "velocity": float(velocity),
                "acceleration": float(acceleration),
            },
        )

    def approach_point_base(
        self,
        point_base: Sequence[float],
        *,
        along: Sequence[float] = (0.0, 0.0, 1.0),
        standoff_m: float | None = None,
        reference: str | None = None,
    ) -> dict:
        """An approach pose ``standoff_m`` from a **base-frame** point along
        ``along`` (a surface normal), tool orientation
        unchanged — the same output shape as :meth:`locate` (``approach_pose``,
        ``flange_target_pose``, ``reachable``) so Move/Approach can reuse it.
        Reads the flange pose; moves nothing."""

        pt = [float(v) for v in point_base]
        if len(pt) != 3 or not all(math.isfinite(v) for v in pt):
            raise ValueError("point_base must be [x, y, z]")
        n = [float(v) for v in along]
        norm = math.sqrt(sum(v * v for v in n))
        if len(n) != 3 or norm < 1e-9:
            raise ValueError("along must be a non-zero direction")
        n = [v / norm for v in n]
        so = self.standoff_m if standoff_m is None else float(standoff_m)
        if not (0.0 <= so <= 1.0):
            raise ValueError("standoff_m must be within 0..1 m")
        ref = (reference or self.approach_reference).lower()
        if ref not in APPROACH_REFERENCES:
            raise ValueError(f"reference must be one of {APPROACH_REFERENCES}")
        fp = self.flange_pose()
        if not fp.get("ok") or not fp.get("flange") or not fp.get("tcp"):
            return {"ok": False, "error": fp.get("error") or "could not read the flange pose", "robot": fp}
        offset = fp.get("tcp_offset")
        if ref == "flange" and not offset:
            return {"ok": False, "error": "reference='flange' needs the active tcp_offset"}
        short = [pt[i] + so * n[i] for i in range(3)]
        flange_now, tcp_now = [float(v) for v in fp["flange"]], [float(v) for v in fp["tcp"]]
        if ref == "fingertip":
            target = [*short, *flange_now[3:]]  # the fingertip TCP's pose
            flange_target = pose_trans(target, pose_inv(fingertip_tcp(self.tip_m)))
        elif ref == "flange":
            flange_target = [*short, *flange_now[3:]]
            target = pose_trans(flange_target, offset)
        else:
            target = [*short, *tcp_now[3:]]
            flange_target = pose_trans(target, pose_inv(offset)) if offset else None
        commanded = flange_target if ref in ("flange", "fingertip") else target
        dist = math.sqrt(sum(v * v for v in commanded[:3]))
        max_reach = self.robot.max_reach()
        result = {
            "ok": True,
            "point_base_m": pt,
            "along": n,
            "standoff_m": so,
            "reference": ref,
            "approach_pose": target,
            "flange_target_pose": flange_target,
            "tcp": self.reference_tcp(ref),
            "tool_tcp": fingertip_tcp(self.tip_m) if ref == "fingertip" else None,
            "tip_m": self.tip_m if ref == "fingertip" else None,
            "commanded_distance_m": dist,
            "max_reach_m": max_reach,
            "reachable": None if max_reach is None else bool(dist <= max_reach),
            "model": self.robot.safety.model or None,
            "flange_pose": flange_now,
            "tcp_pose": tcp_now,
            "tcp_offset": offset,
            "robot": {"tcp_offset": offset, "dry_run": fp.get("dry_run")},
            "handeye": self.handeye.as_dict(),
            "point_cam_m": None,
            "point_flange_m": None,
            "view_ray_base": None,
        }
        self._controller_reach(result)
        return result

    def _controller_reach(self, result: dict) -> None:
        """Replace the datasheet-sphere ``reachable`` with the controller's own
        inverse kinematics for the pose that will be commanded (the flange target
        with the TCP overridden to the flange, or the approach pose under the active
        TCP). The sphere stays when the controller gives no answer."""
        ref = result.get("reference")
        pose = result.get("flange_target_pose") if ref == "flange" else result.get("approach_pose")
        answer = {"reachable": None, "joints": None}
        if pose:
            answer = self.robot.inverse_kin([list(pose)], tcp=self.reference_tcp(ref))[0]
        # the controller's own joint solution (nearest the live joints) — what a
        # joint-space move screen (PolyScope 5's requestUserToMoveRobot) is handed
        result["joint_target"] = answer["joints"]
        if answer["reachable"] is None:
            result["reach_check"] = "sphere"
        else:
            result["reachable"] = answer["reachable"]
            result["reach_check"] = "controller_ik"

    def tcp_offset(self) -> list[float] | None:
        """The active flange→TCP offset (UR pose) from one ``flange_pose`` read."""
        fp = self.flange_pose()
        off = fp.get("tcp_offset") if fp.get("ok") else None
        return [float(v) for v in off] if off else None

    def approach_cycle(
        self,
        point_cam: Sequence[float],
        *,
        standoff_m: float | None = None,
        reference: str | None = None,
        clearance_m: float = DEFAULT_CYCLE_CLEARANCE_M,
        hold_s: float = DEFAULT_CYCLE_HOLD_S,
        velocity: float = DEFAULT_CYCLE_VELOCITY,
        acceleration: float = DEFAULT_CYCLE_ACCELERATION,
    ) -> dict:
        """Locate the clicked point, then run one program: over the object at
        ``clearance_m`` above the approach, down to the approach, dwell
        ``hold_s``, back up, back to the capture pose (where the arm is now).
        With the ``flange`` reference every leg runs with the TCP overridden to
        the flange, so the pendant's active TCP can't skew it. Refused (nothing
        sent) when the target is out of reach or the envelope rejects a leg.
        """
        if not (0.0 <= clearance_m <= 0.5):
            raise ValueError("clearance_m must be within 0..0.5 m")
        if not (0.0 <= hold_s <= 60.0):
            raise ValueError("hold_s must be within 0..60 s")
        if not (0.0 < velocity <= 1.0) or not (0.0 < acceleration <= 5.0):
            raise ValueError("velocity must be within (0, 1] m/s and acceleration within (0, 5] m/s^2")
        loc = self.locate(point_cam, standoff_m=standoff_m, reference=reference)
        if not loc.get("ok"):
            return {"ok": False, "error": loc.get("error", "locate failed"), "locate": loc}
        if loc.get("reachable") is False:
            return {
                "ok": False,
                "error": "out of reach: " + reach_note(loc),
                "locate": loc,
            }
        if loc["reference"] == "fingertip":
            tcp = self.reference_tcp("fingertip")
            capture, target = pose_trans(loc["flange_pose"], tcp), loc["approach_pose"]
        elif loc["reference"] == "flange":
            tcp, capture, target = FLANGE_TCP, loc["flange_pose"], loc["flange_target_pose"]
        else:
            tcp, capture, target = None, loc["tcp_pose"], loc["approach_pose"]
        over = [target[0], target[1], target[2] + clearance_m, *target[3:]]
        legs = [
            {"pose": over, "velocity": velocity, "acceleration": acceleration},
            {"pose": target, "velocity": velocity, "acceleration": acceleration, "dwell_s": hold_s},
            {"pose": over, "velocity": velocity, "acceleration": acceleration},
            {"pose": list(capture), "velocity": velocity, "acceleration": acceleration},
        ]
        params: dict = {"legs": legs}
        if tcp is not None:
            params["tcp"] = tcp
        run = self._tool("move_tcp_path", params)
        out = dict(run)
        if not out.get("ok") and not out.get("error"):
            viol = (out.get("safety") or {}).get("violations")
            if viol:
                leg = (out.get("safety") or {}).get("leg")
                out["error"] = f"refused by the safety envelope on leg {leg}: " + "; ".join(
                    f"{v['rule']}: {v['detail']}" for v in viol
                )
            elif out.get("protective_stop"):
                out["error"] = "protective stop during the cycle"
            else:
                out["error"] = f"cycle stopped after {out.get('completed_legs', 0)} of 4 legs"
        out["locate"] = loc
        out["cycle"] = {
            "reference": loc["reference"],
            "standoff_m": loc["standoff_m"],
            "clearance_m": clearance_m,
            "hold_s": hold_s,
            "velocity": velocity,
            "tcp": tcp,
            "capture_pose": list(capture),
            "target_pose": list(target),
            "over_pose": over,
        }
        return out

    # -- hand-eye calibration (touch-and-click; perception.calibrate) --------------

    def cal_record_mark(self) -> dict:
        """The live TCP position is the mark in the base frame (the tip is on it)."""
        fp = self.flange_pose()
        if not fp.get("ok") or not fp.get("tcp"):
            return {"ok": False, "error": fp.get("error") or "could not read the TCP pose", "robot": fp}
        out = self.calibration.record_mark(fp["tcp"][:3])
        out["ok"] = True
        return out

    def cal_add_view(self, point_cam: Sequence[float], *, pixel=None, seq=None) -> dict:
        fp = self.flange_pose()
        if not fp.get("ok") or not fp.get("flange"):
            return {"ok": False, "error": fp.get("error") or "could not read the flange pose", "robot": fp}
        out = self.calibration.add_view(fp["flange"], point_cam, pixel=pixel, seq=seq)
        out["ok"] = True
        return out

    def cal_solve(self) -> dict:
        return self.calibration.solve()

    def cal_apply(self, *, save: bool = True, path: str | None = None, force: bool = False) -> dict:
        """Use the solved transform from now on (and write it so the next start
        picks it up: env > file > bracket seed). A solve that carries warnings
        (poor rotation diversity, high residual) is refused unless ``force``."""
        result = self.calibration.result
        if result and result.get("warnings") and not force:
            return {
                "ok": False,
                "error": "solve has warnings; fix them or pass force=true: " + "; ".join(result["warnings"]),
                "warnings": result["warnings"],
            }
        self.handeye = self.calibration.handeye()
        out: dict = {"ok": True, "handeye": self.handeye.as_dict()}
        if save:
            saved = self.calibration.save(path or default_handeye_path())
            out["saved"] = str(saved)
        return out

    def cal_status(self) -> dict:
        d = self.calibration.as_dict()
        d["ok"] = True
        d["active_handeye"] = self.handeye.as_dict()
        return d

    # -- pilot actions (each one tool call; all safety-enveloped + audited) ----

    def jog(self, delta: Sequence[float], *, velocity: float = DEFAULT_JOG_VELOCITY) -> dict:
        """One relative base-frame nudge: ``[dx, dy, dz, drx, dry, drz]``,
        each translation ≤ :data:`MAX_JOG_STEP_M`, each rotation ≤ :data:`MAX_JOG_STEP_RAD`."""
        vals = [float(v) for v in delta]
        if len(vals) != 6 or not all(math.isfinite(v) for v in vals):
            raise ValueError("delta must be 6 finite numbers [dx, dy, dz, drx, dry, drz]")
        if any(abs(v) > MAX_JOG_STEP_M for v in vals[:3]):
            raise ValueError(f"a jog moves at most {MAX_JOG_STEP_M} m per axis (got {vals[:3]})")
        if any(abs(v) > MAX_JOG_STEP_RAD for v in vals[3:]):
            raise ValueError(f"a jog rotates at most {MAX_JOG_STEP_RAD} rad per axis (got {vals[3:]})")
        if not any(vals):
            raise ValueError("zero jog")
        if not (0.0 < velocity <= 0.25):
            raise ValueError("jog velocity must be within (0, 0.25] m/s")
        return self._tool(
            "move_tcp",
            {"pose": vals, "relative": True, "velocity": float(velocity), "acceleration": 0.3},
        )

    def bring_up(self) -> dict:
        return self._tool("bring_up")

    def stop(self) -> dict:
        return self._tool("stop")

    def freedrive(self, enable: bool) -> dict:
        return self._tool("freedrive", {"enable": bool(enable)})

    def gripper(self, action: str, position: int | None = None) -> dict:
        params: dict = {"action": action}
        if position is not None:
            params["position"] = int(position)
        return self._tool("gripper", params)

    def rtde_state(self, deep: bool = False) -> dict:
        return self._tool("rtde_state", {"deep": bool(deep)})

    def _tool(self, name: str, params: dict | None = None) -> dict:
        try:
            return call_tool(self.robot, name, params)
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except OSError as exc:
            return {"ok": False, "error": f"robot unreachable at {self.config.host}: {exc}"}
