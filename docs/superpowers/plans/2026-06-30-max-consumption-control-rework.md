# Max-consumption Control Rework Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the fleet reliably consume PV surplus — survive restarts engaged, and run the most efficient miner(s) that fit the surplus (any miner alone, S19j soaking what the S21+ can't) instead of stranding ~2 kW on a single pilot miner.

**Architecture:** Keep the proven pipeline (sensors → loop → decision → fleet → miners) and the snap-to-surplus + dwell + safety machinery untouched. Change four things: (1) persist operator-control state across restarts; (2) replace the matrix *generator* with an efficiency-aware "fill the surplus" ladder; (3) add snap hysteresis so miner swaps don't flap; (4) surface engaged/observe-only status. Pure layers (generator, loop) stay testable without HA.

**Tech Stack:** Python 3.13, Home Assistant custom integration, pydantic models, pytest + `pytest-homeassistant-custom-component`, `aioresponses` for Braiins REST.

**Spec:** `docs/superpowers/specs/2026-06-30-max-consumption-control-rework-design.md`

## Global Constraints

- **Tests MUST run on Python 3.13** (`pip install -r requirements_test.txt`, then `python -m pytest`). pytest config lives in `pyproject.toml` (`asyncio_mode=auto`, `-q`).
- **`manifest.json` `version` is the release source of truth** (`pyproject.toml` version is stale/unused). Target version: **0.5.0**.
- **Editable config lives in `entry.options`** (post-v2), merged as `{**data, **options}`. New persisted *operator* state goes in a dedicated `Store`, NOT in options.
- **`ControlConfig` (models.py) is the single source of truth** for every tunable and its default.
- **State 0 = all miners sleep is mandatory** in every fleet-state matrix; every miner must appear in every state.
- **No feed-in tariff:** the control objective is "most hashes without importing", keeping only `export_reserve_w` of export.
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit. Frequent commits.

---

## Phase 1 — Reliability (persistence + visibility)

Phase 1 ships independently: it stops the "silently off after every reboot/update" failure and makes a disengaged controller visible. It does not change ramp behaviour.

---

### Task 1: Persist operator state across restarts (load + apply at build)

**Files:**
- Create: `custom_components/pv_surplus_mining/store.py`
- Modify: `custom_components/pv_surplus_mining/coordinator.py` (add `_operator_state`/`_apply_operator_state`; load+apply in `async_build_coordinator`)
- Modify: `custom_components/pv_surplus_mining/models.py:41` (flip `enabled_default` default to `True`)
- Modify: `tests/test_entities.py` (make the two existing tests pin `enabled_default=False` so they stay deterministic)
- Test: `tests/test_persistence.py` (new)

**Interfaces:**
- Produces: `store.operator_store(hass, entry_id) -> homeassistant.helpers.storage.Store` and `store.STORE_VERSION = 1`.
- Produces: `PvSurplusCoordinator._operator_state() -> dict` and `PvSurplusCoordinator._apply_operator_state(saved: dict | None) -> None`.
- Consumes (Task 2): `_operator_state()` shape for the save path.

- [ ] **Step 1: Write the failing test for the store helper + collect/apply roundtrip**

Add to `tests/test_persistence.py`:

```python
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import DOMAIN
from custom_components.pv_surplus_mining.coordinator import PvSurplusCoordinator
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.miner import MinerConfig
from custom_components.pv_surplus_mining.models import ControlConfig, FleetStateTarget, MinerStatus, CommandResult


class StubCtrl:
    def __init__(self, mid, prio=1, min_power_w=1000, max_power_w=4000):
        self.cfg = MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio,
                               min_power_w=min_power_w, max_power_w=max_power_w)
        self.available = True
        self.paused = True
    async def get_status(self):
        return MinerStatus(miner_id=self.cfg.id, online=True, paused=self.paused, available=True)


def _coord(hass):
    a = StubCtrl("a")
    states = {0: {"a": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000)}}
    fleet = FleetController({"a": a}, states)
    cfg = ControlConfig(loop_interval_s=10, avg_window_s=10)
    return PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.g", import_positive=True)


async def test_operator_store_roundtrip(hass):
    from custom_components.pv_surplus_mining.store import operator_store
    s = operator_store(hass, "abc")
    await s.async_save({"auto_enabled": True})
    assert (await operator_store(hass, "abc").async_load()) == {"auto_enabled": True}


async def test_collect_and_apply_operator_state(hass):
    c = _coord(hass)
    c.auto_enabled = True
    c.manual_state = 1
    c.miner_enabled["a"] = False
    snapshot = c._operator_state()

    c2 = _coord(hass)
    assert c2.auto_enabled is False          # fresh coordinator built with non-persisted config
    c2._apply_operator_state(snapshot)
    assert c2.auto_enabled is True
    assert c2.manual_state == 1
    assert c2.miner_enabled["a"] is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: FAIL — `ModuleNotFoundError: ... store` and `AttributeError: ... _operator_state`.

- [ ] **Step 3: Create the store module**

Create `custom_components/pv_surplus_mining/store.py`:

```python
"""Persisted operator-control state (survives restarts/reloads), keyed per entry."""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORE_VERSION = 1


