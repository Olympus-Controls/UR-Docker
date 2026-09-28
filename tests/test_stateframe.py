"""The state-broadcast decoder (urctl.stateframe) and get_flange_pose's use of it.

A synthetic controller: robot-state messages built byte-for-byte the way the
Client Interface lays them out, with the Cartesian package computed from the
same joints and DH, so the decoder's flange, TCP pose and offset can be checked
against ground truth — and the self-check (flange ∘ offset = TCP) against a
controller that reports an offset its TCP pose was not computed with.
"""

from __future__ import annotations

import math
import random
import socket
import struct
import threading

import pytest

from urctl import Robot, RobotConfig, stateframe
from urctl.pose import pose_trans

# UR3e nominal DH (UR's published kinematics); "calibration" deltas on top below
UR3E = {
    "theta": [0.0] * 6,
    "a": [0.0, -0.24355, -0.2132, 0.0, 0.0, 0.0],
    "d": [0.15185, 0.0, 0.0, 0.13105, 0.08535, 0.0921],
    "alpha": [math.pi / 2, 0.0, 0.0, math.pi / 2, -math.pi / 2, 0.0],
}
CALIBRATED = {
    "theta": [1e-4, -2e-4, 3e-4, 0.0, -1e-4, 2e-4],
    "a": [4e-5, -0.24361, -0.21316, 1e-5, 0.0, 0.0],
    "d": [0.15187, 3e-5, -2e-5, 0.13101, 0.08539, 0.09208],
    "alpha": [math.pi / 2 + 1e-4, -1e-4, 2e-4, math.pi / 2, -math.pi / 2, 0.0],
}
JOINTS = [0.2, -1.1, 1.3, -1.8, -1.5708, 0.4]
HANDE = [0.0, 0.0, 0.163, 0.0, 0.0, 0.0]


def _pkg(ptype: int, body: bytes) -> bytes:
    return struct.pack(">iB", 5 + len(body), ptype) + body


def _msg(mtype: int, body: bytes) -> bytes:
    return struct.pack(">iB", 5 + len(body), mtype) + body


def joint_data(q) -> bytes:
    return b"".join(struct.pack(">3d4fB", v, v, 0.0, 0.1, 48.0, 30.0, 35.0, 253) for v in q)


def cartesian(tcp, offset=None) -> bytes:
    return struct.pack(">6d", *tcp) + (struct.pack(">6d", *offset) if offset is not None else b"")


def kinematics(dh) -> bytes:
    return (
        struct.pack(">6I", *range(6))
        + b"".join(struct.pack(">6d", *dh[k]) for k in ("theta", "a", "d", "alpha"))
        + struct.pack(">I", 2)
    )


def robot_state(q=JOINTS, dh=CALIBRATED, offset=HANDE, *, reported_offset=None, with_dh=True) -> bytes:
    """One robot-state message; the TCP pose is computed with ``offset`` and the
    Cartesian package reports ``reported_offset`` (default: the same one)."""
    tcp = pose_trans(stateframe.flange_from_joints(q, dh), offset)
    body = _pkg(0, b"\x00" * 42)  # ROBOT_MODE_DATA stand-in: skipped by length
    body += _pkg(stateframe.PKG_JOINT_DATA, joint_data(q))
    body += _pkg(
        stateframe.PKG_CARTESIAN_INFO, cartesian(tcp, offset if reported_offset is None else reported_offset)
    )
    if with_dh:
        body += _pkg(stateframe.PKG_KINEMATICS_INFO, kinematics(dh))
    return _msg(stateframe.MESSAGE_ROBOT_STATE, body)


def _close(a, b, tol):
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


# -- kinematics -----------------------------------------------------------------------------


def test_forward_kinematics_at_zero_is_the_stretched_ur3e():
    # UR's zero pose: the arm along base -X, the flange (a2+a3, -(d4+d6), d1-d5) pointing -Y
    f = stateframe.flange_from_joints([0.0] * 6, UR3E)
    assert _close(f[:3], [-0.45675, -0.22315, 0.0665], 1e-9)


