import pytest

from custom_components.pv_surplus_mining.coordinator import PvSurplusCoordinator
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.miner import MinerConfig
from custom_components.pv_surplus_mining.models import (
    CommandResult, ControlConfig, FleetStateTarget, MinerStatus,
)


class StubCtrl:
    def __init__(self, mid, prio, online=True, paused=False):
        self.cfg = MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000)
        self.available = True
        self._online = online
        self.paused = paused
        self.applied = []
    async def get_status(self):
        return MinerStatus(miner_id=self.cfg.id, online=self._online, paused=self.paused, temp_max_c=60.0, available=self.available)
    async def get_tuner_state(self):
        from custom_components.pv_surplus_mining.models import TunerState
        return TunerState(power_target_w=self.cfg.min_power_w)
    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.applied.append(watt); self.paused = False
        return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")
    async def curtail(self, action, wake_target_w=None):
        self.applied.append(("curtail", action))
        if action == "sleep":
            self.paused = True
        return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")
    async def pause(self):
        self.applied.append(("pause",)); self.paused = True
        return CommandResult(miner_id=self.cfg.id, action="pause", target_w=0, changed=True, verified=True, result="ok")


def _fleet(paused=True):
    # Default paused=True: a fresh fleet sits at state 0 (all sleep), so the miner
    # is in sync with state 0 and self-heal does not re-command it.
    a = StubCtrl("a", 1, paused=paused)
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

async def test_normal_mode_runs_per_miner_power(hass):
    """24/7 (Normal) mode applies each enabled miner's per-miner power, independent
    of surplus (stub default power = max 4000)."""
    c, a = _coord(hass)
    c.normal_mode = True
    hass.states.async_set("sensor.grid_power", "-200")
    data = await c._async_update_data()
    assert data["reason"] == "24/7 per-miner mode"
    assert a.applied and a.applied[-1] == 4000


async def test_disabled_miner_is_force_paused(hass):
    """A disabled miner is force-paused even when observe-only (explicit operator action)."""
    c, a = _coord(hass, ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=False))
    c.miner_enabled["a"] = False
    a.paused = False                    # currently running
    hass.states.async_set("sensor.grid_power", "0")
    await c._async_update_data()
    assert a.paused is True             # force-paused despite observe-only
    assert ("pause",) in a.applied


async def test_disable_excludes_miner_from_matrix(hass):
    """Disabling a miner regenerates the matrix with that miner pinned to sleep
    everywhere, while the remaining miner still ramps."""
    a = StubCtrl("a", 1); b = StubCtrl("b", 2)
    states = {
        0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
        1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")},
        2: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="active", power_w=2500)},
    }
    fleet = FleetController({"a": a, "b": b}, states)
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True, step_up_export_threshold_w=700)
    c = PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.grid_power", import_positive=True)
    c.miner_enabled["b"] = False
    c._rebuild_fleet_states()
    assert all(c.fleet.states[s]["b"].action == "sleep" for s in c.fleet.states)
    assert any(c.fleet.states[s]["a"].action == "active" for s in c.fleet.states)


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


async def test_not_engaged_never_commands_miners(hass):
    # automation off, normal off, emergency-stop off -> observe-only
    c, a = _coord(hass, ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=False,
                                      emergency_import_threshold_w=1200, emergency_required_duration_s=0))
    c.loop.current_state = 1  # pretend miners are running
    hass.states.async_set("sensor.grid_power", "5000")  # big import that WOULD emergency-pause if engaged
    data = await c._async_update_data()
    assert a.applied == []                 # hands-off: miners are never commanded
    assert data["emergency"] is False
    assert "observe-only" in data["reason"]


async def test_reapplies_when_active_miner_is_paused(hass):
    """Self-heal: the loop is already AT the target state (no change this tick), but a
    miner that should be running is actually paused. The coordinator must re-apply the
    state to wake it, instead of believing it already ramped up."""
    c, a = _coord(hass)                 # enabled_default=True; _fleet miner starts paused
    c.loop.current_state = 1            # already at state 1 -> decision holds (no change)
    a.paused = True                     # but the miner is actually paused (desync)
    hass.states.async_set("sensor.grid_power", "0")  # neutral -> loop holds at 1
    data = await c._async_update_data()
    assert data["target_state"] == 1                 # no state change
    assert a.applied and a.applied[-1] == 2000       # re-applied state-1 active target
    assert a.paused is False                         # miner resumed


async def test_in_sync_state_not_recommanded(hass):
    """When reality already matches the held state, no redundant command is issued."""
    c, a = _coord(hass)
    c.loop.current_state = 1
    a.paused = False                    # running, in sync with active state 1
    hass.states.async_set("sensor.grid_power", "0")
    await c._async_update_data()
    assert a.applied == []              # idempotent: nothing re-commanded


