"""Control-loop coordinator: read sensors → tick → apply, on the control interval."""
from __future__ import annotations

import logging
import math
from datetime import timedelta
from pathlib import Path

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
from .fleet_states import generate_fleet_states, load_fleet_states, validate_fleet_states
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
        # PV-production mode: when on, the loop tracks PV production (pv_entity)
        # instead of grid surplus — miners ramp to consume the PV output regardless
        # of house load. Off (default) = control on grid surplus at the meter.
        self.pv_mode = False
        # test/simulation: when simulate_grid is on, the loop uses simulated_grid_w
        # (+import / -export) instead of the real grid sensor.
        self.simulate_grid = False
        self.simulated_grid_w = 0.0

    def _read_grid(self) -> float | None:
        if self.simulate_grid:
            return float(self.simulated_grid_w)
        from .normalize import normalize_grid_power
        state = self.hass.states.get(self.grid_entity)
        return normalize_grid_power(state.state if state else None, self.import_positive)

    def _read_pv(self) -> float | None:
        """PV production in watts (non-negative), or None if unavailable."""
        if not self.pv_entity:
            return None
        state = self.hass.states.get(self.pv_entity)
        if state is None:
            return None
        raw = state.state
        if raw is None or str(raw).strip().lower() in ("unknown", "unavailable", "none", ""):
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value):
            return None
        if (state.attributes or {}).get("unit_of_measurement") in ("kW", "kw"):
            value *= 1000.0
        return abs(value)

    def _state_desynced(self, target_state: int, statuses: dict) -> bool:
        """True if any miner's live pause posture disagrees with what target_state
        wants: should be active but is paused, or should sleep but is running.
        Miners with no fresh online status are skipped (we can't tell)."""
        targets = self.fleet.states.get(target_state, {})
        for mid, t in targets.items():
            s = statuses.get(mid)
            if s is None or not s.online:
                continue
            should_active = (t.action == "active" and t.power_w is not None)
            if should_active and s.paused:
                return True
            if not should_active and not s.paused:
                return True
        return False

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

        # Reconcile each controller's pause belief from live status (truth, not the
        # optimistic flag set by resume()). This lets desync detection and the
        # auto-resume in set_power_target act on reality.
        for mid, ctrl in self.fleet.miners.items():
            s = statuses.get(mid)
            if s is not None and s.online:
                ctrl.paused = s.paused

        available_ids = {
            mid for mid, ctrl in self.fleet.miners.items()
            if ctrl.available and statuses.get(mid) is not None and statuses[mid].online
        }
        self.loop.max_available_state = self.fleet.max_available_state(available_ids)

        temps = [s.temp_max_c for s in statuses.values() if s and s.temp_max_c is not None]
        any_warn = any(t >= WARN_TEMP_C for t in temps)
        any_crit = any(t >= CRIT_TEMP_C for t in temps)

        grid_w = self._read_grid()
        pv_w = self._read_pv()
        if self.pv_mode and not self.simulate_grid:
            # PV-production control: drive miners to consume the PV output (less the
            # export buffer baked into the step thresholds), independent of house load.
            # Signal (import-positive) = current fleet draw − PV. Negative => PV
            # headroom available => step up; positive => miners exceed PV => step down.
            # PV unknown -> neutral hold (never increase).
            miner_target_w = self.fleet.state_power_total(self.loop.current_state)
            sample = (miner_target_w - pv_w) if pv_w is not None else 0.0
        else:
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

        # The controller only commands miners when the user has ENGAGED it
        # (automation on, normal mode on, or the emergency-stop switch on). With
        # everything off it is observe-only and never touches the miners — so a
        # hands-off install is not paused by the hard-import safety reacting to
        # the miners' own draw.
        #
        # Apply on a state change/emergency OR when reality drifts from the target
        # state (a miner that should run is paused, or vice-versa). Re-applying an
        # unchanged state is self-healing: apply_state is idempotent for in-sync
        # miners, and re-issues resume()/pause() for the ones that are not — so a
        # paused miner that should be running gets woken every tick until it is,
        # instead of the controller silently believing it ramped up.
        engaged = self.auto_enabled or self.normal_mode or self.emergency_stop
        if engaged and (decision.changed or decision.emergency
                        or self._state_desynced(decision.target_state, statuses)):
            try:
                await self.fleet.apply_state(decision.target_state, force=decision.emergency)
            except (AdapterError, KeyError) as exc:
                _LOGGER.warning("apply_state(%s) failed: %s", decision.target_state, exc)

        return {
            "grid_w": grid_w,
            "grid_avg_w": self.loop.grid_avg_w,
            "pv_w": pv_w,
            "control_mode": "pv_production" if self.pv_mode else "surplus",
            "control_signal_w": sample,
            "current_state": self.loop.current_state,
            "target_state": decision.target_state if engaged else self.loop.current_state,
            "max_available_state": self.loop.max_available_state,
            "reason": decision.reason if engaged else "observe-only (controller not engaged)",
            "emergency": decision.emergency and engaged,
            "miners": {mid: (s.model_dump() if s else None) for mid, s in statuses.items()},
        }


async def async_build_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> PvSurplusCoordinator:
    session = async_get_clientsession(hass)
    cfg = {**entry.data, **(entry.options or {})}
    control_kwargs = {k: cfg[k] for k in ControlConfig.model_fields if k in cfg}
    control = ControlConfig(**control_kwargs)

    def _default_w(m: dict) -> int:
        return int(m.get("default_power_w") or (m.get("power_targets_w") or {}).get("normal") or m["max_power_w"])

    miners: dict[str, MinerController] = {}
    for m in cfg[CONF_MINERS]:
        mc = MinerConfig(
            id=m["id"], model=m.get("model", m["id"]), ip=m["ip"], priority=m.get("priority", 1),
            min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
            power_targets_w={"normal": _default_w(m)},
            command_cooldown_sec=m.get("command_cooldown_sec", 120),
            username=m.get("username", "root"),
        )
        miners[mc.id] = MinerController(mc, AioBraiinsClient(mc, m["password"], session))

    path = cfg.get(CONF_FLEET_STATES_PATH) or ""
    if path and Path(path).exists():
        states = load_fleet_states(path)
    else:
        gen = [{"id": m["id"], "min_power_w": m["min_power_w"],
                "default_power_w": _default_w(m), "priority": m.get("priority", 1)} for m in cfg[CONF_MINERS]]
        states = generate_fleet_states(gen, control.step_up_export_threshold_w)
    validate_fleet_states(states, set(miners))
    fleet = FleetController(miners, states)

    coordinator = PvSurplusCoordinator(
        hass, control, fleet,
        grid_entity=cfg[CONF_GRID_ENTITY],
        import_positive=cfg.get(CONF_IMPORT_POSITIVE, True),
        pv_entity=cfg.get(CONF_PV_ENTITY),
    )
    coordinator.config_entry = entry
    return coordinator
