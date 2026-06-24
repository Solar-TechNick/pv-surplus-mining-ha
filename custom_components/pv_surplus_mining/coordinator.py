"""Control-loop coordinator: read sensors → tick → apply, on the control interval."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS,
    CONF_PV_ENTITY, DOMAIN,
)
from .control.loop import ControlInputs, ControllerLoop
from .errors import AdapterError
from .fleet import FleetController
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig, MinerController
from .models import ControlConfig

_LOGGER = logging.getLogger(__name__)
WARN_TEMP_C = 85.0
CRIT_TEMP_C = 95.0


class PvSurplusCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config: ControlConfig, fleet: FleetController,
                 grid_entity: str, import_positive: bool, pv_entity: str | None = None):
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=config.loop_interval_s),
        )
        self.config = config
        self.fleet = fleet
        self.grid_entity = grid_entity
        self.pv_entity = pv_entity
        self.import_positive = import_positive
        self.loop = ControllerLoop(config, max_available_state=fleet.max_state, current_state=0)
        # operator controls (mutated by entities)
        self.auto_enabled = config.enabled_default
        self.emergency_stop = False
        self.manual_override = False
        self.manual_state = 0
        self.max_state = config.max_state
        self.normal_mode = False

    def _read_grid(self) -> float | None:
        from .normalize import normalize_grid_power
        state = self.hass.states.get(self.grid_entity)
        return normalize_grid_power(state.state if state else None, self.import_positive)

    async def _async_update_data(self) -> dict:
        statuses = {}
        for mid, ctrl in self.fleet.miners.items():
            try:
                statuses[mid] = await ctrl.get_status()
            except AdapterError:
                statuses[mid] = None

        # Recovery pass: if a miner was latched unavailable but a successful status read
        # shows it is now reachable and online, clear the latch so it can be commanded again
        # (including emergency-sleep). A non-None status proves the REST API responded.
        for mid, ctrl in self.fleet.miners.items():
            s = statuses.get(mid)
            if not ctrl.available and s is not None and s.online:
                ctrl.available = True
                ctrl.failure_count = 0
                _LOGGER.info("Miner %s is reachable again; clearing unavailable latch.", mid)

        available_ids = {
            mid for mid, ctrl in self.fleet.miners.items()
            if ctrl.available and statuses.get(mid) is not None and statuses[mid].online
        }
        self.loop.max_available_state = self.fleet.max_available_state(available_ids)

        temps = [s.temp_max_c for s in statuses.values() if s and s.temp_max_c is not None]
        any_warn = any(t >= WARN_TEMP_C for t in temps)
        any_crit = any(t >= CRIT_TEMP_C for t in temps)

        grid_w = self._read_grid()
        sample = grid_w if grid_w is not None else 0.0   # invalid grid -> neutral -> hold (never increase)

        # Note: `telemetry_stale` and `repeated_failures` are intentionally left at their
        # defaults (False). This integration handles grid-sensor loss by holding (feeding a
        # neutral 0.0 sample — see `sample` above), and per-miner write failures by shrinking
        # `max_available_state` (excluding only the affected miner via the availability latch
        # in miner.py), rather than using the vendored core's whole-fleet emergency-to-zero,
        # which would over-react. Sustained-grid-loss escalation is a possible future
        # enhancement when `telemetry_stale` could be wired to a consecutive-None counter.
        normal = self.normal_mode and not self.emergency_stop
        inputs = ControlInputs(
            auto_enabled=(self.auto_enabled or normal),
            emergency_stop=self.emergency_stop,
            manual_override=(self.manual_override or normal),
            manual_state=(self.fleet.max_state if normal else self.manual_state),
            max_state=(self.fleet.max_state if normal else self.max_state),
            all_required_online=(available_ids == set(self.fleet.miners)),
            any_over_temp_warning=any_warn,
            any_over_temp_critical=any_crit,
        )
        decision = self.loop.tick(sample, inputs)

        if decision.changed or decision.emergency:
            try:
                await self.fleet.apply_state(decision.target_state, force=decision.emergency)
            except (AdapterError, KeyError) as exc:
                _LOGGER.warning("apply_state(%s) failed: %s", decision.target_state, exc)

        return {
            "grid_w": grid_w,
            "grid_avg_w": self.loop.grid_avg_w,
            "current_state": self.loop.current_state,
            "target_state": decision.target_state,
            "max_available_state": self.loop.max_available_state,
            "reason": decision.reason,
            "emergency": decision.emergency,
            "miners": {mid: (s.model_dump() if s else None) for mid, s in statuses.items()},
        }


async def async_build_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> PvSurplusCoordinator:
    session = async_get_clientsession(hass)
    data = entry.data
    miners: dict[str, MinerController] = {}
    for m in data[CONF_MINERS]:
        cfg = MinerConfig(
            id=m["id"], model=m["model"], ip=m["ip"], priority=m["priority"],
            min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
            power_targets_w=m.get("power_targets_w", {}),
            command_cooldown_sec=m.get("command_cooldown_sec", 120),
            username=m.get("username", "root"),
        )
        client = AioBraiinsClient(cfg, m["password"], session)
        miners[cfg.id] = MinerController(cfg, client)

    states = load_fleet_states(data[CONF_FLEET_STATES_PATH])
    validate_fleet_states(states, set(miners))
    fleet = FleetController(miners, states)

    config = ControlConfig(**(entry.options or {}))
    coordinator = PvSurplusCoordinator(
        hass, config, fleet,
        grid_entity=data[CONF_GRID_ENTITY],
        import_positive=data.get(CONF_IMPORT_POSITIVE, True),
        pv_entity=data.get(CONF_PV_ENTITY),
    )
    coordinator.config_entry = entry   # explicit (version-independent) — entity.py needs entry_id
    return coordinator
