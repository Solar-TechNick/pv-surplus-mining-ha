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
    assert result["type"] is FlowResultType.CREATE_ENTRY
    miners = result["options"][CONF_MINERS]
    assert len(miners) == 1 and miners[0]["id"] == "s21" and miners[0]["default_power_w"] == 3878


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
