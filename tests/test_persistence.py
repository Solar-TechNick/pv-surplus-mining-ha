from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import DOMAIN
from custom_components.pv_surplus_mining.coordinator import PvSurplusCoordinator
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.miner import MinerConfig
from custom_components.pv_surplus_mining.models import ControlConfig, FleetStateTarget, MinerStatus, CommandResult


class StubCtrl:
    def __init__(self, mid, prio=1, min_power_w=1000, max_power_w=4000):
        self.cfg = MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio,
                               min_power_w=min_power_w, max_power_w=max_power_w)
        self.available = True
        self.paused = True
    async def get_status(self):
        return MinerStatus(miner_id=self.cfg.id, online=True, paused=self.paused, available=True)


def _coord(hass):
    a = StubCtrl("a")
    states = {0: {"a": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000)}}
    fleet = FleetController({"a": a}, states)
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=False)
    return PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.g", import_positive=True)


async def test_operator_store_roundtrip(hass):
    from custom_components.pv_surplus_mining.store import operator_store
    s = operator_store(hass, "abc")
    await s.async_save({"auto_enabled": True})
    assert (await operator_store(hass, "abc").async_load()) == {"auto_enabled": True}


async def test_collect_and_apply_operator_state(hass):
    c = _coord(hass)
    c.auto_enabled = True
    c.manual_state = 1
    c.miner_enabled["a"] = False
    snapshot = c._operator_state()

    c2 = _coord(hass)
    assert c2.auto_enabled is False          # fresh coordinator built with non-persisted config
    c2._apply_operator_state(snapshot)
    assert c2.auto_enabled is True
    assert c2.manual_state == 1
    assert c2.miner_enabled["a"] is False


def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True,
        "miners": [{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw",
                    "username": "root", "min_power_w": 800, "max_power_w": 6435,
                    "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    return entry


async def test_build_restores_persisted_auto_enabled(hass, hass_storage):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.store import STORE_VERSION
    entry = _entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}.operator"] = {
        "version": STORE_VERSION, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}.operator",
        "data": {"auto_enabled": True, "manual_state": 0},
    }
    coordinator = await async_build_coordinator(hass, entry)
    assert coordinator.auto_enabled is True   # restored, not enabled_default


async def test_build_defaults_on_for_fresh_install(hass, hass_storage):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    entry = _entry(hass)   # no store entry seeded
    coordinator = await async_build_coordinator(hass, entry)
    assert coordinator.auto_enabled is True   # enabled_default flipped to True