def operator_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORE_VERSION, f"{DOMAIN}.{entry_id}.operator")
```

- [ ] **Step 4: Add collect/apply to the coordinator**

In `custom_components/pv_surplus_mining/coordinator.py`, add these methods to `PvSurplusCoordinator` (e.g. right after `_sync_loop_state_power`):

```python
    def _operator_state(self) -> dict:
        """The operator-control state to persist across restarts."""
        return {
            "auto_enabled": self.auto_enabled,
            "normal_mode": self.normal_mode,
            "manual_override": self.manual_override,
            "pv_mode": self.pv_mode,
            "manual_state": self.manual_state,
            "max_state": self.max_state,
            "miner_enabled": dict(self.miner_enabled),
            "miner_power_w": dict(self.miner_power_w),
            "miner_max_w": dict(self.miner_max_w),
        }

    def _apply_operator_state(self, saved: dict | None) -> None:
        """Restore persisted operator state onto the coordinator (before first tick)."""
        if not saved:
            return
        for key in ("auto_enabled", "normal_mode", "manual_override", "pv_mode"):
            if key in saved:
                setattr(self, key, bool(saved[key]))
        for key in ("manual_state", "max_state"):
            if key in saved:
                setattr(self, key, int(saved[key]))
        for attr, cast in (("miner_enabled", bool), ("miner_power_w", int), ("miner_max_w", int)):
            if isinstance(saved.get(attr), dict):
                cur = getattr(self, attr)
                for mid, v in saved[attr].items():
                    if mid in cur:
                        cur[mid] = cast(v)
        self._rebuild_fleet_states()
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Write the failing build-time restore test**

Append to `tests/test_persistence.py`:

```python
def _entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={
        "grid_entity": "sensor.grid", "grid_import_positive": True,
        "miners": [{"id": "a", "name": "A", "model": "m", "ip": "10.0.0.1", "password": "pw",
                    "username": "root", "min_power_w": 800, "max_power_w": 6435,
                    "default_power_w": 3000, "command_cooldown_sec": 120, "priority": 1}],
    })
    entry.add_to_hass(hass)
    return entry


async def test_build_restores_persisted_auto_enabled(hass, hass_storage):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    from custom_components.pv_surplus_mining.store import STORE_VERSION
    entry = _entry(hass)
    hass_storage[f"{DOMAIN}.{entry.entry_id}.operator"] = {
        "version": STORE_VERSION, "minor_version": 1, "key": f"{DOMAIN}.{entry.entry_id}.operator",
        "data": {"auto_enabled": True, "manual_state": 0},
    }
    coordinator = await async_build_coordinator(hass, entry)
    assert coordinator.auto_enabled is True   # restored, not enabled_default


async def test_build_defaults_on_for_fresh_install(hass, hass_storage):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    entry = _entry(hass)   # no store entry seeded
    coordinator = await async_build_coordinator(hass, entry)
    assert coordinator.auto_enabled is True   # enabled_default flipped to True
```

- [ ] **Step 7: Run to verify it fails**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: FAIL — `test_build_restores_persisted_auto_enabled` fails (auto_enabled is False) and `test_build_defaults_on_for_fresh_install` fails (enabled_default still False).

- [ ] **Step 8: Flip the default and wire load+apply into the builder**

In `custom_components/pv_surplus_mining/models.py:41`, change:

```python
    enabled_default: bool = False
```
to:
```python
    enabled_default: bool = True
```

In `custom_components/pv_surplus_mining/coordinator.py`, add the import near the other `.` imports:

```python
from .store import operator_store
```

Then in `async_build_coordinator`, replace the tail (the lines that set `_matrix_generated` and `config_entry`, then `return coordinator`) with:

```python
    coordinator._matrix_generated = not (path and Path(path).exists())
    coordinator.config_entry = entry
    saved = await operator_store(hass, entry.entry_id).async_load()
    coordinator._apply_operator_state(saved)
    coordinator._saved_operator = coordinator._operator_state()
    return coordinator
```

In `PvSurplusCoordinator.__init__`, add near the other instance attributes (e.g. after `self._matrix_generated = True`):

