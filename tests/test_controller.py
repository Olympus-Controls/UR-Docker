"""The vendor-neutral seams: Robot satisfies the Controller protocol, the Robotiq
adapter satisfies Gripper and maps onto Robot.gripper, and the tool registry's
canonical names (no vendor prefix) with the ur_ aliases resolve everywhere."""

from __future__ import annotations

import pytest

from tests.test_urctl import FakeController
from urctl import Controller, Gripper, RobotiqUrcapGripper
from urctl.config import RobotConfig
from urctl.robot import Robot
from urctl.tools import (
    ALIAS_PREFIX,
    TOOL_ALIASES,
    TOOLS,
    ToolError,
    call_tool,
    canonical_name,
    get_tool_schemas,
)


@pytest.fixture
def robot(monkeypatch):
    fake = FakeController().install(monkeypatch)
    fake.remote = True
    fake.gripper_object = 120
    return fake, Robot(RobotConfig(host="fake-ur.invalid"))


def test_robot_is_a_controller_and_its_gripper_a_gripper(robot):
    fake, r = robot
    assert isinstance(r, Controller)
    assert isinstance(r.gripper_device, Gripper)
    assert isinstance(RobotiqUrcapGripper(r), Gripper)


def test_a_non_controller_is_rejected_by_the_protocol_check():
    class Half:
        def get_state(self):
            return {}

    assert not isinstance(Half(), Controller)
    assert not isinstance(object(), Gripper)


def test_robotiq_adapter_drives_the_daemon_through_robot_gripper(robot):
    fake, r = robot
    g = r.gripper_device
    st = g.status()
    assert st["ok"] and st["status"]["STA"] == 3
    closed = g.close()
    assert closed["ok"] and closed["object_detected"] is True and closed["status"]["POS"] == 120
    opened = g.open()
    assert opened["ok"] and opened["object_detected"] is False and opened["status"]["POS"] == 0
    moved = g.move(60)
    assert moved["ok"] and moved["status"]["POS"] == 60
    assert any("SET POS 060" in s or "SET POS 60" in s for s in fake.gripper_sends)
    assert g.activate()["ok"]
    # speed/force defaults ride along with every motion
    custom = RobotiqUrcapGripper(r, speed=100, force=30)
    custom.close()
    assert any("SET SPE 100" in s and "SET FOR 030" in s for s in fake.gripper_sends[-1:]) or any(
        "SET SPE 100" in s and "SET FOR 30" in s for s in fake.gripper_sends[-1:]
    )


def test_canonical_tool_names_carry_no_vendor_prefix():
    names = [t.name for t in TOOLS]
    assert names and not any(n.startswith(ALIAS_PREFIX) for n in names)
    assert {"get_state", "move_tcp", "move_joints", "gripper", "flange_pose", "rtde_state"} <= set(names)
    schemas = get_tool_schemas()
    assert [s["name"] for s in schemas] == names


def test_every_alias_maps_to_one_canonical_tool():
    names = {t.name for t in TOOLS}
    assert set(TOOL_ALIASES.values()) == names and len(TOOL_ALIASES) == len(names)
    for alias, canon in TOOL_ALIASES.items():
        assert alias == ALIAS_PREFIX + canon and canonical_name(alias) == canon
    assert canonical_name("move_tcp") == "move_tcp" and canonical_name("nope") == "nope"
    with_aliases = get_tool_schemas(include_aliases=True)
    assert len(with_aliases) == 2 * len(names)
    assert any(
        s["name"] == "ur_move_tcp" and s["description"].startswith("Alias of move_tcp") for s in with_aliases
    )


def test_call_tool_accepts_both_names_and_names_the_prefix_on_unknown(robot):
    fake, r = robot
    a = call_tool(r, "get_state")
    b = call_tool(r, "ur_get_state")
    assert a["ok"] and b["ok"] and a["robot_mode"] == b["robot_mode"]
    with pytest.raises(ToolError, match="ur_"):
        call_tool(r, "ur_nope")
