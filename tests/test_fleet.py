from pathlib import Path

import pytest

from custom_components.pv_surplus_mining.errors import AdapterError, ConfigError
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
    def __init__(self, cfg, *, fail_set=False, tuner_power_w=None):
        self.cfg = cfg
        self.calls = []
        self._fail_set = fail_set
        self._tuner_power_w = tuner_power_w

    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.calls.append(("set", watt))
        if self._fail_set:
            raise AdapterError("injected failure")
        return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")

    async def curtail(self, action, wake_target_w=None):
        self.calls.append(("curtail", action))
        return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")

    async def get_tuner_state(self):
        return TunerState(power_target_w=self._tuner_power_w)


def _ctrl(mid, prio, **kwargs):
    return StubController(MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000), **kwargs)


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


# ---------------------------------------------------------------------------
# Task 4 additions
# ---------------------------------------------------------------------------

async def test_apply_state_error_does_not_abort_loop():
    """A failing set_power_target is caught; the lower-priority miner still runs."""
    a = _ctrl("a", 1, fail_set=True)   # priority 1 — processed first; will error
    b = _ctrl("b", 2)                  # priority 2 — must still receive curtail
    states = {
        0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
        1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")},
    }
    fc = FleetController({"a": a, "b": b}, states)
    results = await fc.apply_state(1)

    assert len(results) == 2
    a_result = next(r for r in results if r.miner_id == "a")
    assert a_result.result.startswith("error:")
    assert b.calls == [("curtail", "sleep")]


async def test_get_state_matches_correctly():
    """get_state maps observed tuner readings onto fleet state IDs."""
    states = {
        0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
        1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")},
    }

    # all miners at min_power_w (1000) → state 0
    a = _ctrl("a", 1, tuner_power_w=1000)
    b = _ctrl("b", 2, tuner_power_w=1000)
    fc = FleetController({"a": a, "b": b}, states)
    result = await fc.get_state()
    assert result["matched_state"] == 0

    # "a" at 2000, "b" at 1000 → state 1
    a2 = _ctrl("a", 1, tuner_power_w=2000)
    b2 = _ctrl("b", 2, tuner_power_w=1000)
    fc2 = FleetController({"a": a2, "b": b2}, states)
    result2 = await fc2.get_state()
    assert result2["matched_state"] == 1

    # "a" at off-matrix value → no match
    a3 = _ctrl("a", 1, tuner_power_w=1234)
    b3 = _ctrl("b", 2, tuner_power_w=1000)
    fc3 = FleetController({"a": a3, "b": b3}, states)
    result3 = await fc3.get_state()
    assert result3["matched_state"] is None


def test_validate_fleet_states_rejects_extra_miner(tmp_path):
    """validate_fleet_states raises ConfigError when the YAML has more miners than the fleet."""
    p = tmp_path / "fleet-states.yaml"
    p.write_text(FLEET_YAML)
    states = load_fleet_states(p)
    # fleet only has "a"; "b" is extra in every state
    with pytest.raises(ConfigError):
        validate_fleet_states(states, {"a"})


def test_load_fleet_states_missing_file(tmp_path):
    """load_fleet_states raises ConfigError when the path does not exist."""
    with pytest.raises(ConfigError):
        load_fleet_states(tmp_path / "does-not-exist.yaml")


def test_load_fleet_states_empty_states(tmp_path):
    """load_fleet_states raises ConfigError when the file defines no states."""
    p = tmp_path / "fleet-states.yaml"
    p.write_text("states:\n")   # present but empty mapping
    with pytest.raises(ConfigError):
        load_fleet_states(p)

    p2 = tmp_path / "fleet-states2.yaml"
    p2.write_text("{}")         # no states key at all
    with pytest.raises(ConfigError):
        load_fleet_states(p2)


def test_max_available_state_shrinks_when_miner_unavailable():
    a, b = _ctrl("a", 1), _ctrl("b", 2)
    states = {
        0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
        1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")},
        2: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="active", power_w=1500)},
    }
    fc = FleetController({"a": a, "b": b}, states)
    assert fc.max_available_state({"a", "b"}) == 2
    assert fc.max_available_state({"a"}) == 1     # b down -> can't reach state 2
    assert fc.max_available_state(set()) == 0
