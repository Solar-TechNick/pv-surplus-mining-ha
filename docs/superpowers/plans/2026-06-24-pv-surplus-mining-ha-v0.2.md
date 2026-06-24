# PV-Surplus Mining HA Integration v0.2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hard-coded 3-miner fleet with dynamically added miners, auto-generate the fleet-state matrix from each miner's power range (mandatory hand-written file becomes optional), and ship a Lovelace dashboard — released as v0.2.0.

**Architecture:** Miners become a list stored in `entry.options`, managed by an options-flow menu (add/edit/remove/hub/tuning); each miner's power range + model auto-detect from the Braiins API on add. A pure `generate_fleet_states()` builds the state matrix from the miners (true-off state 0, smallest-min-first ramp to each miner's default in ~`step_up_export_threshold_w` steps); the coordinator uses it unless a custom file overrides. The control core (decision/loop, miner pause-to-off, coordinator tick) is unchanged.

**Tech Stack:** Home Assistant config + options flow, `aiohttp`, `pydantic` v2, PyYAML. Tests: `pytest` + `pytest-homeassistant-custom-component` + `aioresponses`.

Design: [docs/superpowers/specs/2026-06-24-pv-surplus-mining-ha-v0.2-dynamic-miners-design.md](../specs/2026-06-24-pv-surplus-mining-ha-v0.2-dynamic-miners-design.md).

## Global Constraints

- **Domain** `pv_surplus_mining`. Run tests from repo root: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest`. `asyncio_mode = "auto"`. **CI runs Python 3.13** (the harness pins a Py3.13 Home Assistant).
- Do NOT modify the vendored `control/decision.py` or `control/loop.py`, nor the `MinerController` pause/resume/safety logic, nor the coordinator's tick (`_async_update_data`). This version changes only configuration, the matrix source, the client's read methods, and adds a dashboard.
- **`DEFAULT_MINERS` is removed** — miners are fully dynamic.
- Editable config (hub settings, miners list, thresholds) lives in **`entry.options`**; the coordinator reads a merged view `{**entry.data, **entry.options}`. New entries are **config-entry VERSION 2**; v1 entries are migrated.
- Each miner dict (`MinerCfg`): `{id, name, model, ip, password, username, min_power_w, max_power_w, default_power_w, command_cooldown_sec, priority}`. `id` = unique slug of `name`. `priority` = rank by ascending `min_power_w` unless overridden.
- Auto-generated matrix: state 0 = all `sleep`; ramp each miner (smallest-min first) `min_power_w → default_power_w` in ~`step_w` steps (default `step_w = step_up_export_threshold_w`); earlier miners pinned at default, later asleep; every state lists every miner.
- Credentials never in repo files (passwords live only in the config entry's encrypted `.storage`).
- Confirmed Braiins endpoints: `GET /api/v1/configuration/constraints` (power min/max/step, undocumented nesting → parse leniently), `GET /api/v1/miner/details` (`platform`/`miner_identity` = model), `GET /api/v1/performance/tuner-state` (current `power_target.watt`).

---

## File Structure

```
custom_components/pv_surplus_mining/
  miner.py            # MODIFY: + AioBraiinsClient.get_constraints; + module fn parse_power_constraints
  fleet_states.py     # MODIFY: + generate_fleet_states (load/validate unchanged)
  miner_list.py       # CREATE: slugify_id, ensure_unique_ids, build_miner, recompute_priorities
  const.py            # MODIFY: remove DEFAULT_MINERS; keep keys
  coordinator.py      # MODIFY: async_build_coordinator reads dynamic miners + generated/override matrix
  config_flow.py      # REWRITE: initial step (hub + first miner) + options menu (add/edit/remove/hub/tuning)
  __init__.py         # MODIFY: + async_migrate_entry (V1->V2); ConfigFlow VERSION=2 (in config_flow.py)
  translations/en.json# MODIFY: strings for the new flow steps/menu
  manifest.json       # MODIFY: version 0.1.0 -> 0.2.0
dashboards/
  pv-surplus-mining.yaml   # CREATE: Lovelace dashboard (built-in cards)
README.md             # MODIFY: dynamic-miners setup + Dashboard section
tests/
  test_miner.py            # MODIFY: + get_constraints / parse_power_constraints
  test_fleet_states_gen.py # CREATE: generate_fleet_states
  test_miner_list.py       # CREATE: slug/unique/build/recompute
  test_coordinator.py      # MODIFY: dynamic-miner build + generated/override matrix
  test_config_flow.py      # REWRITE: initial + options menu add/edit/remove
  test_migration.py        # CREATE: v1 -> v2 entry migration
  test_dashboard.py        # CREATE: dashboard YAML parses + entity-key check
```

---

### Task 1: `get_constraints` + `parse_power_constraints` (auto-detect power range)

**Files:**
- Modify: `custom_components/pv_surplus_mining/miner.py`
- Test: `tests/test_miner.py`

**Interfaces:**
- Produces: `AioBraiinsClient.get_constraints() -> dict` (`GET /configuration/constraints`).
- Produces: module fn `parse_power_constraints(raw: dict) -> tuple[int, int, int] | None` returning `(min_w, max_w, step_w)` or `None` if power-target min/max can't be found. Lenient about nesting (`{"watt": N}` vs flat `N`).

- [ ] **Step 1: Write failing tests — append to `tests/test_miner.py`**

```python
from custom_components.pv_surplus_mining.miner import parse_power_constraints, AioBraiinsClient


