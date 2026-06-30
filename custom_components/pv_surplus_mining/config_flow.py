"""Config + options flow for pv_surplus_mining (dynamic miners)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BATTERY_ENTITY, CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY,
    CONF_IMPORT_POSITIVE, CONF_MINERS, CONF_PV_ENTITY, DOMAIN,
)
from .errors import ConfigError
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig, parse_power_constraints
from .miner_list import build_miner, recompute_priorities
from .models import ControlConfig

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_CONTROL_KEYS = list(ControlConfig.model_fields.keys())
# max_state is matrix-derived (tracks the generated top); exclude it from the
# tuning form so users cannot set a stale cap via config. The "Max state" NUMBER
# entity still serves as a transient run-time throttle.
_TUNING_EXCLUDE = {"max_state"}


def _hub_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_GRID_ENTITY, default=d.get(CONF_GRID_ENTITY)): _SENSOR,
        vol.Required(CONF_IMPORT_POSITIVE, default=d.get(CONF_IMPORT_POSITIVE, True)): bool,
        vol.Optional(CONF_PV_ENTITY, default=d.get(CONF_PV_ENTITY) or ""): str,
        vol.Optional(CONF_BATTERY_ENTITY, default=d.get(CONF_BATTERY_ENTITY) or ""): str,
        vol.Optional(CONF_FLEET_STATES_PATH, default=d.get(CONF_FLEET_STATES_PATH) or ""): str,
    })


def _basics_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("name", default=d.get("name", "")): str,
        vol.Required("ip", default=d.get("ip", "")): str,
        vol.Required("password", default=""): str,
    })


def _detail_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("model", default=d.get("model", "")): str,
        vol.Required("min_power_w", default=int(d.get("min_power_w", 0) or 0)): int,
        vol.Required("max_power_w", default=int(d.get("max_power_w", 0) or 0)): int,
        vol.Required("default_power_w", default=int(d.get("default_power_w", 0) or 0)): int,
    })


def _edit_schema(m: dict) -> vol.Schema:
    """Schema for the edit-detail step: includes ip + password (blank = keep)."""
    return vol.Schema({
        vol.Required("model", default=m.get("model", "")): str,
        vol.Required("ip", default=m.get("ip", "")): str,
        vol.Optional("password", default=""): str,
        vol.Required("min_power_w", default=int(m.get("min_power_w", 0) or 0)): int,
        vol.Required("max_power_w", default=int(m.get("max_power_w", 0) or 0)): int,
        vol.Required("default_power_w", default=int(m.get("default_power_w", 0) or 0)): int,
    })


def _tuning_schema(opts: dict) -> vol.Schema:
    tuning_keys = [k for k in _CONTROL_KEYS if k not in _TUNING_EXCLUDE]
    cur = {**ControlConfig().model_dump(), **{k: opts[k] for k in tuning_keys if k in opts}}
    return vol.Schema({
        vol.Required(k, default=cur[k]): (bool if isinstance(cur[k], bool) else (int if isinstance(cur[k], int) else float))
        for k in tuning_keys
    })


async def _detect(hass, name: str, ip: str, password: str) -> dict:
    """Best-effort auto-detect of model + power range. Returns detail-form defaults;
    leaves zeros/blanks on any failure so the user fills them in manually."""
    out = {"name": name, "model": "", "min_power_w": 0, "max_power_w": 0, "default_power_w": 0}
    session = async_get_clientsession(hass)
    cfg = MinerConfig(id="probe", model="", ip=ip, priority=1, min_power_w=0, max_power_w=100000)
    client = AioBraiinsClient(cfg, password, session)
    try:
        await client.login()
        details = await client.get_miner_details()
        ident = details.get("miner_identity") or {}
        out["model"] = ident.get("miner_model") or ident.get("name") or ""
        constraints = await client.get_constraints()
        rng = parse_power_constraints(constraints)
        if rng:
            out["min_power_w"], out["max_power_w"], _ = rng
        # Prefer the firmware's recommended default power target, then the current target.
        default_w = (((constraints.get("tuner_constraints") or {}).get("power_target") or {})
                     .get("default") or {}).get("watt")
        cur = (await client.get_tuner_state()).power_target_w
        out["default_power_w"] = int(default_w or cur or 0)
        if not out["default_power_w"] and out["max_power_w"]:
            out["default_power_w"] = (out["min_power_w"] + out["max_power_w"]) // 2
    except Exception:  # noqa: BLE001 - detection is best-effort; manual entry on failure
        pass
    return out


class PvSurplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._hub: dict = {}
        self._basics: dict = {}
        self._miners: list[dict] = []   # accumulate miners across the setup loop

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._hub = {
                CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                CONF_FLEET_STATES_PATH: user_input.get(CONF_FLEET_STATES_PATH) or "",
            }
            self._basics = {"name": user_input["name"], "ip": user_input["ip"], "password": user_input["password"]}
            return await self.async_step_miner_detail()
        schema = _hub_schema({}).extend(dict(_basics_schema({}).schema))
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_miner_detail(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            d = await _detect(self.hass, self._basics["name"], self._basics["ip"], self._basics["password"])
            return self.async_show_form(step_id="miner_detail", data_schema=_detail_schema(d))
        self._miners.append(build_miner(
            self._basics["name"], self._basics["ip"], self._basics["password"],
            user_input["model"], user_input["min_power_w"], user_input["max_power_w"],
            user_input["default_power_w"], taken_ids={m["id"] for m in self._miners}))
        return await self.async_step_add_another()

    async def async_step_add_another(self, user_input: dict[str, Any] | None = None):
        # After each miner is added, offer to add another or finish.
        return self.async_show_menu(step_id="add_another", menu_options=["add_miner", "finish"])

    async def async_step_add_miner(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            return self.async_show_form(step_id="add_miner", data_schema=_basics_schema({}))
        self._basics = {"name": user_input["name"], "ip": user_input["ip"], "password": user_input["password"]}
        return await self.async_step_miner_detail()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        options = {**self._hub, CONF_MINERS: recompute_priorities(self._miners), **ControlConfig().model_dump()}
        return self.async_create_entry(title="PV-Surplus Mining", data={}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PvSurplusOptionsFlow()


class PvSurplusOptionsFlow(config_entries.OptionsFlow):
    def __init__(self) -> None:
        self._basics: dict = {}
        self._edit_id: str | None = None

    def _config(self) -> dict:
        return {**self.config_entry.data, **(self.config_entry.options or {})}

    def _miners(self) -> list[dict]:
        return [dict(m) for m in self._config().get(CONF_MINERS, [])]

    async def _save(self, updates: dict):
        cfg = self._config()
        cfg.update(updates)
        return self.async_create_entry(title="", data=cfg)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init",
            menu_options=["add_miner", "edit_miner", "remove_miner", "hub", "tuning"])

    async def async_step_add_miner(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="add_miner", data_schema=_basics_schema({}))
        self._basics = {"name": user_input["name"], "ip": user_input["ip"], "password": user_input["password"]}
        return await self.async_step_add_detail()

    async def async_step_add_detail(self, user_input=None):
        if user_input is None:
            d = await _detect(self.hass, self._basics["name"], self._basics["ip"], self._basics["password"])
            return self.async_show_form(step_id="add_detail", data_schema=_detail_schema(d))
        miners = self._miners()
        new = build_miner(self._basics["name"], self._basics["ip"], self._basics["password"],
                          user_input["model"], user_input["min_power_w"], user_input["max_power_w"],
                          user_input["default_power_w"], taken_ids={m["id"] for m in miners})
        miners.append(new)
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_edit_miner(self, user_input=None):
        ids = [m["id"] for m in self._miners()]
        if user_input is None:
            return self.async_show_form(step_id="edit_miner",
                data_schema=vol.Schema({vol.Required("miner"): vol.In(ids)}))
        self._edit_id = user_input["miner"]
        return await self.async_step_edit_detail()

    async def async_step_edit_detail(self, user_input=None):
        miners = self._miners()
        m = next(x for x in miners if x["id"] == self._edit_id)
        if user_input is None:
            return self.async_show_form(step_id="edit_detail", data_schema=_edit_schema(m))
        m.update(model=user_input["model"], ip=user_input["ip"],
                 min_power_w=user_input["min_power_w"],
                 max_power_w=user_input["max_power_w"],
                 default_power_w=user_input["default_power_w"])
        if user_input.get("password"):
            m["password"] = user_input["password"]
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_remove_miner(self, user_input=None):
        ids = [m["id"] for m in self._miners()]
        if user_input is None:
            return self.async_show_form(step_id="remove_miner",
                data_schema=vol.Schema({vol.Required("miner"): vol.In(ids)}))
        miners = [m for m in self._miners() if m["id"] != user_input["miner"]]
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_hub(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input.get(CONF_FLEET_STATES_PATH) or ""
            if path:
                try:
                    validate_fleet_states(load_fleet_states(path), {m["id"] for m in self._miners()})
                except ConfigError:
                    errors["base"] = "bad_fleet_states"
            if not errors:
                return await self._save({
                    CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                    CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                    CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                    CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                    CONF_FLEET_STATES_PATH: path,
                })
        return self.async_show_form(step_id="hub", data_schema=_hub_schema(self._config()), errors=errors)

    async def async_step_tuning(self, user_input=None):
        if user_input is not None:
            return await self._save(user_input)
        return self.async_show_form(step_id="tuning", data_schema=_tuning_schema(self.config_entry.options or {}))
