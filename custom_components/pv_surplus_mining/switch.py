from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        _ControlSwitch(coordinator, "auto_enabled", "Automation enabled"),
        _ControlSwitch(coordinator, "emergency_stop", "Emergency stop"),
        _ControlSwitch(coordinator, "manual_override", "Manual override"),
        _ControlSwitch(coordinator, "normal_mode", "Normal mode"),
        _ControlSwitch(coordinator, "pv_mode", "Control on PV production"),
        _ControlSwitch(coordinator, "simulate_grid", "Simulate grid (test)"),
    ]
    for mid in coordinator.fleet.miners:
        entities.append(_MinerEnableSwitch(coordinator, mid))
        entities.append(_MinerSteadySwitch(coordinator, mid))
    add_entities(entities)


class _ControlSwitch(PvSurplusEntity, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, self._key))

    async def async_turn_on(self, **kwargs):
        setattr(self.coordinator, self._key, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        setattr(self.coordinator, self._key, False)
        await self.coordinator.async_request_refresh()


class _MinerEnableSwitch(PvSurplusEntity, SwitchEntity):
    """Hard kill-switch per miner: off => force-paused and excluded from the fleet
    matrix (regenerated), on => available to all modes again."""

    def __init__(self, coordinator, mid):
        super().__init__(coordinator, f"{mid}_enabled", f"{mid} enabled")
        self._mid = mid

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.miner_enabled.get(self._mid, True))

    async def _set(self, value: bool):
        self.coordinator.miner_enabled[self._mid] = value
        self.coordinator._rebuild_fleet_states()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)


class _MinerSteadySwitch(PvSurplusEntity, SwitchEntity):
    """Per-miner steady mode: on => the miner runs ON/OFF at its fixed power
    (its 'power (24/7)' value) instead of being power-modulated, and is ranked
    last in the surplus matrix. For tuner-sensitive miners that thrash when
    their power target is changed. Toggling regenerates the matrix."""

    def __init__(self, coordinator, mid):
        super().__init__(coordinator, f"{mid}_steady", f"{mid} steady (fixed power)")
        self._mid = mid

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.miner_steady.get(self._mid, False))

    async def _set(self, value: bool):
        self.coordinator.miner_steady[self._mid] = value
        self.coordinator._rebuild_fleet_states()
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs):
        await self._set(True)

    async def async_turn_off(self, **kwargs):
        await self._set(False)
