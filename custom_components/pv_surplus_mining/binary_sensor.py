from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([_EngagedBinarySensor(coordinator)])


class _EngagedBinarySensor(PvSurplusEntity, BinarySensorEntity):
    """ON when the controller is actually commanding the fleet (auto / normal /
    manual-override / emergency); OFF means observe-only. `reason` attribute carries
    the controller's current decision reason."""

    def __init__(self, coordinator):
        super().__init__(coordinator, "engaged", "Controller engaged")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("engaged"))

    @property
    def extra_state_attributes(self) -> dict:
        return {"reason": (self.coordinator.data or {}).get("reason")}