```python
        self._saved_operator: dict | None = None
```

- [ ] **Step 9: Keep the existing entity tests deterministic**

In `tests/test_entities.py`, the two tests assert the fresh default is off. Pin it explicitly so the flipped default doesn't break them. In `_entry_data`, add `enabled_default`:

```python
def _entry_data(path):
    return {
        CONF_MINERS: [{"id": "s21plus_01", "model": "S21+", "ip": "10.0.0.5", "priority": 1,
                       "min_power_w": 1400, "max_power_w": 4000, "password": "pw",
                       "power_targets_w": {"normal": 3000}}],
        CONF_GRID_ENTITY: "sensor.grid_power", CONF_IMPORT_POSITIVE: True,
        CONF_FLEET_STATES_PATH: str(path),
        "enabled_default": False,
    }
```

- [ ] **Step 10: Run the full suite to verify green**

Run: `python -m pytest tests/test_persistence.py tests/test_entities.py tests/test_coordinator.py -v`
Expected: PASS (new persistence tests pass; entity/coordinator tests still pass).

- [ ] **Step 11: Commit**

```bash
git add custom_components/pv_surplus_mining/store.py \
        custom_components/pv_surplus_mining/coordinator.py \
        custom_components/pv_surplus_mining/models.py \
        tests/test_persistence.py tests/test_entities.py
git commit -m "feat: persist operator state and restore it at build; default automation on"
```

---

### Task 2: Save operator state whenever it changes

**Files:**
- Modify: `custom_components/pv_surplus_mining/coordinator.py` (`_async_update_data`: persist on change)
- Test: `tests/test_persistence.py`

**Interfaces:**
- Consumes: `_operator_state()`, `operator_store()`, `self._saved_operator` (Task 1).

- [ ] **Step 1: Write the failing save test**

Append to `tests/test_persistence.py`:

```python
async def test_tick_persists_operator_change(hass, hass_storage):
    from custom_components.pv_surplus_mining.coordinator import async_build_coordinator
    entry = _entry(hass)
    coordinator = await async_build_coordinator(hass, entry)
    hass.states.async_set("sensor.grid", "0")

    coordinator.auto_enabled = False           # operator turns automation off
    await coordinator._async_update_data()
    await hass.async_block_till_done()

    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.operator"]["data"]
    assert stored["auto_enabled"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_persistence.py::test_tick_persists_operator_change -v`
Expected: FAIL — `KeyError` (nothing written) or stored value not updated.

- [ ] **Step 3: Persist on change at the end of the tick**

In `custom_components/pv_surplus_mining/coordinator.py`, in `_async_update_data`, immediately before the final `return {` add:

```python
        # Persist operator-control state so the controller resumes exactly as the
        # operator left it after any restart/reload (only writes when it changed).
        if getattr(self, "config_entry", None) is not None:
            state = self._operator_state()
            if state != self._saved_operator:
                self._saved_operator = state
                await operator_store(self.hass, self.config_entry.entry_id).async_save(state)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_persistence.py -v`
Expected: PASS (all persistence tests).

- [ ] **Step 5: Run the coordinator suite to confirm no regressions**

Run: `python -m pytest tests/test_coordinator.py -v`
Expected: PASS (the `_coord` helper sets no `config_entry`, so persistence is skipped there).

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_surplus_mining/coordinator.py tests/test_persistence.py
git commit -m "feat: save operator state on change each tick"
```

---

### Task 3: Surface engaged / observe-only status

**Files:**
- Create: `custom_components/pv_surplus_mining/binary_sensor.py`
- Modify: `custom_components/pv_surplus_mining/const.py:3` (add `"binary_sensor"` to `PLATFORMS`)
- Modify: `custom_components/pv_surplus_mining/coordinator.py` (add `"engaged"` to the data dict)
- Test: `tests/test_entities.py`

**Interfaces:**
- Produces: coordinator `data["engaged"]: bool`; entity `binary_sensor.pv_surplus_mining_controller_engaged` with `reason` attribute.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_entities.py`:

```python
async def test_engaged_binary_sensor_reports_status(hass, tmp_path):
    fleet_file = tmp_path / "fleet-states.yaml"; fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "100")
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        all_bs = [s.entity_id for s in hass.states.async_all("binary_sensor")]
        matches = [eid for eid in all_bs if "engaged" in eid]
        assert matches, f"No engaged binary_sensor; registered: {all_bs}"
        st = hass.states.get(matches[0])
        assert st.state == "off"                       # enabled_default=False in _entry_data
        assert "reason" in st.attributes
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_entities.py::test_engaged_binary_sensor_reports_status -v`
Expected: FAIL — no `binary_sensor` entities registered.

