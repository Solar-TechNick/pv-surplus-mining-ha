"""All-in-one PV-surplus mining controller integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_MINERS, DOMAIN, PLATFORMS
from .coordinator import async_build_coordinator
from .miner_list import recompute_priorities


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate v1 to v2 (all editable config in options; each miner gains default_power_w)."""
    if entry.version >= 2:
        return True
    data = dict(entry.data)
    old_opts = dict(entry.options or {})
    raw = data.pop(CONF_MINERS, None) or old_opts.pop(CONF_MINERS, None) or []
    miners = []
    for m in raw:
        nm = dict(m)
        nm.setdefault("name", nm.get("model", nm["id"]))
        nm["default_power_w"] = int(
            (nm.get("power_targets_w") or {}).get("normal") or nm.get("default_power_w") or nm["max_power_w"]
        )
        miners.append(nm)
    new_options = {**data, **old_opts, CONF_MINERS: recompute_priorities(miners)}
    hass.config_entries.async_update_entry(entry, data={}, options=new_options, version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up pv_surplus_mining from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    coordinator = await async_build_coordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id] = coordinator
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    return True


async def _async_reload_on_update(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = True
    if PLATFORMS:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
