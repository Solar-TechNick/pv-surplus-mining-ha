"""Config + options flow for pv_surplus_mining."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BATTERY_ENTITY, CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE,
    CONF_MINERS, CONF_PV_ENTITY, DEFAULT_FLEET_STATES_FILENAME, DEFAULT_MINERS, DOMAIN,
)
from .errors import ConfigError
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig
from .models import ControlConfig

_ENTITY_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _user_schema(defaults: dict) -> vol.Schema:
    fields: dict = {}
    for spec in DEFAULT_MINERS:
        mid = spec["id"]
        fields[vol.Required(f"{mid}_ip", default=defaults.get(f"{mid}_ip", ""))] = str
        fields[vol.Required(f"{mid}_password", default="")] = str
    fields[vol.Required(CONF_GRID_ENTITY, default=defaults.get(CONF_GRID_ENTITY))] = _ENTITY_SENSOR
    fields[vol.Optional(CONF_PV_ENTITY, default=defaults.get(CONF_PV_ENTITY, ""))] = str
    fields[vol.Optional(CONF_BATTERY_ENTITY, default=defaults.get(CONF_BATTERY_ENTITY, ""))] = str
    fields[vol.Required(CONF_IMPORT_POSITIVE, default=defaults.get(CONF_IMPORT_POSITIVE, True))] = bool
    fields[vol.Required(CONF_FLEET_STATES_PATH, default=defaults.get(CONF_FLEET_STATES_PATH, ""))] = str
    return vol.Schema(fields)


class PvSurplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        default_path = self.hass.config.path(DOMAIN, DEFAULT_FLEET_STATES_FILENAME)

        if user_input is not None:
            miners = []
            for spec in DEFAULT_MINERS:
                mid = spec["id"]
                miners.append({**spec, "ip": user_input[f"{mid}_ip"], "password": user_input[f"{mid}_password"]})

            # validate fleet-states file
            try:
                states = load_fleet_states(user_input[CONF_FLEET_STATES_PATH])
                validate_fleet_states(states, {m["id"] for m in miners})
            except ConfigError:
                errors["base"] = "bad_fleet_states"

            # best-effort connectivity check (one login per miner)
            if not errors:
                session = async_get_clientsession(self.hass)
                for m in miners:
                    cfg = MinerConfig(id=m["id"], model=m["model"], ip=m["ip"], priority=m["priority"],
                                      min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
                                      username=m["username"])
                    try:
                        await AioBraiinsClient(cfg, m["password"], session).login()
                    except Exception:  # noqa: BLE001 - any network error blocks setup
                        errors["base"] = "cannot_connect"
                        break

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_MINERS: miners,
                    CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                    CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                    CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                    CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                    CONF_FLEET_STATES_PATH: user_input[CONF_FLEET_STATES_PATH],
                }
                return self.async_create_entry(title="PV-Surplus Mining", data=data, options={})

        defaults = user_input or {CONF_FLEET_STATES_PATH: default_path, CONF_IMPORT_POSITIVE: True}
        return self.async_show_form(step_id="user", data_schema=_user_schema(defaults), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PvSurplusOptionsFlow(config_entry)


_OPTION_KEYS = list(ControlConfig.model_fields.keys())


class PvSurplusOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**ControlConfig().model_dump(), **(self.config_entry.options or {})}
        schema = vol.Schema({
            vol.Required(k, default=current[k]): (bool if isinstance(current[k], bool)
                                                  else (int if isinstance(current[k], int) else float))
            for k in _OPTION_KEYS
        })
        return self.async_show_form(step_id="init", data_schema=schema)