- [ ] **Step 3: Expose `engaged` in coordinator data**

In `custom_components/pv_surplus_mining/coordinator.py`, in the `return {` dict at the end of `_async_update_data`, add a line (next to `"emergency": ...`):

```python
            "engaged": engaged,
```

- [ ] **Step 4: Create the binary_sensor platform**

Create `custom_components/pv_surplus_mining/binary_sensor.py`:

```python
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([_EngagedBinarySensor(coordinator)])


class _EngagedBinarySensor(PvSurplusEntity, BinarySensorEntity):
    """ON when the controller is actually commanding the fleet (auto / normal /
    manual-override / emergency); OFF means observe-only. `reason` attribute carries
    the controller's current decision reason."""

    def __init__(self, coordinator):
        super().__init__(coordinator, "engaged", "Controller engaged")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("engaged"))

    @property
    def extra_state_attributes(self) -> dict:
        return {"reason": (self.coordinator.data or {}).get("reason")}
```

- [ ] **Step 5: Register the platform**

In `custom_components/pv_surplus_mining/const.py:3`, change:

```python
PLATFORMS: list[str] = ["sensor", "switch", "number"]
```
to:
```python
PLATFORMS: list[str] = ["sensor", "switch", "number", "binary_sensor"]
```

- [ ] **Step 6: Run to verify it passes**

Run: `python -m pytest tests/test_entities.py -v`
Expected: PASS (engaged sensor + existing entity tests).

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_surplus_mining/binary_sensor.py \
        custom_components/pv_surplus_mining/const.py \
        custom_components/pv_surplus_mining/coordinator.py \
        tests/test_entities.py
git commit -m "feat: expose 'Controller engaged' binary_sensor with decision reason"
```

---

## Phase 2 — Consumption (fill ladder + hysteresis)

Phase 2 ships independently on top of Phase 1: it replaces the wasteful matrix generator so the fleet fills the surplus with the most efficient miner that fits.

---

### Task 4: Efficiency-aware "fill the surplus" generator

**Files:**
- Modify: `custom_components/pv_surplus_mining/fleet_states.py` (add `generate_surplus_fill_states`)
- Test: `tests/test_fleet_states_gen.py`

**Interfaces:**
- Produces: `generate_surplus_fill_states(miners: list[dict], step_w: int) -> dict[int, dict[str, FleetStateTarget]]`. Each miner dict: `{id, min_power_w, cap, efficiency_rank?}` (lower `efficiency_rank` = more efficient = filled first; absent ⇒ ranked by descending `min_power_w`). Returns state 0 = all sleep, totals monotonic non-decreasing, top state = every miner at its cap.
- Consumes (Task 5): this function, called by the coordinator.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fleet_states_gen.py`:

```python
from custom_components.pv_surplus_mining.fleet_states import generate_surplus_fill_states

FILL_MINERS = [
    {"id": "pp", "min_power_w": 817,  "cap": 3300, "efficiency_rank": 1},   # S19j Pro+
    {"id": "pr", "min_power_w": 944,  "cap": 3068, "efficiency_rank": 2},   # S19j Pro
    {"id": "s",  "min_power_w": 2457, "cap": 3878, "efficiency_rank": 0},   # S21+ (most efficient)
]


def test_fill_state_zero_all_sleep_and_validates():
    states = generate_surplus_fill_states(FILL_MINERS, step_w=200)
    assert all(t.action == "sleep" for t in states[0].values())
    for targets in states.values():
        assert set(targets) == {"pp", "pr", "s"}
    validate_fleet_states(states, {"pp", "pr", "s"})


def test_fill_totals_monotonic_and_top_is_all_caps():
    states = generate_surplus_fill_states(FILL_MINERS, step_w=200)
    totals = [_total(states[s]) for s in sorted(states)]
    assert totals == sorted(totals) and totals[0] == 0
    assert totals[-1] == 3300 + 3068 + 3878            # top = every miner at its cap
    caps = {m["id"]: m["cap"] for m in FILL_MINERS}
    for targets in states.values():
        for mid, t in targets.items():
            if t.action == "active":
                assert t.power_w <= caps[mid]


def test_fill_runs_efficient_miner_alone():
    """A budget that fits the S21+ (2457) but not pilot+S21+ runs the S21+ ALONE."""
    states = generate_surplus_fill_states(FILL_MINERS, step_w=200)
    s_alone = [sid for sid, tg in states.items()
               if tg["s"].action == "active"
               and tg["pp"].action == "sleep" and tg["pr"].action == "sleep"]
    assert s_alone, "expected at least one state with the S21+ running alone"
    # and the smallest such state's S21+ target is near its minimum
    sid = min(s_alone, key=lambda s: _total(states[s]))
    assert states[sid]["s"].power_w >= 2457


def test_fill_soaks_sub_s21_surplus_with_a_low_min_miner():
    """Below the S21+'s minimum, a less-efficient S19j ramps up to soak the surplus."""
    states = generate_surplus_fill_states(FILL_MINERS, step_w=200)
    # find a rung whose total is ~1500 W (between pilot min and S21+ min)
    rung = min(states, key=lambda s: abs(_total(states[s]) - 1500))
    tg = states[rung]
    assert tg["s"].action == "sleep"                       # S21+ can't start here
    assert any(tg[m].action == "active" for m in ("pp", "pr"))   # an S19j soaks it
    assert 1200 <= _total(tg) <= 1700


def test_fill_single_miner_ramps_min_to_cap():
    states = generate_surplus_fill_states([{"id": "x", "min_power_w": 1000, "cap": 2000}], step_w=500)
    totals = [_total(states[s]) for s in sorted(states)]
    assert totals[0] == 0 and totals[-1] == 2000
    assert all(states[s]["x"].action in ("sleep", "active") for s in states)


def test_fill_empty_fleet_is_state_zero_only():
    assert generate_surplus_fill_states([], step_w=200) == {0: {}}
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_fleet_states_gen.py -k fill -v`
Expected: FAIL — `ImportError: cannot import name 'generate_surplus_fill_states'`.