def test_forward_kinematics_rejects_short_inputs():
    with pytest.raises(ValueError):
        stateframe.flange_from_joints([0.0] * 5, UR3E)


# -- decoding ----------------------------------------------------------------------------


def test_one_message_decodes_to_the_flange_the_joints_imply():
    msgs, tail = stateframe.iter_messages(robot_state())
    assert tail == b"" and len(msgs) == 1
    st = stateframe.flange_state(stateframe.parse_robot_state(msgs[0][1]))
    assert st["flange_source"] == "kinematics"
    assert _close(st["flange"], stateframe.flange_from_joints(JOINTS, CALIBRATED), 1e-12)
    assert _close(st["tcp_offset"], HANDE, 0) and _close(st["joints"], JOINTS, 0)
    assert st["consistency_m"] < 1e-9


def test_an_offset_the_tcp_pose_was_not_computed_with_is_caught():
    # the 09-23 failure: the controller reports 220 mm while its pose used the Hand-E's 163 mm
    wrong = [0.0, -0.035, 0.22, 0.0, 0.0, 0.0]
    st = stateframe.flange_state(
        stateframe.parse_robot_state(stateframe.iter_messages(robot_state(reported_offset=wrong))[0][0][1])
    )
    assert st["consistency_m"] > 0.05
    # …and the kinematic flange does not care which offset was reported
    assert _close(st["flange"], stateframe.flange_from_joints(JOINTS, CALIBRATED), 1e-12)


def test_without_the_kinematics_package_the_flange_comes_from_tcp_and_offset():
    st = stateframe.flange_state(
        stateframe.parse_robot_state(stateframe.iter_messages(robot_state(with_dh=False))[0][0][1])
    )
    assert st["flange_source"] == "tcp_offset"
    assert _close(st["flange"][:3], stateframe.flange_from_joints(JOINTS, CALIBRATED)[:3], 1e-9)


def test_a_pre_e_series_cartesian_package_has_no_offset():
    body = _pkg(stateframe.PKG_CARTESIAN_INFO, cartesian([0.1] * 6))
    assert stateframe.parse_robot_state(body) == {"tcp": [0.1] * 6, "tcp_offset": None}


def test_split_stream_reassembles_and_skips_other_message_types():
    stream = _msg(20, b"version URControl 5.25.1") + robot_state() + robot_state()[:17]
    msgs, tail = stateframe.iter_messages(stream)
    assert [m[0] for m in msgs] == [20, 16] and tail == robot_state()[:17]


@pytest.mark.parametrize("size", [-1, 0, 4, stateframe.MAX_MESSAGE_BYTES + 1])
def test_impossible_message_lengths_raise(size):
    with pytest.raises(stateframe.StateFrameError):
        stateframe.iter_messages(struct.pack(">iB", size, 16) + b"\x00" * 16)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.757388200993436e159])
def test_non_finite_and_absurd_values_are_refused(bad):
    # 1.76e159: what a flipped byte decoded to in the fuzz below — finite, and it
    # overflowed Rodrigues (math domain error) before the plausibility bound
    body = _pkg(stateframe.PKG_CARTESIAN_INFO, cartesian([0.0, bad, 0.0, 0.0, 0.0, 0.0], [0.0] * 6))
    with pytest.raises(stateframe.StateFrameError):
        stateframe.parse_robot_state(body)
    dh = {k: list(v) for k, v in CALIBRATED.items()}
    dh["d"][2] = bad
    with pytest.raises(stateframe.StateFrameError):
        stateframe.parse_robot_state(_pkg(stateframe.PKG_KINEMATICS_INFO, kinematics(dh)))


