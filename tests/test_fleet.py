from pathlib import Path

import pytest

from custom_components.pv_surplus_mining.errors import ConfigError
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.fleet_states import load_fleet_states, validate_fleet_states
from custom_components.pv_surplus_mining.miner import MinerConfig, MinerController
from custom_components.pv_surplus_mining.models import FleetStateTarget, TunerState, CommandResult

FLEET_YAML = """
states:
  0:
    a: { action: sleep }
    b: { action: sleep }
  1:
    a: { action: active, power_w: 2000 }
    b: { action: sleep }
"""


def test_load_and_validate(tmp_path):
    p = tmp_path / "fleet-states.yaml"
    p.write_text(FLEET_YAML)
    states = load_fleet_states(p)
    assert set(states) == {0, 1}
    validate_fleet_states(states, {"a", "b"})


def test_validate_rejects_missing_miner(tmp_path):
    p = tmp_path / "fleet-states.yaml"
    p.write_text(FLEET_YAML)
    states = load_fleet_states(p)
    with pytest.raises(ConfigError):
        validate_fleet_states(states, {"a", "b", "c"})


class StubController(MinerController):
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.calls.append(("set", watt)); return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")
    async def curtail(self, action, wake_target_w=None):
        self.calls.append(("curtail", action)); return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")


def _ctrl(mid, prio):
    return StubController(MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000))


async def test_apply_state_in_merit_order():
    a, b = _ctrl("a", 1), _ctrl("b", 2)
    states = {0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")}}
    fc = FleetController({"a": a, "b": b}, states)
    results = await fc.apply_state(1)
    assert a.calls == [("set", 2000)] and b.calls == [("curtail", "sleep")]
    assert len(results) == 2


def test_fleetcontroller_rejects_state_missing_miner():
    a = _ctrl("a", 1)
    with pytest.raises(ConfigError):
        FleetController({"a": a}, {0: {}})