- [ ] **Step 3: Implement the generator**

In `custom_components/pv_surplus_mining/fleet_states.py`, add (after `generate_s21_priority_states`, before `generate_fleet_states`):

```python
def generate_surplus_fill_states(miners: list[dict], step_w: int) -> dict[int, dict[str, "FleetStateTarget"]]:
    """Efficiency-aware 'fill the surplus' matrix.

    Each rung is the highest-hashrate miner allocation whose total power fits a
    given budget: load the most-efficient runnable miner toward its cap first,
    then the next, never running a miner below its minimum. Because exported
    energy earns nothing, any miner may run ALONE and a less-efficient miner
    soaks surplus the efficient one cannot (below its minimum or above its cap).

    miners: list of ``{id, min_power_w, cap, efficiency_rank?}``. Lower
    ``efficiency_rank`` = more efficient (filled first); when absent, miners are
    ranked by DESCENDING ``min_power_w`` (the high-minimum Antminers are the
    efficient ones). ``step_w`` sets the budget granularity.

    Returns ``{state_id: {miner_id: FleetStateTarget}}`` with state 0 = all
    sleep, totals monotonic non-decreasing, every miner present in every state,
    and the top state = every miner at its cap.
    """
    if not miners:
        return {0: {}}
    ids = [m["id"] for m in miners]
    caps = {m["id"]: int(m["cap"]) for m in miners}
    mins = {m["id"]: int(m["min_power_w"]) for m in miners}

    def _rank(m):
        r = m.get("efficiency_rank")
        return (0, int(r)) if r is not None else (1, -int(m["min_power_w"]))
    order = sorted(miners, key=_rank)

    def allocate(budget: int) -> dict[str, int]:
        """Max-hashrate allocation with total <= budget (greedy by efficiency)."""
        remaining = budget
        alloc = {mid: 0 for mid in ids}
        for m in order:
            mid = m["id"]
            if remaining >= mins[mid]:
                p = min(caps[mid], remaining)
                alloc[mid] = p
                remaining -= p
        return alloc

    total_cap = sum(caps.values())
    budgets = list(range(0, total_cap + 1, max(1, step_w)))
    if budgets[-1] != total_cap:
        budgets.append(total_cap)

    seq: list[dict[str, int]] = []
    for b in budgets:
        a = allocate(b)
        if not seq or a != seq[-1]:
            seq.append(a)

    return {
        idx: {
            mid: (FleetStateTarget(action="active", power_w=int(w)) if w
                  else FleetStateTarget(action="sleep"))
            for mid, w in a.items()
        }
        for idx, a in enumerate(seq)
    }
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_fleet_states_gen.py -k fill -v`
Expected: PASS (all six fill tests).

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/fleet_states.py tests/test_fleet_states_gen.py
git commit -m "feat: efficiency-aware surplus-fill matrix generator"
```

---

### Task 5: Wire the coordinator to the fill generator (and retire the old one)

**Files:**
- Modify: `custom_components/pv_surplus_mining/miner.py:44-54` (add `efficiency_rank` to `MinerConfig`)
- Modify: `custom_components/pv_surplus_mining/coordinator.py` (use `generate_surplus_fill_states` in `_rebuild_fleet_states` and `async_build_coordinator`; pass `efficiency_rank`)
- Modify: `custom_components/pv_surplus_mining/fleet_states.py` (remove `generate_s21_priority_states`)
- Modify: `tests/test_fleet_states_gen.py` (remove the 5 `generate_s21_priority_states` tests)
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Consumes: `generate_surplus_fill_states` (Task 4).
- Produces: `MinerConfig.efficiency_rank: int | None`.

- [ ] **Step 1: Write the failing coordinator test**

Append to `tests/test_coordinator.py`:

```python
async def test_rebuild_uses_fill_generator_runs_s21_alone(hass):
    a = StubCtrl("pp", 1, paused=True, min_power_w=817, max_power_w=6435)
    b = StubCtrl("pr", 2, paused=True, min_power_w=944, max_power_w=6435)
    s = StubCtrl("s", 3, paused=True, min_power_w=2457, max_power_w=6435)
    s.cfg.efficiency_rank = 0; a.cfg.efficiency_rank = 1; b.cfg.efficiency_rank = 2
    fleet = FleetController({"pp": a, "pr": b, "s": s}, {0: {
        "pp": FleetStateTarget(action="sleep"), "pr": FleetStateTarget(action="sleep"),
        "s": FleetStateTarget(action="sleep")}})
    c = PvSurplusCoordinator(hass, ControlConfig(loop_interval_s=10, avg_window_s=10, fleet_state_step_w=200),
                             fleet, grid_entity="sensor.g", import_positive=True)
    c.miner_max_w = {"pp": 3300, "pr": 3068, "s": 3878}
    c._rebuild_fleet_states()
    # there is now a state running the S21+ alone (impossible with the old matrix)
    assert any(t["s"].action == "active" and t["pp"].action == "sleep" and t["pr"].action == "sleep"
               for t in c.fleet.states.values())
    assert c.fleet.state_power_total(max(c.fleet.states)) == 3300 + 3068 + 3878
