"""Load and validate the fleet-state matrix (same format the sweep produces)."""
from __future__ import annotations

from pathlib import Path

import yaml

from .errors import ConfigError
from .models import FleetStateTarget


def load_fleet_states(path: Path) -> dict[int, dict[str, FleetStateTarget]]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"fleet-states file not found at {path}")
    data = yaml.safe_load(path.read_text()) or {}
    states: dict[int, dict[str, FleetStateTarget]] = {}
    for state_id, miners in (data.get("states") or {}).items():
        states[int(state_id)] = {
            mid: FleetStateTarget(**(target or {})) for mid, target in (miners or {}).items()
        }
    if not states:
        raise ConfigError(f"fleet-states file {path} defines no states")
    return states


def validate_fleet_states(states: dict[int, dict[str, FleetStateTarget]], miner_ids: set[str]) -> None:
    if 0 not in states:
        raise ConfigError("fleet states must include state 0 (all miners safe/off)")
    for sid, targets in states.items():
        present = set(targets)
        missing = miner_ids - present
        extra = present - miner_ids
        if missing:
            raise ConfigError(f"fleet state {sid} omits miner(s): {sorted(missing)}")
        if extra:
            raise ConfigError(f"fleet state {sid} references unknown miner(s): {sorted(extra)}")
