import pathlib
import yaml
from aioresponses import aioresponses
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

DASH = pathlib.Path("dashboards/pv-surplus-mining.yaml")


def _collect(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("entity"), str):
            out.add(node["entity"])
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out)


def _mock_miner(m, ip="10.0.0.5"):
    base = f"http://{ip}/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details", payload={"status": "online"}, repeat=True)
    m.get(f"{base}/miner/stats", payload={"power": {"approx": 1400}, "temp_max_c": 60}, repeat=True)
    m.get(f"{base}/performance/tuner-state", payload={"power_target": {"watt": 1400}}, repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)
    m.put(f"{base}/actions/pause", payload=True, repeat=True)
    m.put(f"{base}/actions/resume", payload=True, repeat=True)


async def test_dashboard_parses_and_referenced_entities_exist(hass):
    hass.states.async_set("sensor.grid", "100")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True,
        CONF_MINERS: [{"id": "m1", "name": "M1", "model": "x", "ip": "10.0.0.5", "password": "pw",
                       "username": "root", "min_power_w": 800, "max_power_w": 6435,
                       "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    dash = yaml.safe_load(DASH.read_text())
    refs = set()
    _collect(dash, refs)
    created = set(hass.states.async_entity_ids())
    missing = {e for e in refs if e not in created}
    assert not missing, f"dashboard references entities that do not exist: {sorted(missing)}\nActual entities: {sorted(created)}"
