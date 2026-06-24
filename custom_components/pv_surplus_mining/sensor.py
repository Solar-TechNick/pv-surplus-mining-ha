from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        _DataSensor(coordinator, "fleet_state", "Fleet state", lambda d: d.get("current_state")),
        _DataSensor(coordinator, "target_state", "Target state", lambda d: d.get("target_state")),
        _DataSensor(coordinator, "max_available_state", "Max available state", lambda d: d.get("max_available_state")),
        _PowerSensor(coordinator, "grid_power_w", "Grid power", lambda d: d.get("grid_w")),
        _PowerSensor(coordinator, "grid_avg_w", "Grid power (avg)", lambda d: d.get("grid_avg_w")),
    ]
    for mid in coordinator.fleet.miners:
        entities.append(_PowerSensor(coordinator, f"{mid}_power_w", f"{mid} power",
                                     lambda d, mid=mid: (d.get("miners", {}).get(mid) or {}).get("actual_power_w")))
        entities.append(_TempSensor(coordinator, f"{mid}_temp_c", f"{mid} temperature",
                                    lambda d, mid=mid: (d.get("miners", {}).get(mid) or {}).get("temp_max_c")))
    add_entities(entities)


class _DataSensor(PvSurplusEntity, SensorEntity):
    def __init__(self, coordinator, key, name, getter):
        super().__init__(coordinator, key, name)
        self._getter = getter

    @property
    def native_value(self):
        return self._getter(self.coordinator.data or {})


class _PowerSensor(_DataSensor):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT


class _TempSensor(_DataSensor):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