async def test_manual_override_engages_and_commands(hass):
    """Manual override alone (automation off) must engage the controller and apply
    the manual state — not sit observe-only."""
    c, a = _coord(hass, ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=False))
    c.manual_override = True
    c.manual_state = 1                  # run miner "a" at state 1 (active@2000)
    a.paused = True                     # currently paused
    hass.states.async_set("sensor.grid_power", "0")
    data = await c._async_update_data()
    assert data["target_state"] == 1                 # manual state applied, not observe-only
    assert a.applied and a.applied[-1] == 2000        # miner commanded/resumed
    assert "observe-only" not in (data["reason"] or "")


async def test_engaged_via_emergency_switch_still_commands(hass):
    c, a = _coord(hass, ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=False))
    c.loop.current_state = 1
    c.emergency_stop = True                # explicit engagement
    hass.states.async_set("sensor.grid_power", "0")
    await c._async_update_data()
    assert a.applied and a.applied[-1] == ("curtail", "sleep")   # forced to state 0


async def test_simulate_grid_overrides_real_sensor(hass):
    c, a = _coord(hass)  # enabled_default=True
    hass.states.async_set("sensor.grid_power", "5000")  # real sensor says import
    c.simulate_grid = True
    c.simulated_grid_w = -3000  # simulate 3 kW surplus/export
    data = await c._async_update_data()
    assert data["grid_w"] == -3000   # the simulated value drives the loop, not the sensor


# ── PV-production mode tests ───────────────────────────────────────────────────

async def test_pv_mode_steps_up_on_pv_production(hass):
    """In PV mode, available PV (exceeding current fleet draw) steps the fleet up,
    even while the real grid is importing (house load is ignored)."""
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True,
                        step_up_export_threshold_w=700, step_up_required_duration_s=5, min_state_dwell_s=0)
    c, a = _coord(hass, cfg)
    c.pv_entity = "sensor.pv"; c.pv_mode = True
    hass.states.async_set("sensor.grid_power", "5000")  # real grid importing -> ignored in PV mode
    hass.states.async_set("sensor.pv", "3000")          # 3 kW PV production
    data = await c._async_update_data()
    assert data["control_mode"] == "pv_production"
    assert data["pv_w"] == 3000.0
    assert data["target_state"] == 1          # PV headroom -> step up
    assert a.applied[-1] == 2000              # state 1 sets power_w=2000


async def test_pv_mode_steps_down_when_miners_exceed_pv(hass):
    """In PV mode, when the fleet draws more than PV produces, it ramps down —
    regardless of the real grid (which here is exporting)."""
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True,
                        step_down_import_threshold_w=250, step_down_required_duration_s=5)
    c, a = _coord(hass, cfg)
    c.loop.current_state = 1                  # fleet running at 2000 W
    c.pv_entity = "sensor.pv"; c.pv_mode = True
    hass.states.async_set("sensor.grid_power", "-9999")  # real grid exporting -> ignored in PV mode
    hass.states.async_set("sensor.pv", "500")            # PV below fleet draw
    data = await c._async_update_data()
    assert data["control_mode"] == "pv_production"
    assert data["target_state"] == 0          # miners exceed PV -> ramp down
    assert a.applied[-1] == ("curtail", "sleep")


async def test_pv_mode_unknown_pv_holds(hass):
    """PV reading unavailable -> neutral sample -> hold, never increase."""
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True,
                        step_up_required_duration_s=5, min_state_dwell_s=0)
    c, a = _coord(hass, cfg)
    c.loop.current_state = 1
    a.paused = False                          # already running at state 1 (in sync)
    c.pv_entity = "sensor.pv"; c.pv_mode = True
    hass.states.async_set("sensor.pv", "unavailable")
    data = await c._async_update_data()
    assert data["pv_w"] is None
    assert data["target_state"] == 1          # held, not increased
    assert a.applied == []


async def test_surplus_mode_ignores_pv(hass):
    """With PV mode off (default), the grid drives the loop and PV is display-only."""
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True,
                        step_up_export_threshold_w=700, step_up_required_duration_s=5, min_state_dwell_s=0)
    c, a = _coord(hass, cfg)
    c.pv_entity = "sensor.pv"                  # pv_mode stays False
    hass.states.async_set("sensor.grid_power", "100")   # mild import -> holds (no step up, no emergency)
    hass.states.async_set("sensor.pv", "9000")          # plenty of PV, but ignored in surplus mode
    data = await c._async_update_data()
    assert data["control_mode"] == "surplus"
    assert data["pv_w"] == 9000.0             # still read for display
    assert data["target_state"] == 0
    assert a.applied == []
