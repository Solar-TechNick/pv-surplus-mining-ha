from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PvSurplusCoordinator


class PvSurplusEntity(CoordinatorEntity[PvSurplusCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PvSurplusCoordinator, key: str, name: str):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="PV-Surplus Mining",
            manufacturer="Bitmain / Braiins",
        )
