"""Config flow stub — implemented in Task 4."""
from __future__ import annotations

from homeassistant.config_entries import ConfigFlow

from .const import DOMAIN


class PvSurplusMiningConfigFlow(ConfigFlow, domain=DOMAIN):
    """Placeholder config flow; full implementation in Task 4."""

    VERSION = 1