```

(Note: `StubCtrl` in `test_coordinator.py` builds `MinerConfig` without `efficiency_rank`; the test sets `s.cfg.efficiency_rank` directly after construction, which requires Step 3's field.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_coordinator.py::test_rebuild_uses_fill_generator_runs_s21_alone -v`
Expected: FAIL — `ValidationError`/`AttributeError` on `efficiency_rank`, or no S21-alone state (old generator still wired).

- [ ] **Step 3: Add `efficiency_rank` to `MinerConfig`**

In `custom_components/pv_surplus_mining/miner.py`, in `class MinerConfig`, add the field (after `command_cooldown_sec`):

```python
    efficiency_rank: int | None = None
```

- [ ] **Step 4: Switch the coordinator's generator + plumb efficiency_rank**

In `custom_components/pv_surplus_mining/coordinator.py`:

Update the import line (currently importing the generators) to:

```python
from .fleet_states import (
    generate_fleet_states, generate_surplus_fill_states, load_fleet_states, validate_fleet_states,
)
```

In `_rebuild_fleet_states`, change the `gen` comprehension and the generate call:

```python
        gen = [
            {"id": c.cfg.id, "min_power_w": c.cfg.min_power_w,
             "cap": int(self.miner_max_w.get(mid) or c.cfg.power_targets_w.get("normal") or c.cfg.max_power_w),
             "efficiency_rank": c.cfg.efficiency_rank}
            for mid, c in self.fleet.miners.items() if self.miner_enabled.get(mid, True)
        ]
        states = generate_surplus_fill_states(gen, self.config.fleet_state_step_w) if gen else {0: {}}
```

In `async_build_coordinator`, update the `MinerConfig(...)` construction to pass `efficiency_rank=m.get("efficiency_rank")`, and the auto-generate branch:

```python
        gen = [{"id": m["id"], "min_power_w": m["min_power_w"], "cap": _default_w(m),
                "efficiency_rank": m.get("efficiency_rank")} for m in cfg[CONF_MINERS]]
        states = generate_surplus_fill_states(gen, control.fleet_state_step_w)
```

- [ ] **Step 5: Remove the retired generator and its tests**

In `custom_components/pv_surplus_mining/fleet_states.py`, delete the entire `generate_s21_priority_states` function.

In `tests/test_fleet_states_gen.py`, delete the top import of `generate_s21_priority_states`, the `S21_MINERS` list, and the five `test_s21_*` tests. Keep the `generate_fleet_states` tests and the `generate_surplus_fill_states` tests.

- [ ] **Step 6: Run the affected suites to verify green**