def test_parse_power_constraints_nested_watt():
    raw = {"tuner_constraints": {"power_target": {"min": {"watt": 817}, "max": {"watt": 6435}, "step": {"watt": 100}}}}
    assert parse_power_constraints(raw) == (817, 6435, 100)


def test_parse_power_constraints_flat():
    raw = {"tuner_constraints": {"power_target": {"min": 944, "max": 6435}}}
    assert parse_power_constraints(raw) == (944, 6435, 100)   # step defaults to 100


def test_parse_power_constraints_missing_returns_none():
    assert parse_power_constraints({}) is None
    assert parse_power_constraints({"tuner_constraints": {}}) is None


async def test_get_constraints_calls_endpoint():
    import aiohttp
    from aioresponses import aioresponses
    from custom_components.pv_surplus_mining.miner import MinerConfig
    cfg = MinerConfig(id="m", model="x", ip="10.0.0.9", priority=1, min_power_w=800, max_power_w=6435)
    base = "http://10.0.0.9/api/v1"
    with aioresponses() as m:
        m.post(f"{base}/auth/login", payload={"token": "T"})
        m.get(f"{base}/configuration/constraints",
              payload={"tuner_constraints": {"power_target": {"min": {"watt": 800}, "max": {"watt": 6435}}}})
        async with aiohttp.ClientSession() as session:
            raw = await AioBraiinsClient(cfg, "pw", session).get_constraints()
    assert parse_power_constraints(raw) == (800, 6435, 100)
```

- [ ] **Step 2: Run to verify they fail**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_miner.py -k "constraints" -v`
Expected: ImportError / AttributeError (not defined yet).

- [ ] **Step 3: Add the client method + parser to `miner.py`**

Add the method inside `AioBraiinsClient` (next to `get_tuner_state`):
```python
    async def get_constraints(self) -> dict:
        return await self._request_json("GET", "/configuration/constraints")
```

Add this module-level function to `miner.py` (after the imports, before the classes):
```python
def parse_power_constraints(raw: dict) -> tuple[int, int, int] | None:
    """Best-effort extract (min_w, max_w, step_w) from /configuration/constraints.

    Field nesting is undocumented; accept either {"watt": N} or a bare number.
    Returns None when power-target min/max cannot be found (caller falls back
    to manual entry).
    """
    def _watt(v):
        if isinstance(v, dict):
            return v.get("watt")
        return v

    pt = ((raw or {}).get("tuner_constraints") or {}).get("power_target") or {}
    lo, hi, step = _watt(pt.get("min")), _watt(pt.get("max")), _watt(pt.get("step"))
    if lo is None or hi is None:
        return None
    try:
        return int(lo), int(hi), int(step) if step is not None else 100
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 4: Run the tests + full suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_miner.py -v` then `... -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/miner.py tests/test_miner.py
git commit -m "feat: Braiins get_constraints + lenient parse_power_constraints"
```

---

### Task 2: `generate_fleet_states` (auto-built matrix)

**Files:**
- Modify: `custom_components/pv_surplus_mining/fleet_states.py`
- Test: `tests/test_fleet_states_gen.py`

**Interfaces:**
- Consumes: `models.FleetStateTarget`.
- Produces: `generate_fleet_states(miners: list[dict], step_w: int) -> dict[int, dict[str, FleetStateTarget]]`. Each `miners` dict needs at least `id`, `min_power_w`, `default_power_w`, `priority`. State 0 = all sleep; ramp each miner (sorted by `priority` then `min_power_w`) from `min_power_w` to `default_power_w` in ~`step_w` increments; earlier miners pinned at their `default_power_w`, later miners `sleep`. Every state contains every miner id.

- [ ] **Step 1: Write failing tests — `tests/test_fleet_states_gen.py`**

```python
from custom_components.pv_surplus_mining.fleet_states import generate_fleet_states, validate_fleet_states


MINERS = [
    {"id": "a", "min_power_w": 800, "default_power_w": 3000, "priority": 1},
    {"id": "b", "min_power_w": 2400, "default_power_w": 3800, "priority": 2},
]


def _total(state):
    return sum(t.power_w for t in state.values() if t.action == "active" and t.power_w)


def test_state_zero_all_sleep():
    states = generate_fleet_states(MINERS, step_w=700)
    assert 0 in states
    assert all(t.action == "sleep" for t in states[0].values())
    assert set(states[0]) == {"a", "b"}


def test_every_state_lists_every_miner_and_validates():
    states = generate_fleet_states(MINERS, step_w=700)
    for targets in states.values():
        assert set(targets) == {"a", "b"}
    validate_fleet_states(states, {"a", "b"})   # must not raise


def test_smallest_min_first_and_capped_at_default():
    states = generate_fleet_states(MINERS, step_w=700)
    # miner "a" (min 800) ramps first; "b" stays asleep until "a" is at its default
    first_active = states[1]
    assert first_active["a"].action == "active" and first_active["a"].power_w == 800
    assert first_active["b"].action == "sleep"
    # no active target ever exceeds the miner's default
    for targets in states.values():
        for mid, t in targets.items():
            if t.action == "active":
                cap = next(m["default_power_w"] for m in MINERS if m["id"] == mid)
                assert t.power_w <= cap


def test_totals_monotonic_increasing():
    states = generate_fleet_states(MINERS, step_w=700)
    totals = [_total(states[s]) for s in sorted(states)]
    assert totals == sorted(totals)
    assert totals[0] == 0


def test_single_level_when_default_equals_min():
    miners = [{"id": "x", "min_power_w": 2457, "default_power_w": 2457, "priority": 1}]
    states = generate_fleet_states(miners, step_w=700)
    assert sorted(states) == [0, 1]
    assert states[1]["x"].power_w == 2457
```

