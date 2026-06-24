from aioresponses import aioresponses
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
        CONF_MINERS: [{"id": "s21plus_01", "model": "S21+", "ip": "10.0.0.5", "priority": 1,
                       "min_power_w": 1400, "max_power_w": 4000, "password": "pw",
                       "power_targets_w": {"normal": 3000}}],
        CONF_GRID_ENTITY: "sensor.grid_power", CONF_IMPORT_POSITIVE: True,
        CONF_FLEET_STATES_PATH: str(path),
    }


def _mock_miner(m):
    base = "http://10.0.0.5/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details",
          payload={"miner_identity": {"miner_model": "Antminer S21+", "name": "s21plus_01"}, "status": 2},
          repeat=True)
    m.get(f"{base}/miner/stats",
          payload={"power_stats": {"approximated_consumption": {"watt": 1400}},
                   "miner_stats": {"real_hashrate": {"last_1m": {"gigahash_per_second": 28477.6}}}},
          repeat=True)
    m.get(f"{base}/cooling/state",
          payload={"highest_temperature": {"temperature": {"degree_c": 58.5}}},
          repeat=True)
    m.get(f"{base}/performance/tuner-state",
          payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 1400}}}},
          repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)
    m.put(f"{base}/actions/pause", payload=True, repeat=True)
    m.put(f"{base}/actions/resume", payload=True, repeat=True)


async def test_entities_created_and_switch_writes_back(hass, tmp_path):
    fleet_file = tmp_path / "fleet-states.yaml"; fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "100")
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get("sensor.pv_surplus_mining_fleet_state") is not None
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.auto_enabled is False

        # Find the automation-enabled switch by inspecting registered entities
        # (slug may vary); fall back to searching all switch states.
        switch_entity_id = "switch.pv_surplus_mining_automation_enabled"
        if hass.states.get(switch_entity_id) is None:
            all_switches = [s.entity_id for s in hass.states.async_all("switch")]
            matches = [eid for eid in all_switches if "auto" in eid or "automation" in eid]
            assert matches, f"No automation switch found; registered switches: {all_switches}"
            switch_entity_id = matches[0]

        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": switch_entity_id}, blocking=True,
        )
    assert coordinator.auto_enabled is True


async def test_normal_mode_switch_toggles_coordinator(hass, tmp_path):
    """Toggling the normal_mode switch flips coordinator.normal_mode."""
    fleet_file = tmp_path / "fleet-states.yaml"; fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "100")
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.normal_mode is False

        # Find the normal_mode switch
        all_switches = [s.entity_id for s in hass.states.async_all("switch")]
        matches = [eid for eid in all_switches if "normal" in eid]
        assert matches, f"No normal_mode switch found; registered switches: {all_switches}"
        switch_entity_id = matches[0]

        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": switch_entity_id}, blocking=True,
        )
        assert coordinator.normal_mode is True

        await hass.services.async_call(
            "switch", "turn_off",
            {"entity_id": switch_entity_id}, blocking=True,
        )
        assert coordinator.normal_mode is False
