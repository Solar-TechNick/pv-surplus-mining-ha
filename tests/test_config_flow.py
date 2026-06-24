import yaml
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

DETECTED = {"name": "S21+", "model": "Antminer S21+", "min_power_w": 2457, "max_power_w": 6435, "default_power_w": 3878}


def _entry(miners):
    return MockConfigEntry(domain=DOMAIN, data={}, options={
        CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True, CONF_MINERS: miners,
    })


async def test_initial_flow_creates_entry_with_one_miner(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    with patch("custom_components.pv_surplus_mining.config_flow._detect",
               AsyncMock(return_value=DETECTED)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True,
            "name": "S21+", "ip": "10.0.0.5", "password": "pw",
        })
        assert result["step_id"] == "miner_detail"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "model": "Antminer S21+", "min_power_w": 2457, "max_power_w": 6435, "default_power_w": 3878,
        })
        # now offered "add another?" — finish with one miner
        assert result["type"] is FlowResultType.MENU and result["step_id"] == "add_another"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    miners = result["options"][CONF_MINERS]
    assert len(miners) == 1 and miners[0]["id"] == "s21" and miners[0]["default_power_w"] == 3878


async def test_initial_flow_adds_multiple_miners(hass):
    with patch("custom_components.pv_surplus_mining.config_flow._detect",
               AsyncMock(return_value=DETECTED)):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True,
            "name": "Alpha", "ip": "10.0.0.1", "password": "pw"})
        assert result["step_id"] == "miner_detail"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "model": "m", "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000})
        assert result["step_id"] == "add_another"
        # add a second miner
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "add_miner"})
        assert result["step_id"] == "add_miner"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "name": "Beta", "ip": "10.0.0.2", "password": "pw"})
        assert result["step_id"] == "miner_detail"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "model": "m", "min_power_w": 900, "max_power_w": 6435, "default_power_w": 3100})
        assert result["step_id"] == "add_another"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    miners = result["options"][CONF_MINERS]
    assert len(miners) == 2 and {m["id"] for m in miners} == {"alpha", "beta"}


async def test_options_add_and_remove_miner(hass):
    entry = _entry([{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
                     "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}])
    entry.add_to_hass(hass)

    # add: patch _detect BEFORE submitting basics (detect is called when basics
    # are submitted and async_step_add_detail(None) is triggered internally)
    with patch("custom_components.pv_surplus_mining.config_flow._detect",
               AsyncMock(return_value={"name": "B", "model": "m", "min_power_w": 900, "max_power_w": 6435, "default_power_w": 3100})):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_miner"})
        assert result["step_id"] == "add_miner"
        result = await hass.config_entries.options.async_configure(result["flow_id"],
            {"name": "B", "ip": "10.0.0.2", "password": "pw"})
        # submitting basics triggers async_step_add_detail(None) → _detect → form shown
        assert result["step_id"] == "add_detail"
    # detail form now displayed; submit it
    result = await hass.config_entries.options.async_configure(result["flow_id"],
        {"model": "m", "min_power_w": 900, "max_power_w": 6435, "default_power_w": 3100})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    ids = {m["id"] for m in result["data"][CONF_MINERS]}
    assert ids == {"a", "b"}

    # remove "a"
    entry2 = _entry(result["data"][CONF_MINERS]); entry2.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry2.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_miner"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"miner": "a"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {m["id"] for m in result["data"][CONF_MINERS]} == {"b"}


async def test_options_edit_miner(hass):
    """Edit step allows changing ip + password; id and name remain stable."""
    entry = _entry([{
        "id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "original",
        "username": "root", "min_power_w": 800, "max_power_w": 6435,
        "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1,
    }])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit_miner"}
    )
    assert result["step_id"] == "edit_miner"

    # Select miner "a"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"miner": "a"}
    )
    assert result["step_id"] == "edit_detail"

    # Submit edit: new IP, new default_power_w, blank password (keep original)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {
            "model": "m",
            "ip": "10.0.0.9",
            "password": "",          # blank → keep existing
            "min_power_w": 800,
            "max_power_w": 6435,
            "default_power_w": 3500,
        }
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    miners = result["data"][CONF_MINERS]
    assert len(miners) == 1
    m = miners[0]
    assert m["id"] == "a"            # stable
    assert m["ip"] == "10.0.0.9"    # updated
    assert m["default_power_w"] == 3500  # updated
    assert m["password"] == "original"   # unchanged (blank submitted)


async def test_options_hub_rejects_bad_fleet_states(hass, tmp_path):
    """hub step shows form error when fleet-states file references unknown miner ids."""
    entry = _entry([{
        "id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw",
        "username": "root", "min_power_w": 800, "max_power_w": 6435,
        "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1,
    }])
    entry.add_to_hass(hass)

    # Write a fleet-states file that references miner "zzz", not "a"
    bad_states = {
        "states": {
            0: {"zzz": {"action": "sleep"}},
            1: {"zzz": {"action": "active", "power_w": 800}},
        }
    }
    states_file = tmp_path / "fleet_states.yaml"
    states_file.write_text(yaml.dump(bad_states))

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "hub"}
    )
    assert result["step_id"] == "hub"

    # Submit with the bad fleet-states path
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {
            CONF_GRID_ENTITY: "sensor.grid",
            CONF_IMPORT_POSITIVE: True,
            "fleet_states_path": str(states_file),
        }
    )
    # Should stay on the hub form with a bad_fleet_states error
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"
    assert result["errors"]["base"] == "bad_fleet_states"
    # Entry options must NOT have been updated
    assert entry.options.get(CONF_MINERS, [{}])[0]["id"] == "a"


async def test_options_tuning_roundtrip(hass):
    """Tuning step persists updated ControlConfig values."""
    entry = _entry([{
        "id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw",
        "username": "root", "min_power_w": 800, "max_power_w": 6435,
        "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1,
    }])
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tuning"}
    )
    assert result["step_id"] == "tuning"

    # Submit full ControlConfig field set; change loop_interval_s to 15.0
    # Float fields must be submitted as floats (schema type is float)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {
            "enabled_default": False,
            "loop_interval_s": 15.0,
            "export_reserve_w": 300,
            "step_up_export_threshold_w": 700,
            "step_up_required_duration_s": 180.0,
            "step_down_import_threshold_w": 250,
            "step_down_required_duration_s": 30.0,
            "emergency_import_threshold_w": 1200,
            "emergency_required_duration_s": 5.0,
            "min_state_dwell_s": 120.0,
            "fallback_state": 0,
            "max_state": 14,
            "avg_window_s": 60.0,
        }
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["loop_interval_s"] == 15.0
