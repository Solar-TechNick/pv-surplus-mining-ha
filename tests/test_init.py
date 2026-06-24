import aiohttp  # noqa: F401  (ensures aiohttp import path used by integration)
from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

FLEET_YAML = """
states:
  0:
    s21plus_01: { action: sleep }
  1:
    s21plus_01: { action: active, power_w: 2000 }
"""


def _entry_data(path):
    return {
        CONF_MINERS: [{
            "id": "s21plus_01", "model": "S21+", "ip": "10.0.0.5", "priority": 1,
            "min_power_w": 1400, "max_power_w": 4000, "password": "pw",
            "power_targets_w": {"normal": 3000},
        }],
        CONF_GRID_ENTITY: "sensor.grid_power",
        CONF_IMPORT_POSITIVE: True,
        CONF_FLEET_STATES_PATH: str(path),
    }


def _mock_miner(m):
    base = "http://10.0.0.5/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details", payload={"status": "online"}, repeat=True)
    m.get(f"{base}/miner/stats", payload={"power": {"approx": 1400}, "temp_max_c": 60}, repeat=True)
    m.get(f"{base}/performance/tuner-state", payload={"power_target": {"watt": 1400}}, repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)
    m.put(f"{base}/actions/pause", payload=True, repeat=True)
    m.put(f"{base}/actions/resume", payload=True, repeat=True)


async def test_setup_and_unload_entry(hass, tmp_path):
    fleet_file = tmp_path / "fleet-states.yaml"
    fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "500")  # mild import -> hold at 0
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data["current_state"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