Run: `python -m pytest tests/test_fleet_states_gen.py tests/test_coordinator.py -v`
Expected: PASS — the new fill/coordinator tests pass; remaining `generate_fleet_states` tests pass; the deleted S21 tests are gone.

- [ ] **Step 7: Commit**

```bash
git add custom_components/pv_surplus_mining/miner.py \
        custom_components/pv_surplus_mining/coordinator.py \
        custom_components/pv_surplus_mining/fleet_states.py \
        tests/test_fleet_states_gen.py tests/test_coordinator.py
git commit -m "feat: drive the fleet matrix from the surplus-fill generator (retire S21-priority)"
```

---

### Task 6: Snap hysteresis (stop swap-boundary flapping)

**Files:**
- Modify: `custom_components/pv_surplus_mining/models.py` (add `snap_hysteresis_w`)
- Modify: `custom_components/pv_surplus_mining/control/loop.py:57-72` (`_surplus_target` uses the margin)
- Test: `tests/test_loop.py`

**Interfaces:**
- Produces: `ControlConfig.snap_hysteresis_w: int = 100`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_loop.py`:

```python
def test_snap_hysteresis_blocks_step_up_without_margin():
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=0, snap_hysteresis_w=200),
                          max_available_state=14, current_state=1)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000}
    loop.actual_draw_w = 0
    # budget = 0 + 2100 - 0 = 2100; stepping above current(1) needs 2000+200=2200 -> stay at 1
    loop.tick(-2100, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 1


def test_snap_hysteresis_allows_step_up_with_margin():
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=0, snap_hysteresis_w=200),
                          max_available_state=14, current_state=1)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000}
    loop.actual_draw_w = 0
    loop.tick(-2300, ControlInputs(auto_enabled=True))   # 2300 >= 2000+200
    assert loop.surplus_target_state == 2


