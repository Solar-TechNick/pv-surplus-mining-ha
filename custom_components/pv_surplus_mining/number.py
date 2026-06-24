from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    top = coordinator.fleet.max_state
    add_entities([
        _ControlNumber(coordinator, "manual_state", "Manual state", 0, top),
        _ControlNumber(coordinator, "max_state", "Max state", 0, top),
        _ControlNumber(coordinator, "simulated_grid_w", "Simulated grid (W)", -12000, 12000, step=50),
    ])


class _ControlNumber(PvSurplusEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, key, name, lo, hi, step=1):
        super().__init__(coordinator, key, name)
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = step

    @property
    def native_value(self) -> float:
        return float(getattr(self.coordinator, self._key))

    async def async_set_native_value(self, value: float) -> None:
        setattr(self.coordinator, self._key, int(value))
        await self.coordinator.async_request_refresh()