- [ ] **Step 2: Run to verify they fail**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_fleet_states_gen.py -v`
Expected: ImportError (`generate_fleet_states` not defined).

- [ ] **Step 3: Add `generate_fleet_states` to `fleet_states.py`**

```python
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
```

- [ ] **Step 4: Run the tests + full suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_fleet_states_gen.py -v` then `... -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/fleet_states.py tests/test_fleet_states_gen.py
git commit -m "feat: generate_fleet_states (auto-built smallest-min-first matrix)"
```

---

### Task 3: `miner_list.py` helpers

**Files:**
- Create: `custom_components/pv_surplus_mining/miner_list.py`
- Test: `tests/test_miner_list.py`

> Note: `DEFAULT_MINERS` is NOT removed here — `config_flow.py` still imports it and would break the whole suite. It is removed in Task 5 (the config-flow rewrite), the only place that uses it.

**Interfaces:**
- Produces: `slugify_id(name: str) -> str`; `ensure_unique_id(base: str, taken: set[str]) -> str`; `build_miner(name, ip, password, model, min_power_w, max_power_w, default_power_w, taken_ids, command_cooldown_sec=120, username="root") -> dict`; `recompute_priorities(miners: list[dict]) -> list[dict]` (returns the list with `priority` reassigned by ascending `min_power_w`, 1-based, preserving an explicit `priority_override` when present).

- [ ] **Step 1: Write failing tests — `tests/test_miner_list.py`**

```python
from custom_components.pv_surplus_mining.miner_list import (
    slugify_id, ensure_unique_id, build_miner, recompute_priorities,
)


def test_slugify_id():
    assert slugify_id("Antminer S21+ #1") == "antminer_s21_1"
    assert slugify_id("  ") == "miner"


def test_ensure_unique_id():
    assert ensure_unique_id("s21", set()) == "s21"
    assert ensure_unique_id("s21", {"s21"}) == "s21_2"
    assert ensure_unique_id("s21", {"s21", "s21_2"}) == "s21_3"


def test_build_miner_assigns_unique_id_and_normal_target():
    m = build_miner("S21+", "10.0.0.5", "pw", "Antminer S21+", 2457, 6435, 3878, taken_ids=set())
    assert m["id"] == "s21" and m["ip"] == "10.0.0.5" and m["default_power_w"] == 3878
    assert m["username"] == "root" and m["command_cooldown_sec"] == 120


def test_recompute_priorities_by_ascending_min():
    miners = [
        {"id": "big", "min_power_w": 2457},
        {"id": "small", "min_power_w": 817},
        {"id": "mid", "min_power_w": 944},
    ]
    out = recompute_priorities(miners)
    by_id = {m["id"]: m["priority"] for m in out}
    assert by_id == {"small": 1, "mid": 2, "big": 3}
```

- [ ] **Step 2: Run to verify they fail**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_miner_list.py -v`
Expected: ImportError.

- [ ] **Step 3: Create `custom_components/pv_surplus_mining/miner_list.py`**

```python
"""Helpers for the dynamic miner list (ids, building, priority ordering)."""
from __future__ import annotations

import re


def slugify_id(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "miner"


def ensure_unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def build_miner(name: str, ip: str, password: str, model: str,
                min_power_w: int, max_power_w: int, default_power_w: int,
                taken_ids: set[str], command_cooldown_sec: int = 120,
                username: str = "root") -> dict:
    mid = ensure_unique_id(slugify_id(name), taken_ids)
    return {
        "id": mid, "name": name, "model": model, "ip": ip, "password": password,
        "username": username, "min_power_w": int(min_power_w),
        "max_power_w": int(max_power_w), "default_power_w": int(default_power_w),
        "command_cooldown_sec": int(command_cooldown_sec),
    }


def recompute_priorities(miners: list[dict]) -> list[dict]:
    """Assign priority 1.. by ascending min_power_w (a miner's explicit
    'priority_override' wins and is used as the sort key when present)."""
    def key(m):
        return (m.get("priority_override", m["min_power_w"]), m["min_power_w"])
    out = sorted(miners, key=key)
    for i, m in enumerate(out, start=1):
        m["priority"] = i
    return out
```

- [ ] **Step 4: Run the tests + full suite** (purely additive — suite stays green)

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_miner_list.py -v` then `... -m pytest -q`
Expected: the 4 new tests pass; full suite still green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/miner_list.py tests/test_miner_list.py
git commit -m "feat: dynamic miner-list helpers (slug/unique/build/priority)"
```

---

### Task 4: Coordinator builds from dynamic miners + generated/override matrix

**Files:**
- Modify: `custom_components/pv_surplus_mining/coordinator.py` (only `async_build_coordinator`)
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `generate_fleet_states` (Task 2), `load_fleet_states`/`validate_fleet_states`, `ControlConfig`.
- Produces: `async_build_coordinator(hass, entry)` reads miners from the **merged** config `{**entry.data, **entry.options}`; builds the matrix from a custom file when `fleet_states_path` is set and exists, otherwise `generate_fleet_states(...)`. Backward-compatible with v0.1.0-shaped entries (miners in `entry.data`, `power_targets_w` present, a fleet-states path set). The `PvSurplusCoordinator` class and `_async_update_data` are unchanged.

- [ ] **Step 1: Write failing tests — append to `tests/test_coordinator.py`**

```python
async def test_build_coordinator_generates_matrix_from_options(hass):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.const import DOMAIN
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True,
        "miners": [
            {"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
             "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1},
            {"id": "b", "name": "B", "model": "m", "ip": "10.0.0.2", "password": "pw", "username": "root",
             "min_power_w": 2400, "max_power_w": 6435, "default_power_w": 3800, "command_cooldown_sec": 120, "priority": 2},
        ],
    })
    entry.add_to_hass(hass)
    coordinator = await async_build_coordinator(hass, entry)   # no network at build time
    assert set(coordinator.fleet.miners) == {"a", "b"}
    assert 0 in coordinator.fleet.states and coordinator.fleet.max_state >= 1
    assert all(t.action == "sleep" for t in coordinator.fleet.states[0].values())


