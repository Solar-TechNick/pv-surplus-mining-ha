from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([
        _ControlSwitch(coordinator, "auto_enabled", "Automation enabled"),
        _ControlSwitch(coordinator, "emergency_stop", "Emergency stop"),
        _ControlSwitch(coordinator, "manual_override", "Manual override"),
        _ControlSwitch(coordinator, "normal_mode", "Normal mode"),
        _ControlSwitch(coordinator, "simulate_grid", "Simulate grid (test)"),
    ])


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