def test_snap_hysteresis_does_not_bias_step_down():
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=0, snap_hysteresis_w=200),
                          max_available_state=14, current_state=3)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000}
    loop.actual_draw_w = 0
    # budget 2100; dropping to state 2 (2000<=2100) has no margin -> target 2
    loop.tick(-2100, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_loop.py -k hysteresis -v`
Expected: FAIL — `ValidationError`/`TypeError` (`snap_hysteresis_w` unknown), or wrong target (no margin applied).

- [ ] **Step 3: Add the config field**

In `custom_components/pv_surplus_mining/models.py`, in `ControlConfig`, add (e.g. after `fleet_state_step_w`):

```python
    # Extra export headroom (W) required to step to a HIGHER fleet state than the
    # current one. Damps flapping when the surplus hovers near a state boundary
    # (e.g. swapping a low-min S19j for the S21+ at its 2457 W minimum).
    snap_hysteresis_w: int = 100
```

- [ ] **Step 4: Apply the margin in `_surplus_target`**

In `custom_components/pv_surplus_mining/control/loop.py`, replace the `for`-loop body of `_surplus_target` so the step-up margin is applied:

```python
        budget = current_draw + export - self.config.export_reserve_w
        hys = self.config.snap_hysteresis_w
        best = 0
        for sid, total in sorted(self.state_power_w.items(), key=lambda kv: kv[1]):
            margin = hys if sid > self.current_state else 0
            if total <= budget - margin:
                best = sid
        return best
```

- [ ] **Step 5: Run the loop suite to verify it passes**

Run: `python -m pytest tests/test_loop.py -v`
Expected: PASS (new hysteresis tests + all existing surplus-target tests, which keep comfortable margins under the default 100).

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_surplus_mining/models.py \
        custom_components/pv_surplus_mining/control/loop.py tests/test_loop.py
git commit -m "feat: snap hysteresis to prevent miner-swap flapping at state boundaries"
```

---

### Task 7: Release — field-scenario regression, version bump, docs

**Files:**
- Test: `tests/test_fleet_states_gen.py` (field-scenario regression)
- Modify: `custom_components/pv_surplus_mining/manifest.json` (version → `0.5.0`)
- Modify: `CLAUDE.md` (gotchas: persistence + fill matrix)
- Modify: `README.md` (modes/safety: persistence + fill behaviour)

- [ ] **Step 1: Write the field-scenario regression test**

Append to `tests/test_fleet_states_gen.py`:

```python
def test_field_scenario_uses_surplus_instead_of_stranding_one_miner():
    """Regression for the live bug: ~2.7 kW available (705 W draw + ~2.0 kW export)
    with reserve 300 -> budget ~2.4 kW must drive WAY more than the old 817 W pilot,
    and the S21+ must be reachable alone as surplus grows."""
    states = generate_surplus_fill_states(FILL_MINERS, step_w=200)
    totals = {sid: _total(states[sid]) for sid in states}
    best_at_2400 = max(t for t in totals.values() if t <= 2400)
    assert best_at_2400 >= 2000, f"only soaks {best_at_2400} W of a ~2.4 kW budget"
    # S21+ comes online (alone) once the budget supports its 2457 W minimum
    assert any(states[s]["s"].action == "active" for s in states)
```

- [ ] **Step 2: Run to verify it passes (behaviour already implemented in Task 4)**

Run: `python -m pytest tests/test_fleet_states_gen.py::test_field_scenario_uses_surplus_instead_of_stranding_one_miner -v`
Expected: PASS.

- [ ] **Step 3: Run the entire suite**

Run: `python -m pytest`
Expected: PASS (whole suite green on Python 3.13).

- [ ] **Step 4: Bump the manifest version**

In `custom_components/pv_surplus_mining/manifest.json`, change `"version": "0.4.2"` to `"version": "0.5.0"`.

- [ ] **Step 5: Update CLAUDE.md gotchas**

In `CLAUDE.md`, under "Conventions & gotchas", add two bullets:

```markdown
- **Operator state persists** (`store.py`): `auto_enabled`, the mode switches, and
  per-miner enable/power/cap are saved to an HA `Store` and restored *before* the
  first control tick, so a restart/reload/options-edit no longer silently disables
  the controller. `enabled_default` is `True` (fresh installs run; persisted state
  wins thereafter). A `binary_sensor` "Controller engaged" exposes engaged vs
  observe-only plus the decision `reason`.
- **Matrix = surplus-fill, not S21-priority** (`generate_surplus_fill_states`): each
  rung is the highest-hashrate allocation that fits the budget — any miner may run
  ALONE and a less-efficient S19j soaks surplus the S21+ can't (below its min / above
  its cap). Ranking uses per-miner `efficiency_rank` (lower = more efficient), else
  descending `min_power_w`. `snap_hysteresis_w` adds step-up headroom so swaps near a
  boundary don't flap.
```

- [ ] **Step 6: Update README.md**

In `README.md`, in the section describing fleet/ramp ordering and modes, replace the description of the S21+-priority ramp with the surplus-fill behaviour (any miner can run alone; the most efficient miner that fits runs; S19j soaks sub-S21+ surplus; no feed-in ⇒ minimise export) and note that automation now survives restarts and a "Controller engaged" sensor shows when it's observe-only. Keep it to one short paragraph consistent with the surrounding style.

- [ ] **Step 7: Commit**

```bash
git add tests/test_fleet_states_gen.py custom_components/pv_surplus_mining/manifest.json CLAUDE.md README.md
git commit -m "feat: max-consumption control rework (v0.5.0)"
```

---

## Deferred (with rationale — surface to the user before implementing)

- **Ramp commitment** (spec Part 3, second half). Analysis during planning showed the existing `min_state_dwell_s` (≈ the tuner settle time / command cooldown) plus the import-gated ramp-down already prevent a mid-ramp/mid-swap *reversal*: a transient measured-draw under-read only drops `surplus_target` below `current_state`, which `decide()` resolves as **hold** (ramp-down needs sustained *import*, which isn't happening while a miner is still spinning up). Snap hysteresis (Task 6) covers boundary flap. So commitment is deferred as redundant for now; revisit only if field testing shows ramps stalling or thrashing.
- **Options-flow tuning field for `snap_hysteresis_w`** and **per-miner `efficiency_rank` UI**: both have working defaults (`ControlConfig` / model-name-free min-power proxy), so UI exposure is a follow-up, not required for v0.5.0.
- **Export-while-disengaged persistent notification** (spec Part 4, optional): the "Controller engaged" binary_sensor covers visibility; add a notification later if needed.

## Self-Review (completed during planning)

- **Spec coverage:** Part 1 → Tasks 1–2; Part 4 → Task 3; Part 2 → Tasks 4–5; Part 3 hysteresis → Task 6 (commitment deferred with rationale above); §5 decisions (reserve 300 kept, default-on, efficiency source) → Tasks 1 & 4–5; §7 testing → tests in every task + Task 7 regression.
- **Placeholder scan:** every code step contains full code; the README step specifies exact content/scope.
- **Type consistency:** `generate_surplus_fill_states(miners, step_w)`, `MinerConfig.efficiency_rank`, `ControlConfig.snap_hysteresis_w`, `_operator_state()/_apply_operator_state()`, `operator_store(hass, entry_id)`, and `data["engaged"]` are used consistently across tasks.