async def test_build_coordinator_uses_custom_file_override(hass, tmp_path):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.const import DOMAIN
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    f = tmp_path / "fleet-states.yaml"
    f.write_text("states:\n  0:\n    a: { action: sleep }\n  1:\n    a: { action: active, power_w: 1000 }\n")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True, "fleet_states_path": str(f),
        "miners": [{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
                    "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    coordinator = await async_build_coordinator(hass, entry)
    assert sorted(coordinator.fleet.states) == [0, 1]
    assert coordinator.fleet.states[1]["a"].power_w == 1000
```

- [ ] **Step 2: Run to verify they fail**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_coordinator.py -k "build_coordinator" -v`
Expected: fail (old `async_build_coordinator` reads `entry.data` only / requires a path).

- [ ] **Step 3: Replace `async_build_coordinator` in `coordinator.py`**

Add imports at the top: `from pathlib import Path` and `from .fleet_states import generate_fleet_states` (extend the existing `from .fleet_states import ...` line). Replace the whole `async_build_coordinator` function with:

```python
async def async_build_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> PvSurplusCoordinator:
    session = async_get_clientsession(hass)
    cfg = {**entry.data, **(entry.options or {})}
    control_kwargs = {k: cfg[k] for k in ControlConfig.model_fields if k in cfg}
    control = ControlConfig(**control_kwargs)

    def _default_w(m: dict) -> int:
        return int(m.get("default_power_w") or (m.get("power_targets_w") or {}).get("normal") or m["max_power_w"])

    miners: dict[str, MinerController] = {}
    for m in cfg[CONF_MINERS]:
        mc = MinerConfig(
            id=m["id"], model=m.get("model", m["id"]), ip=m["ip"], priority=m.get("priority", 1),
            min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
            power_targets_w={"normal": _default_w(m)},
            command_cooldown_sec=m.get("command_cooldown_sec", 120),
            username=m.get("username", "root"),
        )
        miners[mc.id] = MinerController(mc, AioBraiinsClient(mc, m["password"], session))

    path = cfg.get(CONF_FLEET_STATES_PATH) or ""
    if path and Path(path).exists():
        states = load_fleet_states(path)
    else:
        gen = [{"id": m["id"], "min_power_w": m["min_power_w"],
                "default_power_w": _default_w(m), "priority": m.get("priority", 1)} for m in cfg[CONF_MINERS]]
        states = generate_fleet_states(gen, control.step_up_export_threshold_w)
    validate_fleet_states(states, set(miners))
    fleet = FleetController(miners, states)

    coordinator = PvSurplusCoordinator(
        hass, control, fleet,
        grid_entity=cfg[CONF_GRID_ENTITY],
        import_positive=cfg.get(CONF_IMPORT_POSITIVE, True),
        pv_entity=cfg.get(CONF_PV_ENTITY),
    )
    coordinator.config_entry = entry
    return coordinator
```

- [ ] **Step 4: Run the suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest -q`
Expected: all green (the existing `test_init`/`test_entities` entries — miners in `data`, `power_targets_w` present, a fleet-states path set — still load via the file branch).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/coordinator.py tests/test_coordinator.py
git commit -m "feat: coordinator builds from dynamic miners + generated/override matrix"
```

---

### Task 5: Config + options flow rewrite (dynamic miners) + remove `DEFAULT_MINERS`

**Files:**
- Rewrite: `custom_components/pv_surplus_mining/config_flow.py`
- Modify: `custom_components/pv_surplus_mining/const.py` (delete `DEFAULT_MINERS`)
- Modify: `custom_components/pv_surplus_mining/translations/en.json`
- Test: `tests/test_config_flow.py` (rewrite)

**Interfaces:**
- Consumes: `miner.parse_power_constraints`/`AioBraiinsClient`/`MinerConfig`, `miner_list.build_miner`/`recompute_priorities`, `fleet_states.load_fleet_states`/`validate_fleet_states`, `ControlConfig`.
- Produces: `PvSurplusConfigFlow` (VERSION 1 here; bumped to 2 in Task 6 with the migration) — `async_step_user` (hub + first miner basics) → `async_step_miner_detail` (auto-detect, confirm) → create entry with everything in `options`, `data={}`. `PvSurplusOptionsFlow` — `async_step_init` menu → `add_miner`/`add_detail`, `edit_miner`/`edit_detail`, `remove_miner`, `hub`, `tuning`. Module fn `_detect(hass, name, ip, password) -> dict`.

- [ ] **Step 1: Delete `DEFAULT_MINERS` from `const.py`**

Delete the `DEFAULT_MINERS = [...]` block (it is now unused — `config_flow.py` is rewritten below to not import it).

- [ ] **Step 2: Replace `custom_components/pv_surplus_mining/config_flow.py` entirely**

```python
"""Config + options flow for pv_surplus_mining (dynamic miners)."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BATTERY_ENTITY, CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY,
    CONF_IMPORT_POSITIVE, CONF_MINERS, CONF_PV_ENTITY, DOMAIN,
)
from .errors import ConfigError
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig, parse_power_constraints
from .miner_list import build_miner, recompute_priorities
from .models import ControlConfig

_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_CONTROL_KEYS = list(ControlConfig.model_fields.keys())


def _hub_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_GRID_ENTITY, default=d.get(CONF_GRID_ENTITY)): _SENSOR,
        vol.Required(CONF_IMPORT_POSITIVE, default=d.get(CONF_IMPORT_POSITIVE, True)): bool,
        vol.Optional(CONF_PV_ENTITY, default=d.get(CONF_PV_ENTITY) or ""): str,
        vol.Optional(CONF_BATTERY_ENTITY, default=d.get(CONF_BATTERY_ENTITY) or ""): str,
        vol.Optional(CONF_FLEET_STATES_PATH, default=d.get(CONF_FLEET_STATES_PATH) or ""): str,
    })


def _basics_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("name", default=d.get("name", "")): str,
        vol.Required("ip", default=d.get("ip", "")): str,
        vol.Required("password", default=""): str,
    })


def _detail_schema(d: dict) -> vol.Schema:
    return vol.Schema({
        vol.Required("model", default=d.get("model", "")): str,
        vol.Required("min_power_w", default=int(d.get("min_power_w", 0) or 0)): int,
        vol.Required("max_power_w", default=int(d.get("max_power_w", 0) or 0)): int,
        vol.Required("default_power_w", default=int(d.get("default_power_w", 0) or 0)): int,
    })


def _tuning_schema(opts: dict) -> vol.Schema:
    cur = {**ControlConfig().model_dump(), **{k: opts[k] for k in _CONTROL_KEYS if k in opts}}
    return vol.Schema({
        vol.Required(k, default=cur[k]): (bool if isinstance(cur[k], bool) else (int if isinstance(cur[k], int) else float))
        for k in _CONTROL_KEYS
    })


async def _detect(hass, name: str, ip: str, password: str) -> dict:
    """Best-effort auto-detect of model + power range. Returns detail-form defaults;
    leaves zeros/blanks on any failure so the user fills them in manually."""
    out = {"name": name, "model": "", "min_power_w": 0, "max_power_w": 0, "default_power_w": 0}
    session = async_get_clientsession(hass)
    cfg = MinerConfig(id="probe", model="", ip=ip, priority=1, min_power_w=0, max_power_w=100000)
    client = AioBraiinsClient(cfg, password, session)
    try:
        await client.login()
        details = await client.get_miner_details()
        out["model"] = details.get("platform") or details.get("miner_identity") or ""
        rng = parse_power_constraints(await client.get_constraints())
        if rng:
            out["min_power_w"], out["max_power_w"], _ = rng
        cur = (await client.get_tuner_state()).power_target_w
        if cur:
            out["default_power_w"] = int(cur)
        if not out["default_power_w"] and out["max_power_w"]:
            out["default_power_w"] = (out["min_power_w"] + out["max_power_w"]) // 2
    except Exception:  # noqa: BLE001 - detection is best-effort; manual entry on failure
        pass
    return out


class PvSurplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1   # bumped to 2 in Task 6, together with async_migrate_entry

    def __init__(self) -> None:
        self._hub: dict = {}
        self._basics: dict = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            self._hub = {
                CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                CONF_FLEET_STATES_PATH: user_input.get(CONF_FLEET_STATES_PATH) or "",
            }
            self._basics = {"name": user_input["name"], "ip": user_input["ip"], "password": user_input["password"]}
            return await self.async_step_miner_detail()
        schema = _hub_schema({}).extend(dict(_basics_schema({}).schema))
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_miner_detail(self, user_input: dict[str, Any] | None = None):
        if user_input is None:
            d = await _detect(self.hass, self._basics["name"], self._basics["ip"], self._basics["password"])
            return self.async_show_form(step_id="miner_detail", data_schema=_detail_schema(d))
        miner = build_miner(self._basics["name"], self._basics["ip"], self._basics["password"],
                            user_input["model"], user_input["min_power_w"], user_input["max_power_w"],
                            user_input["default_power_w"], taken_ids=set())
        miners = recompute_priorities([miner])
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        options = {**self._hub, CONF_MINERS: miners, **ControlConfig().model_dump()}
        return self.async_create_entry(title="PV-Surplus Mining", data={}, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PvSurplusOptionsFlow(config_entry)


class PvSurplusOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry
        self._basics: dict = {}
        self._edit_id: str | None = None

    def _config(self) -> dict:
        return {**self.config_entry.data, **(self.config_entry.options or {})}

    def _miners(self) -> list[dict]:
        return [dict(m) for m in self._config().get(CONF_MINERS, [])]

    async def _save(self, updates: dict):
        cfg = self._config()
        cfg.update(updates)
        return self.async_create_entry(title="", data=cfg)

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init",
            menu_options=["add_miner", "edit_miner", "remove_miner", "hub", "tuning"])

    async def async_step_add_miner(self, user_input=None):
        if user_input is None:
            return self.async_show_form(step_id="add_miner", data_schema=_basics_schema({}))
        self._basics = {"name": user_input["name"], "ip": user_input["ip"], "password": user_input["password"]}
        return await self.async_step_add_detail()

    async def async_step_add_detail(self, user_input=None):
        if user_input is None:
            d = await _detect(self.hass, self._basics["name"], self._basics["ip"], self._basics["password"])
            return self.async_show_form(step_id="add_detail", data_schema=_detail_schema(d))
        miners = self._miners()
        new = build_miner(self._basics["name"], self._basics["ip"], self._basics["password"],
                          user_input["model"], user_input["min_power_w"], user_input["max_power_w"],
                          user_input["default_power_w"], taken_ids={m["id"] for m in miners})
        miners.append(new)
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_edit_miner(self, user_input=None):
        ids = [m["id"] for m in self._miners()]
        if user_input is None:
            return self.async_show_form(step_id="edit_miner",
                data_schema=vol.Schema({vol.Required("miner"): vol.In(ids)}))
        self._edit_id = user_input["miner"]
        return await self.async_step_edit_detail()

    async def async_step_edit_detail(self, user_input=None):
        miners = self._miners()
        m = next(x for x in miners if x["id"] == self._edit_id)
        if user_input is None:
            return self.async_show_form(step_id="edit_detail", data_schema=_detail_schema(m))
        m.update(model=user_input["model"], min_power_w=user_input["min_power_w"],
                 max_power_w=user_input["max_power_w"], default_power_w=user_input["default_power_w"])
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_remove_miner(self, user_input=None):
        ids = [m["id"] for m in self._miners()]
        if user_input is None:
            return self.async_show_form(step_id="remove_miner",
                data_schema=vol.Schema({vol.Required("miner"): vol.In(ids)}))
        miners = [m for m in self._miners() if m["id"] != user_input["miner"]]
        return await self._save({CONF_MINERS: recompute_priorities(miners)})

    async def async_step_hub(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            path = user_input.get(CONF_FLEET_STATES_PATH) or ""
            if path:
                try:
                    validate_fleet_states(load_fleet_states(path), {m["id"] for m in self._miners()})
                except ConfigError:
                    errors["base"] = "bad_fleet_states"
            if not errors:
                return await self._save({
                    CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                    CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                    CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                    CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                    CONF_FLEET_STATES_PATH: path,
                })
        return self.async_show_form(step_id="hub", data_schema=_hub_schema(self._config()), errors=errors)

    async def async_step_tuning(self, user_input=None):
        if user_input is not None:
            return await self._save(user_input)
        return self.async_show_form(step_id="tuning", data_schema=_tuning_schema(self.config_entry.options or {}))
```

- [ ] **Step 3: Replace `tests/test_config_flow.py` entirely** (patch `_detect` so no real aiohttp/session is created)

```python
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

DETECTED = {"name": "S21+", "model": "Antminer S21+", "min_power_w": 2457, "max_power_w": 6435, "default_power_w": 3878}


def _entry(miners):
    return MockConfigEntry(domain=DOMAIN, data={}, options={
        CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True, CONF_MINERS: miners,
    })


async def test_initial_flow_creates_entry_with_one_miner(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    with patch("custom_components.pv_surplus_mining.config_flow._detect",
               AsyncMock(return_value=DETECTED)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True,
            "name": "S21+", "ip": "10.0.0.5", "password": "pw",
        })
        assert result["step_id"] == "miner_detail"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {
            "model": "Antminer S21+", "min_power_w": 2457, "max_power_w": 6435, "default_power_w": 3878,
        })
    assert result["type"] is FlowResultType.CREATE_ENTRY
    miners = result["options"][CONF_MINERS]
    assert len(miners) == 1 and miners[0]["id"] == "s21" and miners[0]["default_power_w"] == 3878


async def test_options_add_and_remove_miner(hass):
    entry = _entry([{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw", "username": "root",
                     "min_power_w": 800, "max_power_w": 6435, "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}])
    entry.add_to_hass(hass)

    # add
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "add_miner"})
    result = await hass.config_entries.options.async_configure(result["flow_id"],
        {"name": "B", "ip": "10.0.0.2", "password": "pw"})
    with patch("custom_components.pv_surplus_mining.config_flow._detect",
               AsyncMock(return_value={"name": "B", "model": "m", "min_power_w": 900, "max_power_w": 6435, "default_power_w": 3100})):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {})  # detail step shown via _detect
    # detail form now displayed; submit it
    result = await hass.config_entries.options.async_configure(result["flow_id"],
        {"model": "m", "min_power_w": 900, "max_power_w": 6435, "default_power_w": 3100})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    ids = {m["id"] for m in result["data"][CONF_MINERS]}
    assert ids == {"a", "b"}

    # remove "a"
    entry2 = _entry(result["data"][CONF_MINERS]); entry2.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry2.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "remove_miner"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"miner": "a"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert {m["id"] for m in result["data"][CONF_MINERS]} == {"b"}
```

> Note: the exact sequence of `async_configure` calls for the two-step add (basics → detail) depends on the installed HA flow API. The implementer should verify the step transitions with `result["step_id"]` and adjust the test's call sequence to match (the assertions that matter: after add, ids == {"a","b"}; after remove, == {"b"}). Patch `_detect` in every test that reaches a detail step so no real client session is created.

- [ ] **Step 4: Update `translations/en.json`** — add `config.step` entries for `user`, `miner_detail` and `options.step` entries for `init` (menu), `add_miner`, `add_detail`, `edit_miner`, `edit_detail`, `remove_miner`, `hub`, `tuning`, plus `config.error`/`options.error` `cannot_connect` and `bad_fleet_states`. Field labels for `name`/`ip`/`password`/`model`/`min_power_w`/`max_power_w`/`default_power_w`/`miner` and the hub/grid keys. (Use the existing file's structure; every `step_id` and field key used above needs a label so the UI isn't blank.)

- [ ] **Step 5: Run the suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest -q`
Expected: all green (including the rewritten config-flow tests; `test_init`/`test_entities` unaffected — they build entries directly and don't use `DEFAULT_MINERS`).

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_surplus_mining/config_flow.py custom_components/pv_surplus_mining/const.py custom_components/pv_surplus_mining/translations/en.json tests/test_config_flow.py
git commit -m "feat: dynamic add/edit/remove miners via config+options flow; drop DEFAULT_MINERS"
```

---

### Task 6: Config-entry migration (V1 → V2)

**Files:**
- Modify: `custom_components/pv_surplus_mining/__init__.py` (add `async_migrate_entry`)
- Test: `tests/test_migration.py`

**Interfaces:**
- Consumes: `recompute_priorities` (Task 3), const keys.
- Produces: `async_migrate_entry(hass, entry) -> bool`. Converts a VERSION 1 entry (miners in `entry.data[CONF_MINERS]`, hub keys + thresholds split between data/options) into the VERSION 2 shape (everything in `entry.options`, `data={}`), deriving each miner's `default_power_w` from the old `power_targets_w["normal"]` (else `max_power_w`), and recomputing priorities.

- [ ] **Step 1: Write failing test — `tests/test_migration.py`**

```python
from custom_components.pv_surplus_mining import async_migrate_entry
from custom_components.pv_surplus_mining.const import CONF_GRID_ENTITY, CONF_MINERS, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_migrate_v1_to_v2(hass):
    v1 = MockConfigEntry(domain=DOMAIN, version=1, data={
        CONF_MINERS: [
            {"id": "s21plus_01", "model": "Antminer S21+", "priority": 1, "min_power_w": 2457, "max_power_w": 6435,
             "power_targets_w": {"normal": 3878}, "command_cooldown_sec": 120, "username": "root",
             "ip": "10.0.0.5", "password": "pw"},
        ],
        CONF_GRID_ENTITY: "sensor.grid", "grid_import_positive": True, "fleet_states_path": "",
    }, options={"loop_interval_s": 10})
    v1.add_to_hass(hass)

    assert await async_migrate_entry(hass, v1) is True
    assert v1.version == 2
    assert v1.data == {}
    miners = v1.options[CONF_MINERS]
    assert miners[0]["id"] == "s21plus_01" and miners[0]["default_power_w"] == 3878
    assert v1.options[CONF_GRID_ENTITY] == "sensor.grid"
    assert v1.options["loop_interval_s"] == 10
```

- [ ] **Step 2: Run to verify it fails**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_migration.py -v`
Expected: ImportError (`async_migrate_entry` not defined).

- [ ] **Step 3: Bump the flow version + add `async_migrate_entry`**

In `config_flow.py`, change `class PvSurplusConfigFlow(...): VERSION = 1` to `VERSION = 2`.

In `__init__.py`, add the import `from .const import CONF_MINERS` (extend the existing const import) and `from .miner_list import recompute_priorities`, then add (shape-robust — handles a v1 entry whether its miners are in `data` (v0.1.0) or already in `options`):

```python
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
```

> Bumping `VERSION` to 2 here (not in Task 5) means the v1 `MockConfigEntry`s in `test_init.py`/`test_entities.py` are now migrated transparently on setup; they must still pass (their miners move to options, `default_power_w` derived from `power_targets_w["normal"]`, their `fleet_states_path` preserved → coordinator's file branch). Run the FULL suite, not just `test_migration.py`.

- [ ] **Step 4: Run the test + full suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_migration.py -v` then `... -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/__init__.py tests/test_migration.py
git commit -m "feat: migrate v1 config entries to v2 (config moved to options)"
```

---

### Task 7: Lovelace dashboard + README section

**Files:**
- Create: `dashboards/pv-surplus-mining.yaml`
- Modify: `README.md` (add a "Dashboard" section)
- Test: `tests/test_dashboard.py`

**Interfaces:** none (YAML + docs). The dashboard references the integration's fixed entities; per-miner cards are a documented template (in a YAML comment).

> Entity-id note: entities use `has_entity_name` + a device named "PV-Surplus Mining", so an entity_id is the slug of *device name + entity name*, e.g. entity name "Grid power" → `sensor.pv_surplus_mining_grid_power`, "Grid power (avg)" → `sensor.pv_surplus_mining_grid_power_avg`, per-miner "`<id>` power" → `sensor.pv_surplus_mining_<id>_power`. The implementer MUST confirm the exact ids from the harness (the test below enforces it) and correct the YAML if HA slugifies differently.

- [ ] **Step 1: Create `dashboards/pv-surplus-mining.yaml`**

```yaml
title: PV-Surplus Mining
views:
  - title: Mining
    path: mining
    cards:
      - type: entities
        title: Controls
        entities:
          - entity: switch.pv_surplus_mining_normal_mode
            name: Normal mode (24/7)
          - entity: switch.pv_surplus_mining_automation_enabled
            name: Automation enabled
          - entity: switch.pv_surplus_mining_emergency_stop
            name: Emergency stop
          - entity: switch.pv_surplus_mining_manual_override
            name: Manual override
          - entity: number.pv_surplus_mining_manual_state
          - entity: number.pv_surplus_mining_max_state
      - type: gauge
        entity: sensor.pv_surplus_mining_grid_power
        name: Grid power (+import / -export)
        min: -12000
        max: 12000
      - type: entities
        title: Status
        entities:
          - entity: sensor.pv_surplus_mining_grid_power_avg
          - entity: sensor.pv_surplus_mining_fleet_state
          - entity: sensor.pv_surplus_mining_target_state
          - entity: sensor.pv_surplus_mining_max_available_state
      - type: history-graph
        title: Surplus tracking (24 h)
        hours_to_show: 24
        entities:
          - entity: sensor.pv_surplus_mining_grid_power
          - entity: sensor.pv_surplus_mining_fleet_state
# --- Per-miner card template (copy one block per miner) ----------------------
# Per-miner sensors are named sensor.pv_surplus_mining_<id>_power and
# sensor.pv_surplus_mining_<id>_temperature, where <id> is the slug of the miner
# name you set when adding it. Add under `cards:` above, replacing <id> and <name>:
#
#      - type: entities
#        title: <name>
#        entities:
#          - entity: sensor.pv_surplus_mining_<id>_power
#          - entity: sensor.pv_surplus_mining_<id>_temperature
```

- [ ] **Step 2: Write the test — `tests/test_dashboard.py`** (sets the integration up, then asserts every entity the dashboard references actually exists)

```python
import pathlib
import yaml
from aioresponses import aioresponses
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

DASH = pathlib.Path("dashboards/pv-surplus-mining.yaml")


def _collect(node, out):
    if isinstance(node, dict):
        if isinstance(node.get("entity"), str):
            out.add(node["entity"])
        for v in node.values():
            _collect(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect(v, out)


def _mock_miner(m, ip="10.0.0.5"):
    base = f"http://{ip}/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details", payload={"status": "online"}, repeat=True)
    m.get(f"{base}/miner/stats", payload={"power": {"approx": 1400}, "temp_max_c": 60}, repeat=True)
    m.get(f"{base}/performance/tuner-state", payload={"power_target": {"watt": 1400}}, repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)
    m.put(f"{base}/actions/pause", payload=True, repeat=True)
    m.put(f"{base}/actions/resume", payload=True, repeat=True)


async def test_dashboard_parses_and_referenced_entities_exist(hass):
    hass.states.async_set("sensor.grid", "100")
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        CONF_GRID_ENTITY: "sensor.grid", CONF_IMPORT_POSITIVE: True,
        CONF_MINERS: [{"id": "m1", "name": "M1", "model": "x", "ip": "10.0.0.5", "password": "pw",
                       "username": "root", "min_power_w": 800, "max_power_w": 6435,
                       "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    dash = yaml.safe_load(DASH.read_text())
    refs = set()
    _collect(dash, refs)
    created = set(hass.states.async_entity_ids())
    missing = {e for e in refs if e not in created}
    assert not missing, f"dashboard references entities that do not exist: {sorted(missing)}"
```

> The test stands the integration up with one miner via the mocked Braiins API, then asserts every `entity:` the dashboard YAML references exists in `hass.states`. If a fixed-card entity_id is wrong, this fails — fix the YAML to the real id (inspect `hass.states.async_entity_ids()`). Per-miner cards live in a YAML comment, so they are not parsed/checked.

- [ ] **Step 3: Run the test — fix the dashboard entity_ids to match reality**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest tests/test_dashboard.py -v`
Expected: PASS. If it reports missing entities, correct those ids in `dashboards/pv-surplus-mining.yaml` to the ones the harness actually created (inspect `hass.states.async_entity_ids()`), then re-run.

- [ ] **Step 4: Add a "Dashboard" section to `README.md`**

```markdown
## Dashboard

A ready-made dashboard is in [`dashboards/pv-surplus-mining.yaml`](dashboards/pv-surplus-mining.yaml).
Add it via **Settings → Dashboards → ⋮ → New dashboard from YAML** (or paste the
cards into an existing dashboard in YAML edit mode). It shows the mode/control
switches, a grid-power gauge (+import / −export), fleet/target/max-available
state, and a 24 h surplus-tracking graph. Per-miner power/temperature cards are
a copy-per-miner template in the YAML's trailing comment — duplicate one block
per miner, replacing `<id>` with your miner's slug.
```

- [ ] **Step 5: Run the full suite + commit**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest -q`
Expected: all green.

```bash
git add dashboards/pv-surplus-mining.yaml tests/test_dashboard.py README.md
git commit -m "feat: ship Lovelace dashboard + docs"
```

---

### Task 8: Version bump to 0.2.0

**Files:**
- Modify: `custom_components/pv_surplus_mining/manifest.json`

**Interfaces:** none.

- [ ] **Step 1: Bump the version in `manifest.json`**

Change `"version": "0.1.0"` to `"version": "0.2.0"`. Keep the keys sorted (domain, name, then alphabetical) so hassfest stays green.

- [ ] **Step 2: Run the full suite**

Run: `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest -q`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add custom_components/pv_surplus_mining/manifest.json
git commit -m "chore: bump version to 0.2.0"
```

> The `v0.2.0` GitHub release/tag is cut by the controller after the branch is merged and CI is green (not a code step).

---

## Notes for the executor

- **Run from repo root** with `/home/nick/projects/pv-surplus-mining-ha/.venv/bin/python -m pytest`. CI runs Python 3.13.
- **Do not touch** `control/decision.py`, `control/loop.py`, the `MinerController` safety/pause logic, or the coordinator's `_async_update_data` — this version only reshapes configuration, the matrix source, the client's read methods, the flow, and the dashboard.
- **Patch `_detect`** in every config-flow test that reaches a miner-detail step (`AsyncMock`) so no real aiohttp client session is created in tests.
- **HA flow API drift:** the exact `async_configure` call sequence and result keys (`step_id`, `type`, `data`/`options`) can vary by HA version. Assert on `result["step_id"]`/`result["type"]` and adjust call sequences to match; the binding assertions are the resulting miner lists, not the intermediate transitions.
- The `pytest-homeassistant-custom-component` cleanup check tolerates the c-ares `_run_safe_shutdown_loop` thread only on the pinned (Py3.13) version — keep tests on that interpreter.
