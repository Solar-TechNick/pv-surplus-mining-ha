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


def generate_s21_priority_states(miners: list[dict], step_w: int) -> dict[int, dict[str, "FleetStateTarget"]]:
    """Fine-grained, efficiency-priority matrix for the S21+/S19j/S19j fleet.

    The most efficient miner (the S21+) has the highest power-target minimum, so
    it cannot run on small surplus. This matrix therefore:
      1. runs the lowest-minimum miner alone to hold tiny surplus,
      2. ramps the highest-minimum (most efficient) miner to its cap FIRST,
      3. adds the remaining miner, then ramps the two lower-minimum units up.
    ``cap`` is each miner's maximum ramp power (e.g. the per-miner max-power
    control). Ramps use ~``step_w`` increments and totals stay monotonic.

    For fleets that are not exactly three miners this falls back to the
    lowest-minimum-first ``generate_fleet_states`` (which expects
    ``default_power_w`` per miner)."""
    if len(miners) != 3:
        return generate_fleet_states(miners, step_w)

    pilot, middle, priority = sorted(miners, key=lambda m: m["min_power_w"])
    pid, pmin, pcap = pilot["id"], pilot["min_power_w"], int(pilot["cap"])
    mid_, mmin, mcap = middle["id"], middle["min_power_w"], int(middle["cap"])
    sid_, smin, scap = priority["id"], priority["min_power_w"], int(priority["cap"])

    seq: list[dict[str, int | None]] = []

    def st(**w):
        seq.append({pid: w.get(pid), mid_: w.get(mid_), sid_: w.get(sid_)})

    st()                                                # 0: all off
    st(**{pid: pmin})                                   # pilot holds tiny surplus
    for lvl in _ramp_levels(smin, scap, step_w):        # priority ramps to cap first
        st(**{pid: pmin, sid_: lvl})
    st(**{pid: pmin, sid_: scap, mid_: mmin})           # middle joins at its minimum
    for lvl in _ramp_levels(pmin, pcap, step_w)[1:]:    # ramp pilot to cap
        st(**{pid: lvl, sid_: scap, mid_: mmin})
    for lvl in _ramp_levels(mmin, mcap, step_w)[1:]:    # ramp middle to cap
        st(**{pid: pcap, sid_: scap, mid_: lvl})

    deduped = [seq[0]]
    for s in seq[1:]:
        if s != deduped[-1]:                            # rounding can repeat an endpoint
            deduped.append(s)

    return {
        idx: {
            m: (FleetStateTarget(action="active", power_w=int(w)) if w
                else FleetStateTarget(action="sleep"))
            for m, w in s.items()
        }
        for idx, s in enumerate(deduped)
    }


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
