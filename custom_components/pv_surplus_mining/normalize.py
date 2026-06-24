"""Pure normalization of an HA sensor state into +import/−export watts."""
from __future__ import annotations

_INVALID = {"unknown", "unavailable", "none", ""}


def normalize_grid_power(raw, import_positive: bool) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        if raw.strip().lower() in _INVALID:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
    return value if import_positive else -value
