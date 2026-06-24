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


def _ramp_levels(lo: int, hi: int, step_w: int) -> list[int]:
    """Power levels from lo to hi inclusive, in ~step_w increments (lo first, hi last)."""
    if hi <= lo:
        return [lo]
    n = max(1, round((hi - lo) / max(1, step_w)))
    return [round(lo + (hi - lo) * k / n) for k in range(0, n + 1)]


def generate_fleet_states(miners: list[dict], step_w: int) -> dict[int, dict[str, "FleetStateTarget"]]:
    """Build a fleet-state matrix: state 0 all-off, then ramp each miner (smallest
    minimum first) from its min to its default power; earlier miners stay at their
    default, later miners sleep."""
    ordered = sorted(miners, key=lambda m: (m.get("priority", 0), m["min_power_w"]))
    ids = [m["id"] for m in ordered]
    states: dict[int, dict[str, FleetStateTarget]] = {
        0: {mid: FleetStateTarget(action="sleep") for mid in ids}
    }
    sid = 1
    for idx, m in enumerate(ordered):
        for lvl in _ramp_levels(m["min_power_w"], m["default_power_w"], step_w):
            state: dict[str, FleetStateTarget] = {}
            for j, mm in enumerate(ordered):
                if j < idx:
                    state[mm["id"]] = FleetStateTarget(action="active", power_w=mm["default_power_w"])
                elif j == idx:
                    state[mm["id"]] = FleetStateTarget(action="active", power_w=lvl)
                else:
                    state[mm["id"]] = FleetStateTarget(action="sleep")
            states[sid] = state
            sid += 1
    return states
