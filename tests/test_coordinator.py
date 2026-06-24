import pytest

from custom_components.pv_surplus_mining.coordinator import PvSurplusCoordinator
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.miner import MinerConfig
from custom_components.pv_surplus_mining.models import (
    CommandResult, ControlConfig, FleetStateTarget, MinerStatus,
)


class StubCtrl:
    def __init__(self, mid, prio, online=True):
        self.cfg = MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000)
        self.available = True
        self._online = online
        self.applied = []
    async def get_status(self):
        return MinerStatus(miner_id=self.cfg.id, online=self._online, temp_max_c=60.0, available=self.available)
    async def get_tuner_state(self):
        from custom_components.pv_surplus_mining.models import TunerState
        return TunerState(power_target_w=self.cfg.min_power_w)
    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.applied.append(watt); return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")
    async def curtail(self, action, wake_target_w=None):
        self.applied.append(("curtail", action)); return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")


def _fleet():
    a = StubCtrl("a", 1)
    states = {0: {"a": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000)}}
    return FleetController({"a": a}, states), a


def _coord(hass, cfg=None):
    fleet, a = _fleet()
    cfg = cfg or ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True)
    c = PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.grid_power", import_positive=True)
    return c, a


async def test_invalid_grid_holds_at_zero(hass):
    c, a = _coord(hass)
    hass.states.async_set("sensor.grid_power", "unknown")
    data = await c._async_update_data()
    assert data["current_state"] == 0
    assert a.applied == []   # nothing changed -> no dispatch


async def test_emergency_stop_applies_state_zero(hass):
    c, a = _coord(hass)
    c.loop.current_state = 1   # pretend the fleet is running at state 1
    c.emergency_stop = True
    hass.states.async_set("sensor.grid_power", "-3000")
    data = await c._async_update_data()
    assert data["emergency"] is True and data["target_state"] == 0
    assert a.applied[-1] == ("curtail", "sleep")


async def test_recovery_clears_unavailable_latch(hass):
    """If a miner was latched unavailable but get_status returns online=True,
    the coordinator should clear the latch and restore max_available_state."""
    fleet, a = _fleet()
    # Start with the miner latched as unavailable (e.g. after N write failures)
    a.available = False
    a.failure_count = 3

    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True)
    c = PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.grid_power", import_positive=True)
    hass.states.async_set("sensor.grid_power", "-3000")

    data = await c._async_update_data()

    # The recovery pass should have cleared the latch
    assert a.available is True
    assert a.failure_count == 0
    # max_available_state should now reflect the re-admitted miner (state 1 is reachable)
    assert data["max_available_state"] == 1


# ── Normal-mode tests ─────────────────────────────────────────────────────────

async def test_normal_mode_targets_top_state(hass):
    """With normal_mode=True and a grid value that would otherwise hold (mild export),
    the decision should target the top reachable state (fleet.max_state = 1)."""
    c, a = _coord(hass)
    c.normal_mode = True
    # Mild export that would normally hold at 0 (below step-up threshold)
    hass.states.async_set("sensor.grid_power", "-200")
    data = await c._async_update_data()
    # Normal mode forces manual_override to max_state (1); decision should target 1
    assert data["target_state"] == 1


async def test_normal_mode_emergency_stop_takes_precedence(hass):
    """With normal_mode=True AND emergency_stop=True, emergency wins and state goes to 0."""
    c, a = _coord(hass)
    c.normal_mode = True
    c.emergency_stop = True
    c.loop.current_state = 1   # pretend fleet is running
    hass.states.async_set("sensor.grid_power", "-200")
    data = await c._async_update_data()
    assert data["emergency"] is True and data["target_state"] == 0


async def test_build_coordinator_generates_matrix_from_options(hass):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.const import DOMAIN
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True,
        "miners": [
            {"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
             "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1},
            {"id": "b", "name": "B", "model": "m", "ip": "10.0.0.2", "password": "pw", "username": "root",
             "min_power_w": 2400, "max_power_w": 6435, "default_power_w": 3800, "command_cooldown_sec": 120, "priority": 2},
        ],
    })
    entry.add_to_hass(hass)
    coordinator = await async_build_coordinator(hass, entry)   # no network at build time
    assert set(coordinator.fleet.miners) == {"a", "b"}
    assert 0 in coordinator.fleet.states and coordinator.fleet.max_state >= 1
    assert all(t.action == "sleep" for t in coordinator.fleet.states[0].values())


async def test_build_coordinator_uses_custom_file_override(hass, tmp_path):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.const import DOMAIN
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    f = tmp_path / "fleet-states.yaml"
    f.write_text("states:\n  0:\n    a: { action: sleep }\n  1:\n    a: { action: active, power_w: 1000 }\n")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True, "fleet_states_path": str(f),
        "miners": [{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
                    "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    coordinator = await async_build_coordinator(hass, entry)
    assert sorted(coordinator.fleet.states) == [0, 1]
    assert coordinator.fleet.states[1]["a"].power_w == 1000
