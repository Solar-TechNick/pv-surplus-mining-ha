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