def test_garbage_only_ever_raises_stateframeerror():
    """Truncations, bit flips and random bytes: the decoder answers or raises its own
    error — never IndexError / struct.error from a length it trusted."""
    rng = random.Random(20260927)
    good = robot_state()
    samples = [good[:n] for n in range(len(good))]
    for _ in range(400):
        b = bytearray(good)
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(len(b))] = rng.randrange(256)
        samples.append(bytes(b))
    samples += [bytes(rng.randrange(256) for _ in range(rng.randint(0, 600))) for _ in range(300)]
    for s in samples:
        try:
            msgs, _ = stateframe.iter_messages(s)
            for mtype, payload in msgs:
                if mtype == stateframe.MESSAGE_ROBOT_STATE:
                    st = stateframe.parse_robot_state(payload)
                    if "joints" in st and "dh" in st:
                        stateframe.flange_state(st)
        except stateframe.StateFrameError:
            pass


# -- over a socket, through Robot.get_flange_pose -----------------------------------------


class FakeBroadcast:
    """A controller's Secondary port: a version message, then robot states in
    awkward chunks. Records whatever a client sends (it must send nothing)."""

    def __init__(self, messages: list[bytes], chunk: int = 7):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.messages, self.chunk = messages, chunk
        self.received = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.srv.accept()
        with conn:
            data = b"".join(self.messages)
            for i in range(0, len(data), self.chunk):
                conn.sendall(data[i : i + self.chunk])
            conn.settimeout(0.3)
            try:
                while chunk := conn.recv(4096):
                    self.received += chunk
            except OSError:
                pass

    def close(self):
        self.srv.close()


@pytest.mark.state_broadcast
def test_get_flange_pose_reads_the_broadcast_and_sends_nothing(monkeypatch):
    fake = FakeBroadcast([_msg(20, b"URControl 5.25.1"), robot_state()])
    try:
        robot = Robot(RobotConfig(host="127.0.0.1", secondary_port=fake.port))
        monkeypatch.setattr(robot.primary, "run_and_capture", lambda *a, **k: pytest.fail("sent a script"))
        fp = robot.get_flange_pose()
    finally:
        fake.thread.join(2)
        fake.close()
    assert fp["ok"] and fp["source"] == "state_broadcast" and fp["flange_source"] == "kinematics"
    assert _close(fp["flange"], stateframe.flange_from_joints(JOINTS, CALIBRATED), 1e-9)
    assert fp["tcp_offset_consistent"] is True
    assert fake.received == b""


@pytest.mark.state_broadcast
def test_get_flange_pose_flags_an_inconsistent_offset(monkeypatch):
    fake = FakeBroadcast([robot_state(reported_offset=[0.0, -0.035, 0.22, 0.0, 0.0, 0.0])])
    try:
        robot = Robot(RobotConfig(host="127.0.0.1", secondary_port=fake.port))
        monkeypatch.setattr(robot.primary, "run_and_capture", lambda *a, **k: pytest.fail("sent a script"))
        fp = robot.get_flange_pose()
    finally:
        fake.thread.join(2)
        fake.close()
    assert fp["ok"] and fp["tcp_offset_consistent"] is False


@pytest.mark.state_broadcast
def test_get_flange_pose_falls_back_to_textmsg_when_the_broadcast_is_absent(monkeypatch):
    srv = socket.create_server(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.close()  # nothing listens here now
    robot = Robot(RobotConfig(host="127.0.0.1", secondary_port=port))
    tcp, off = [0.4, 0.0, 0.3, 0.0, math.pi, 0.0], [0.0, 0.0, 0.1, 0.0, 0.0, 0.0]
    monkeypatch.setattr(
        robot.primary,
        "run_and_capture",
        lambda *a, **k: [f"urctl/flange/tcp=p{tcp}", f"urctl/flange/offset=p{off}"],
    )
    fp = robot.get_flange_pose()
    assert fp["ok"] and fp["source"] == "textmsg" and "refused" in fp["state_error"].lower()
