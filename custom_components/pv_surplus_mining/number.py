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
    entities = [
        _ControlNumber(coordinator, "manual_state", "Manual state", 0, top),
        _ControlNumber(coordinator, "max_state", "Max state", 0, top),
        _ControlNumber(coordinator, "simulated_grid_w", "Simulated grid (W)", -12000, 12000, step=50),
    ]
    for mid, ctrl in coordinator.fleet.miners.items():
        entities.append(_MinerPowerNumber(coordinator, mid, ctrl.cfg.min_power_w, ctrl.cfg.max_power_w))
        entities.append(_MinerMaxPowerNumber(coordinator, mid, ctrl.cfg.min_power_w, ctrl.cfg.max_power_w))
    add_entities(entities)


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


class _MinerPowerNumber(PvSurplusEntity, NumberEntity):
    """Per-miner power target used in 24/7 (Normal) mode."""
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, mid, lo, hi):
        super().__init__(coordinator, f"{mid}_power_target", f"{mid} power (24/7)")
        self._mid = mid
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = 10

    @property
    def native_value(self) -> float:
        return float(self.coordinator.miner_power_w.get(self._mid, 0))

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.miner_power_w[self._mid] = int(value)
        await self.coordinator.async_request_refresh()


class _MinerMaxPowerNumber(PvSurplusEntity, NumberEntity):
    """Per-miner MAX power for surplus mining: the cap this miner ramps to in the
    fleet-state matrix. Changing it regenerates the matrix (S21+-priority)."""
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, mid, lo, hi):
        super().__init__(coordinator, f"{mid}_max_power", f"{mid} max power")
        self._mid = mid
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_native_step = 50

    @property
    def native_value(self) -> float:
        return float(self.coordinator.miner_max_w.get(self._mid, 0))

    async def async_set_native_value(self, value: float) -> None:
        self.coordinator.miner_max_w[self._mid] = int(value)
        self.coordinator._rebuild_fleet_states()
        await self.coordinator.async_request_refresh()
