# PV-Surplus Mining — All-in-One HA Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a single native Home Assistant custom integration (`pv_surplus_mining`) that reads grid/PV sensors, runs the deterministic PV-surplus decision loop, and commands a Braiins OS+ Antminer fleet directly — replacing the original HA + Node-RED + standalone-adapter stack with one HACS-installable component.

**Architecture:** A `DataUpdateCoordinator` ticks on the control interval: read the configured grid/PV entity states → normalize (sign, validity) → `ControllerLoop.tick()` (rolling average, sustained timers, dwell, `current_state`) → `decide()` → apply the resulting fleet state in merit order via per-miner safe writers (aiohttp Braiins REST: idempotent, rate-limited, verify-after-write, mark-unavailable). Operator controls and telemetry are native HA entities. The pure decision/loop logic is **vendored verbatim** from the `sol-miner-vs` adapter; the HTTP/safety layer is **re-homed onto aiohttp**.

**Tech Stack:** Python 3.11+, Home Assistant integration framework (config flow, `DataUpdateCoordinator`, entity platforms), `aiohttp` (HA's shared client session), `pydantic` v2 (declared as a manifest requirement), PyYAML. Tests: `pytest` + `pytest-homeassistant-custom-component` + `aioresponses`. Distribution: HACS custom repository.

Design: [docs/superpowers/specs/2026-06-24-pv-surplus-mining-ha-integration-design.md](../specs/2026-06-24-pv-surplus-mining-ha-integration-design.md).

## Global Constraints

- **Domain:** `pv_surplus_mining`. All entities/services/storage keys use this prefix.
- **Standalone, self-contained repo.** Do NOT import from `sol-miner-vs`. Vendor (copy) the pure logic; re-home the rest. Source modules to port live at `/home/nick/projects/sol-miner-vs/adapter/app/` and config formats at `/home/nick/projects/sol-miner-vs/config/`.
- **Pure logic is vendored verbatim:** `control/decision.py` and `control/loop.py` are byte-for-byte ports except the single import path change noted in Task 2. Their behavior is the locked reference — do not "improve" it.
- **HTTP via aiohttp only.** No `httpx`. Use a passed-in `aiohttp.ClientSession`; in production it is `homeassistant.helpers.aiohttp_client.async_get_clientsession(hass)`.
- **`pydantic>=2,<3`** is declared in `manifest.json` `requirements`. The vendored models use pydantic v2 `BaseModel`.
- **Sign convention (fixed):** grid power `+ = import`, `− = export/surplus`. Export thresholds compare against the negated average.
- **Safe-by-default (MUST):** sensor `unknown`/`unavailable`/non-numeric/stale → hold-or-reduce, never increase. On HA start / reload, `current_state` initializes to `0` and the coordinator reads real miner state before any ramp-up. Miner write failures are never blind-retried; a miner is marked unavailable after N failures and `max_available_state` shrinks so the loop cannot target a state needing it.
- **Merit order (fixed):** apply fleet states in ascending `priority` (S21+ = 1 → S19j Pro+ = 2 → S19j Pro = 3).
- **Credentials never in repo files.** Miner passwords come only from the config entry (HA encrypted `.storage`). No password literal, no `secrets.yaml`, in the repository.
- **Test command** (run from repo root with the project venv): `python -m pytest`. Every task ends green.
- **HA minimum version:** `2024.1.0` (set in `hacs.json` and used as the floor for `pytest-homeassistant-custom-component`).

---

## File Structure

```
pv-surplus-mining-ha/
├── hacs.json                                   # T1 — HACS manifest
├── pyproject.toml                              # T1 — package + test deps + pytest config
├── requirements_test.txt                       # T1 — pinned test deps
├── README.md                                   # T8 — install/config docs (exists; expanded)
├── .github/workflows/
│   ├── test.yml                                # T8 — pytest CI
│   └── validate.yml                            # T8 — hassfest + HACS validation
├── custom_components/pv_surplus_mining/
│   ├── __init__.py                             # T1 setup/unload; T5 wires coordinator+platforms
│   ├── manifest.json                           # T1
│   ├── const.py                                # T1 — DOMAIN, keys, defaults, PLATFORMS
│   ├── errors.py                               # T2 — error hierarchy (vendored, trimmed)
│   ├── models.py                               # T2 — pydantic models incl. ControlConfig
│   ├── control/
│   │   ├── __init__.py                         # T2
│   │   ├── decision.py                         # T2 — VERBATIM port
│   │   └── loop.py                             # T2 — VERBATIM port (1 import line changed)
│   ├── miner.py                                # T3 — aiohttp client + MinerController (safety)
│   ├── fleet.py                                # T4 — FleetController (merit-order apply)
│   ├── fleet_states.py                         # T4 — load + validate fleet-states.yaml
│   ├── normalize.py                            # T5 — pure grid/PV sensor normalization
│   ├── coordinator.py                          # T5 — DataUpdateCoordinator
│   ├── config_flow.py                          # T6 — config + options flow
│   ├── entity.py                               # T7 — shared CoordinatorEntity base
│   ├── sensor.py                               # T7 — telemetry + fleet_state sensors
│   ├── switch.py                               # T7 — auto/emergency/manual switches
│   ├── number.py                               # T7 — manual_state/max_state/export_buffer
│   └── translations/en.json                    # T6
└── tests/
    ├── conftest.py                             # T1 — HA harness fixtures + helpers
    ├── test_init.py                            # T1 — setup/unload
    ├── test_decision.py                        # T2 — ported decision tests
    ├── test_loop.py                            # T2 — ported loop tests
    ├── test_miner.py                           # T3 — safety semantics
    ├── test_fleet.py                           # T4 — merit order + loader
    ├── test_normalize.py                       # T5 — normalization
    ├── test_coordinator.py                     # T5 — tick orchestration + safe defaults
    ├── test_config_flow.py                     # T6 — flow + options + validation
    └── test_entities.py                        # T7 — entity state + control write-back
```

---

### Task 1: HACS scaffold + empty integration that loads and unloads

**Files:**
- Create: `hacs.json`, `pyproject.toml`, `requirements_test.txt`
- Create: `custom_components/pv_surplus_mining/{manifest.json, const.py, __init__.py}`
- Test: `tests/conftest.py`, `tests/test_init.py`

**Interfaces:**
- Produces: `const.DOMAIN = "pv_surplus_mining"`; `const.PLATFORMS: list[str]` (empty in T1, filled in T7); config-entry data keys (`const.CONF_*`).
- Produces: `async_setup_entry(hass, entry) -> bool` and `async_unload_entry(hass, entry) -> bool` that store/clear `hass.data[DOMAIN][entry.entry_id]`.

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "pv-surplus-mining-ha"
version = "0.1.0"
description = "All-in-one Home Assistant integration for PV-surplus Bitcoin mining control (Braiins OS+)."
requires-python = ">=3.11"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 2: Create `requirements_test.txt`**

```text
# pytest-homeassistant-custom-component pins a compatible Home Assistant + pytest stack.
pytest-homeassistant-custom-component>=0.13.100
pydantic>=2,<3
PyYAML>=6
aioresponses>=0.7.6
```

Install into the project venv: `python -m pip install -r requirements_test.txt`.

- [ ] **Step 3: Create `hacs.json`**

```json
{
  "name": "PV-Surplus Mining",
  "render_readme": true,
  "homeassistant": "2024.1.0"
}
```

- [ ] **Step 4: Create `custom_components/pv_surplus_mining/manifest.json`**

```json
{
  "domain": "pv_surplus_mining",
  "name": "PV-Surplus Mining",
  "version": "0.1.0",
  "documentation": "https://github.com/Solar-TechNick/pv-surplus-mining-ha",
  "issue_tracker": "https://github.com/Solar-TechNick/pv-surplus-mining-ha/issues",
  "codeowners": ["@Solar-TechNick"],
  "iot_class": "local_polling",
  "integration_type": "hub",
  "config_flow": true,
  "requirements": ["pydantic>=2,<3", "PyYAML>=6"]
}
```

- [ ] **Step 5: Create `custom_components/pv_surplus_mining/const.py`**

```python
DOMAIN = "pv_surplus_mining"

# Filled in Task 7 once entity platforms exist.
PLATFORMS: list[str] = []

# Config-entry data keys
CONF_MINERS = "miners"            # list[dict]: {id, model, ip, priority, username, password, min_power_w, max_power_w}
CONF_GRID_ENTITY = "grid_entity"
CONF_PV_ENTITY = "pv_entity"
CONF_BATTERY_ENTITY = "battery_entity"
CONF_IMPORT_POSITIVE = "grid_import_positive"
CONF_FLEET_STATES_PATH = "fleet_states_path"

# Options keys (mirror control.yaml). Defaults below are copied verbatim from
# sol-miner-vs/config/control.yaml.
OPT_LOOP_INTERVAL_S = "loop_interval_s"
OPT_AVG_WINDOW_S = "avg_window_s"
OPT_EXPORT_RESERVE_W = "export_reserve_w"
OPT_STEP_UP_EXPORT_THRESHOLD_W = "step_up_export_threshold_w"
OPT_STEP_UP_REQUIRED_DURATION_S = "step_up_required_duration_s"
OPT_STEP_DOWN_IMPORT_THRESHOLD_W = "step_down_import_threshold_w"
OPT_STEP_DOWN_REQUIRED_DURATION_S = "step_down_required_duration_s"
OPT_EMERGENCY_IMPORT_THRESHOLD_W = "emergency_import_threshold_w"
OPT_EMERGENCY_REQUIRED_DURATION_S = "emergency_required_duration_s"
OPT_MIN_STATE_DWELL_S = "min_state_dwell_s"
OPT_FALLBACK_STATE = "fallback_state"
OPT_MAX_STATE = "max_state"

DEFAULT_FLEET_STATES_FILENAME = "fleet-states.yaml"
```

- [ ] **Step 6: Create `custom_components/pv_surplus_mining/__init__.py` (minimal setup/unload)**

```python
"""All-in-one PV-surplus mining controller integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up pv_surplus_mining from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    # Coordinator + platforms are wired in later tasks. For now, claim the entry.
    hass.data[DOMAIN][entry.entry_id] = {}
    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = True
    if PLATFORMS:
        unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
```

- [ ] **Step 7: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures for the HA test harness."""
import pytest

pytest_plugins = ["pytest_homeassistant_custom_component"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom_components/ in every test."""
    yield
```

- [ ] **Step 8: Write the failing test — `tests/test_init.py`**

```python
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import DOMAIN


async def test_setup_and_unload_entry(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="PV-Surplus Mining")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
```

- [ ] **Step 9: Run it to verify it fails (integration not importable yet, or passes once files exist)**

Run: `python -m pytest tests/test_init.py -v`
Expected before Steps 4–6 exist: collection/import error. After: PASS.

- [ ] **Step 10: Run the full suite**

Run: `python -m pytest -v`
Expected: `1 passed`.

- [ ] **Step 11: Commit**

```bash
git add hacs.json pyproject.toml requirements_test.txt custom_components tests
git commit -m "feat: HACS scaffold + empty integration that loads/unloads"
```

---

### Task 2: Vendor the shared core (errors, models, decision, loop)

**Files:**
- Create: `custom_components/pv_surplus_mining/errors.py`, `models.py`
- Create: `custom_components/pv_surplus_mining/control/{__init__.py, decision.py, loop.py}`
- Test: `tests/test_decision.py`, `tests/test_loop.py`

**Interfaces:**
- Produces: `models.ControlConfig` (pydantic) — fields/defaults copied verbatim from `sol-miner-vs/adapter/app/config.py`: `enabled_default:bool=False, loop_interval_s:float=10.0, export_reserve_w:int=300, step_up_export_threshold_w:int=700, step_up_required_duration_s:float=180.0, step_down_import_threshold_w:int=250, step_down_required_duration_s:float=30.0, emergency_import_threshold_w:int=1200, emergency_required_duration_s:float=5.0, min_state_dwell_s:float=120.0, fallback_state:int=0, max_state:int=14, avg_window_s:float=60.0`.
- Produces: `models.FleetStateTarget(action: Literal["sleep","active"]|None, power_w: int|None)`, `models.TunerState`, `models.MinerStatus`, `models.CommandResult` (verbatim from adapter `models.py`).
- Produces: `errors.AdapterError` + subclasses `ConfigError, OutOfRangeError, RateLimitedError, MinerUnavailableError, AuthError, UpstreamError`.
- Produces: `control.decision.DecisionContext`, `control.decision.Decision`, `control.decision.decide(ctx)->Decision`.
- Produces: `control.loop.ControlInputs`, `control.loop.ControllerLoop(config: ControlConfig, max_available_state: int, current_state: int = 0)` with `tick(grid_w: float, inputs: ControlInputs|None) -> Decision`.

- [ ] **Step 1: Create `custom_components/pv_surplus_mining/errors.py`**

```python
"""Error hierarchy (vendored from the adapter; HTTP status codes dropped)."""


class AdapterError(Exception):
    """Base class for controller errors."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigError(AdapterError):
    pass


class OutOfRangeError(AdapterError):
    pass


class RateLimitedError(AdapterError):
    pass


class MinerUnavailableError(AdapterError):
    pass


class AuthError(AdapterError):
    pass


class UpstreamError(AdapterError):
    pass
```

- [ ] **Step 2: Create `custom_components/pv_surplus_mining/models.py`**

```python
from typing import Literal

from pydantic import BaseModel, Field


class FleetStateTarget(BaseModel):
    action: Literal["sleep", "active"] | None = None
    power_w: int | None = None


class TunerState(BaseModel):
    power_target_w: int | None = None
    mode: str | None = None
    profile: str | None = None
    raw: dict = Field(default_factory=dict)


class MinerStatus(BaseModel):
    miner_id: str
    online: bool
    power_target_w: int | None = None
    actual_power_w: int | None = None
    hashrate_ths: float | None = None
    temp_max_c: float | None = None
    tuner_mode: str | None = None
    active_boards: int | None = None
    available: bool = True


class CommandResult(BaseModel):
    miner_id: str
    action: str
    target_w: int | None = None
    changed: bool
    verified: bool
    result: str


class ControlConfig(BaseModel):
    enabled_default: bool = False
    loop_interval_s: float = 10.0
    export_reserve_w: int = 300
    step_up_export_threshold_w: int = 700
    step_up_required_duration_s: float = 180.0
    step_down_import_threshold_w: int = 250
    step_down_required_duration_s: float = 30.0
    emergency_import_threshold_w: int = 1200
    emergency_required_duration_s: float = 5.0
    min_state_dwell_s: float = 120.0
    fallback_state: int = 0
    max_state: int = 14
    avg_window_s: float = 60.0
```

- [ ] **Step 3: Create `custom_components/pv_surplus_mining/control/__init__.py`** (empty file)

- [ ] **Step 4: Create `custom_components/pv_surplus_mining/control/decision.py` — VERBATIM port**

Copy `/home/nick/projects/sol-miner-vs/adapter/app/control/decision.py` exactly (no changes). For reference it is:

```python
from pydantic import BaseModel


class DecisionContext(BaseModel):
    grid_avg_w: float = 0.0
    current_state: int = 0
    seconds_since_last_transition: float = 0.0
    export_sustained_s: float = 0.0
    import_sustained_s: float = 0.0
    import_emergency_s: float = 0.0
    auto_enabled: bool = False
    emergency_stop: bool = False
    manual_override: bool = False
    manual_state: int = 0
    max_state: int = 14
    all_required_online: bool = True
    any_fault: bool = False
    any_over_temp_warning: bool = False
    any_over_temp_critical: bool = False
    telemetry_stale: bool = False
    repeated_failures: bool = False
    step_up_required_duration_s: float = 180.0
    step_down_required_duration_s: float = 30.0
    emergency_required_duration_s: float = 5.0
    min_state_dwell_s: float = 120.0
    fallback_state: int = 0
    max_available_state: int = 14


class Decision(BaseModel):
    target_state: int
    changed: bool
    emergency: bool
    reason: str


def decide(ctx: DecisionContext) -> Decision:
    effective_max = min(ctx.max_state, ctx.max_available_state)

    def out(target: int, emergency: bool, reason: str) -> Decision:
        t = max(0, min(target, effective_max))
        return Decision(
            target_state=t, changed=(t != ctx.current_state),
            emergency=emergency, reason=reason,
        )

    # 1. Emergency (bypasses all dwell)
    if ctx.emergency_stop:
        return out(ctx.fallback_state, True, "emergency stop")
    if ctx.import_emergency_s >= ctx.emergency_required_duration_s:
        return out(ctx.fallback_state, True, "hard grid import sustained")
    if ctx.any_over_temp_critical:
        return out(ctx.fallback_state, True, "critical temperature")
    if ctx.telemetry_stale:
        return out(ctx.fallback_state, True, "telemetry stale")
    if ctx.repeated_failures:
        return out(ctx.fallback_state, True, "repeated command failures")

    # 2. Automation disabled -> hold
    if not ctx.auto_enabled:
        return out(ctx.current_state, False, "automation disabled")

    # 3. Manual override
    if ctx.manual_override:
        return out(ctx.manual_state, False, "manual override")

    # 4. Automatic: fast ramp-down, then gated ramp-up, else hold
    if (
        ctx.import_sustained_s >= ctx.step_down_required_duration_s
        or ctx.any_fault
        or ctx.any_over_temp_warning
        or ctx.current_state > effective_max
    ):
        return out(ctx.current_state - 1, False, "ramp down")

    if (
        ctx.export_sustained_s >= ctx.step_up_required_duration_s
        and ctx.seconds_since_last_transition >= ctx.min_state_dwell_s
        and ctx.all_required_online
        and not ctx.any_fault
        and not ctx.any_over_temp_warning
        and ctx.current_state < effective_max
    ):
        return out(ctx.current_state + 1, False, "ramp up")

    return out(ctx.current_state, False, "hold")
```

- [ ] **Step 5: Create `custom_components/pv_surplus_mining/control/loop.py` — VERBATIM port, ONE import line changed**

Copy `/home/nick/projects/sol-miner-vs/adapter/app/control/loop.py` exactly, EXCEPT change the import `from ..config import ControlConfig` to `from ..models import ControlConfig`. The full file:

```python
from collections import deque
from statistics import mean

from pydantic import BaseModel

from ..models import ControlConfig
from .decision import DecisionContext, Decision, decide


# NOTE: these defaults are fail-OPEN (healthy/enabled) so a caller sets only what
# it exercises. A production caller MUST populate every field from real telemetry
# and never rely on these defaults; the safe-by-default posture lives in
# decide()/DecisionContext, not here.
class ControlInputs(BaseModel):
    auto_enabled: bool = True
    emergency_stop: bool = False
    manual_override: bool = False
    manual_state: int = 0
    max_state: int = 14
    all_required_online: bool = True
    any_fault: bool = False
    any_over_temp_warning: bool = False
    any_over_temp_critical: bool = False
    telemetry_stale: bool = False
    repeated_failures: bool = False


class ControllerLoop:
    """Pure stateful controller. Holds the temporal state (rolling average,
    sustained-condition timers, dwell, current_state) and calls the pure
    decide() each tick. No I/O. Time advances by dt = loop_interval_s per tick."""

    def __init__(self, config: ControlConfig, max_available_state: int, current_state: int = 0):
        self.config = config
        self.max_available_state = max_available_state
        self.current_state = current_state
        self.seconds_since_last_transition = 0.0
        self.export_sustained_s = 0.0
        self.import_sustained_s = 0.0
        self.import_emergency_s = 0.0
        self.grid_avg_w = 0.0
        dt = config.loop_interval_s
        self._window: deque[float] = deque(maxlen=max(1, round(config.avg_window_s / dt)))

    def tick(self, grid_w: float, inputs: ControlInputs | None = None) -> Decision:
        inputs = inputs or ControlInputs()
        c = self.config
        dt = c.loop_interval_s

        self._window.append(grid_w)
        self.grid_avg_w = mean(self._window)

        self.export_sustained_s = (
            self.export_sustained_s + dt if self.grid_avg_w <= -c.step_up_export_threshold_w else 0.0
        )
        self.import_sustained_s = (
            self.import_sustained_s + dt if self.grid_avg_w >= c.step_down_import_threshold_w else 0.0
        )
        self.import_emergency_s = (
            self.import_emergency_s + dt if grid_w >= c.emergency_import_threshold_w else 0.0
        )

        ctx = DecisionContext(
            grid_avg_w=self.grid_avg_w,
            current_state=self.current_state,
            seconds_since_last_transition=self.seconds_since_last_transition,
            export_sustained_s=self.export_sustained_s,
            import_sustained_s=self.import_sustained_s,
            import_emergency_s=self.import_emergency_s,
            step_up_required_duration_s=c.step_up_required_duration_s,
            step_down_required_duration_s=c.step_down_required_duration_s,
            emergency_required_duration_s=c.emergency_required_duration_s,
            min_state_dwell_s=c.min_state_dwell_s,
            fallback_state=c.fallback_state,
            max_available_state=self.max_available_state,
            **inputs.model_dump(),
        )
        decision = decide(ctx)

        if decision.target_state != self.current_state:
            self.current_state = decision.target_state
            self.seconds_since_last_transition = 0.0
        else:
            self.seconds_since_last_transition += dt

        return decision
```

- [ ] **Step 6: Write the failing tests — `tests/test_decision.py`**

```python
from custom_components.pv_surplus_mining.control.decision import DecisionContext, decide


def test_emergency_stop_forces_fallback_state():
    d = decide(DecisionContext(current_state=5, emergency_stop=True, fallback_state=0))
    assert d.target_state == 0 and d.emergency is True and d.changed is True


def test_hard_import_emergency_after_duration():
    d = decide(DecisionContext(current_state=3, import_emergency_s=5, emergency_required_duration_s=5))
    assert d.target_state == 0 and d.emergency is True


def test_disabled_holds_current_state():
    d = decide(DecisionContext(current_state=4, auto_enabled=False))
    assert d.target_state == 4 and d.changed is False and d.reason == "automation disabled"


def test_manual_override_targets_manual_state():
    d = decide(DecisionContext(current_state=1, auto_enabled=True, manual_override=True, manual_state=7, max_state=14, max_available_state=14))
    assert d.target_state == 7 and d.emergency is False


def test_ramp_up_requires_dwell_and_sustained_export():
    ctx = DecisionContext(
        current_state=2, auto_enabled=True, max_state=14, max_available_state=14,
        export_sustained_s=180, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
    )
    assert decide(ctx).target_state == 3


def test_ramp_down_is_fast():
    ctx = DecisionContext(
        current_state=5, auto_enabled=True, max_state=14, max_available_state=14,
        import_sustained_s=30, step_down_required_duration_s=30,
    )
    assert decide(ctx).target_state == 4


def test_target_clamped_to_effective_max():
    d = decide(DecisionContext(current_state=10, auto_enabled=True, manual_override=True, manual_state=99, max_state=14, max_available_state=6))
    assert d.target_state == 6
```

- [ ] **Step 7: Write the failing tests — `tests/test_loop.py`**

```python
from custom_components.pv_surplus_mining.models import ControlConfig
from custom_components.pv_surplus_mining.control.loop import ControllerLoop, ControlInputs


def _cfg(**kw):
    base = dict(loop_interval_s=10, avg_window_s=10, step_up_export_threshold_w=700,
               step_down_import_threshold_w=250, emergency_import_threshold_w=1200,
               step_up_required_duration_s=20, step_down_required_duration_s=10,
               emergency_required_duration_s=10, min_state_dwell_s=10, max_state=14)
    base.update(kw)
    return ControlConfig(**base)


def test_emergency_timer_uses_raw_sample_not_average():
    # window maxlen = round(100/10) = 10. Prime with 9 zeros so the rolling
    # average stays well below the emergency threshold while the raw sample spikes.
    loop = ControllerLoop(_cfg(avg_window_s=100, loop_interval_s=10), max_available_state=14, current_state=3)
    for _ in range(9):
        loop.tick(0, ControlInputs(auto_enabled=True))
    d = loop.tick(5000, ControlInputs(auto_enabled=True))   # window=[0×9, 5000] -> avg 500
    assert loop.grid_avg_w == 500                            # average alone would NOT fire emergency
    assert loop.import_emergency_s == 10                     # but raw 5000 >= 1200 accrues one dt
    assert d.target_state == 0 and d.emergency is True       # emergency fires off the RAW sample


def test_rolling_average_suppresses_single_export_spike():
    # window maxlen = round(100/10) = 10. One -5000 spike among 9 zeros averages to
    # -500, which is ABOVE the -700 export threshold -> no export accrual, no ramp-up.
    loop = ControllerLoop(_cfg(avg_window_s=100, loop_interval_s=10, step_up_export_threshold_w=700),
                          max_available_state=14, current_state=0)
    for _ in range(9):
        loop.tick(0, ControlInputs(auto_enabled=True))
    loop.tick(-5000, ControlInputs(auto_enabled=True))
    assert loop.grid_avg_w == -500
    assert loop.export_sustained_s == 0   # spike suppressed by the average; no sustained export


def test_dwell_resets_on_transition_timers_persist():
    loop = ControllerLoop(_cfg(), max_available_state=14, current_state=0)
    loop.export_sustained_s = 999
    loop.seconds_since_last_transition = 999
    loop.tick(-5000, ControlInputs(auto_enabled=True))  # ramp up
    assert loop.current_state == 1
    assert loop.seconds_since_last_transition == 0.0
```

- [ ] **Step 8: Run tests to verify they fail**

Run: `python -m pytest tests/test_decision.py tests/test_loop.py -v`
Expected: import errors until Steps 1–5 done; then PASS.

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest -v`
Expected: all green.

- [ ] **Step 10: Commit**

```bash
git add custom_components/pv_surplus_mining/errors.py custom_components/pv_surplus_mining/models.py custom_components/pv_surplus_mining/control tests/test_decision.py tests/test_loop.py
git commit -m "feat: vendor shared core (errors, models, decision, loop) + tests"
```

---

### Task 3: aiohttp Braiins client + per-miner safe writer (`MinerController`)

**Files:**
- Create: `custom_components/pv_surplus_mining/miner.py`
- Test: `tests/test_miner.py`

**Interfaces:**
- Consumes: `errors`, `models.TunerState`/`MinerStatus`/`CommandResult` (Task 2).
- Produces: `miner.MinerConfig` (pydantic: `id, model, ip, priority, min_power_w, max_power_w, power_targets_w: dict[str,int|None]={}, command_cooldown_sec:int=120, username:str="root"`).
- Produces: `miner.AioBraiinsClient(cfg: MinerConfig, password: str, session: aiohttp.ClientSession)` with async `login()`, `get_miner_details()->dict`, `get_stats()->dict`, `get_tuner_state()->TunerState`, `set_power_target(watt:int)->None`.
- Produces: `miner.MinerController(cfg, client, clock=time.monotonic, max_failures=3)` with attrs `available:bool`, `failure_count:int`, and async `get_tuner_state()`, `get_status()->MinerStatus`, `set_power_target(watt, *, force=False, audit_action=None)->CommandResult`, `curtail(action: Literal["sleep","wakeup"], wake_target_w=None)->CommandResult`. Same safety semantics as the adapter's `MinerService` (range-check → idempotent skip → rate-limit → write → mark-unavailable-after-N → verify).

- [ ] **Step 1: Create `custom_components/pv_surplus_mining/miner.py`**

```python
"""aiohttp Braiins REST client + per-miner safe writer (re-homed from the adapter)."""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Literal

import aiohttp
from pydantic import BaseModel, Field

from .errors import (
    AdapterError, AuthError, MinerUnavailableError, OutOfRangeError,
    RateLimitedError, UpstreamError,
)
from .models import CommandResult, MinerStatus, TunerState

_AUDIT = logging.getLogger(f"{__package__}.audit")


class MinerConfig(BaseModel):
    id: str
    model: str
    ip: str
    priority: int
    min_power_w: int
    max_power_w: int
    power_targets_w: dict[str, int | None] = Field(default_factory=dict)
    command_cooldown_sec: int = 120
    username: str = "root"


class AioBraiinsClient:
    def __init__(self, cfg: MinerConfig, password: str, session: aiohttp.ClientSession):
        self.cfg = cfg
        self._password = password
        self._session = session
        self._base = f"http://{cfg.ip}/api/v1"
        self.token: str | None = None

    async def login(self) -> None:
        async with self._session.post(
            f"{self._base}/auth/login",
            json={"username": self.cfg.username, "password": self._password},
        ) as resp:
            if resp.status != 200:
                raise AuthError(f"{self.cfg.id}: login failed ({resp.status})")
            data = await resp.json(content_type=None)
        self.token = (data or {}).get("token")
        if not self.token:
            raise AuthError(f"{self.cfg.id}: login response had no token")

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request_json(self, method: str, path: str, **kwargs) -> dict:
        if self.token is None:
            await self.login()
        for attempt in (1, 2):
            async with self._session.request(
                method, f"{self._base}{path}", headers=self._auth_headers(), **kwargs
            ) as resp:
                if resp.status == 401:
                    if attempt == 1:
                        await self.login()
                        continue
                    raise AuthError(f"{self.cfg.id}: re-auth failed for {path}")
                if resp.status >= 500:
                    raise UpstreamError(f"{self.cfg.id}: {method} {path} -> {resp.status}")
                text = await resp.text()
                return json.loads(text) if text else {}
        raise UpstreamError(f"{self.cfg.id}: {method} {path} exhausted retries")

    async def get_miner_details(self) -> dict:
        return await self._request_json("GET", "/miner/details")

    async def get_stats(self) -> dict:
        return await self._request_json("GET", "/miner/stats")

    async def get_tuner_state(self) -> TunerState:
        raw = await self._request_json("GET", "/performance/tuner-state")
        target = (raw.get("power_target") or {}).get("watt")
        return TunerState(power_target_w=target, mode=raw.get("mode"), profile=raw.get("profile"), raw=raw)

    async def set_power_target(self, watt: int) -> None:
        await self._request_json("PUT", "/performance/power-target", json={"watt": watt})


class MinerController:
    """Idempotent, rate-limited, verified, mark-unavailable-after-N writer.
    Assumes a single serialized writer per miner (the coordinator's loop)."""

    def __init__(self, cfg: MinerConfig, client: AioBraiinsClient,
                 clock: Callable[[], float] = time.monotonic, max_failures: int = 3):
        self.cfg = cfg
        self.client = client
        self._clock = clock
        self._max_failures = max_failures
        self.available = True
        self.failure_count = 0
        self._last_command_ts: float | None = None

    def _audit(self, payload: dict) -> None:
        _AUDIT.info("%s", json.dumps(payload))

    async def get_tuner_state(self) -> TunerState:
        return await self.client.get_tuner_state()

    async def get_status(self) -> MinerStatus:
        try:
            details = await self.client.get_miner_details()
            stats = await self.client.get_stats()
            tuner = await self.client.get_tuner_state()
        except AdapterError:
            return MinerStatus(miner_id=self.cfg.id, online=False, available=self.available)
        return MinerStatus(
            miner_id=self.cfg.id,
            online=str(details.get("status", "")).lower() != "offline",
            power_target_w=tuner.power_target_w,
            actual_power_w=(stats.get("power") or {}).get("approx"),
            hashrate_ths=stats.get("hashrate_ths"),
            temp_max_c=stats.get("temp_max_c"),
            tuner_mode=tuner.mode,
            available=self.available,
        )

    def _check_rate_limit(self, force: bool) -> None:
        if force or self._last_command_ts is None:
            return
        if self._clock() - self._last_command_ts < self.cfg.command_cooldown_sec:
            raise RateLimitedError(f"{self.cfg.id}: within command cooldown")

    async def set_power_target(self, watt: int, *, force: bool = False,
                               audit_action: str | None = None) -> CommandResult:
        action = audit_action or "set_power_target"
        if not self.available:
            raise MinerUnavailableError(f"{self.cfg.id}: marked unavailable")
        if not (self.cfg.min_power_w <= watt <= self.cfg.max_power_w):
            raise OutOfRangeError(
                f"{self.cfg.id}: {watt}W outside [{self.cfg.min_power_w},{self.cfg.max_power_w}]"
            )

        current = await self.client.get_tuner_state()
        if current.power_target_w == watt:
            self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "skipped_idempotent"})
            return CommandResult(miner_id=self.cfg.id, action=action, target_w=watt, changed=False, verified=True, result="skipped_idempotent")

        self._check_rate_limit(force)

        try:
            await self.client.set_power_target(watt)
        except AdapterError as exc:
            self.failure_count += 1
            if self.failure_count >= self._max_failures:
                self.available = False
            self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "error", "error": str(exc), "failures": self.failure_count})
            raise

        self.failure_count = 0
        self._last_command_ts = self._clock()
        verified = (await self.client.get_tuner_state()).power_target_w == watt
        self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "ok", "verified": verified})
        return CommandResult(miner_id=self.cfg.id, action=action, target_w=watt, changed=True, verified=verified, result="ok")

    async def curtail(self, action: Literal["sleep", "wakeup"], wake_target_w: int | None = None) -> CommandResult:
        if action == "sleep":
            target = self.cfg.min_power_w
        elif wake_target_w is not None:
            target = wake_target_w
        else:
            target = self.cfg.power_targets_w.get("normal") or self.cfg.min_power_w
        return await self.set_power_target(int(target), force=(action == "sleep"), audit_action=f"curtail:{action}")
```

- [ ] **Step 2: Write the failing tests — `tests/test_miner.py`**

```python
import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.pv_surplus_mining.errors import (
    MinerUnavailableError, OutOfRangeError, RateLimitedError,
)
from custom_components.pv_surplus_mining.miner import AioBraiinsClient, MinerConfig, MinerController

CFG = MinerConfig(id="s21plus_01", model="S21+", ip="10.0.0.5", priority=1,
                  min_power_w=1400, max_power_w=4000, command_cooldown_sec=120,
                  power_targets_w={"normal": 3000})
BASE = "http://10.0.0.5/api/v1"


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


async def _client(session):
    c = AioBraiinsClient(CFG, "pw", session)
    return c


async def test_login_and_set_power_target_verifies():
    clock = FakeClock()
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})   # current
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})   # verify
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), clock=clock)
            res = await ctrl.set_power_target(3000)
    assert res.changed is True and res.verified is True and res.result == "ok"


async def test_idempotent_skip_when_already_at_target():
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.set_power_target(3000)
    assert res.changed is False and res.result == "skipped_idempotent"


async def test_out_of_range_rejected():
    async with aiohttp.ClientSession() as session:
        ctrl = MinerController(CFG, await _client(session))
        with pytest.raises(OutOfRangeError):
            await ctrl.set_power_target(999)


async def test_rate_limited_within_cooldown():
    clock = FakeClock()
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})  # 2nd call current
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), clock=clock)
            await ctrl.set_power_target(3000)        # sets _last_command_ts
            clock.t += 10                            # < 120 cooldown
            with pytest.raises(RateLimitedError):
                await ctrl.set_power_target(2000)


async def test_marked_unavailable_after_repeated_failures():
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        # 3 failed writes: each does current-read (200) then PUT (500)
        for _ in range(3):
            m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})
            m.put(f"{BASE}/performance/power-target", status=500)
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), max_failures=3)
            for _ in range(3):
                with pytest.raises(Exception):
                    await ctrl.set_power_target(3000, force=True)
    assert ctrl.available is False
    with pytest.raises(MinerUnavailableError):
        await ctrl.set_power_target(3000, force=True)
```

- [ ] **Step 3: Run tests to verify they fail then pass**

Run: `python -m pytest tests/test_miner.py -v`
Expected: import error first; after Step 1, all PASS.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest -v` — all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/miner.py tests/test_miner.py
git commit -m "feat: aiohttp Braiins client + MinerController safe writer"
```

---

### Task 4: Fleet apply in merit order + fleet-states loader/validator

**Files:**
- Create: `custom_components/pv_surplus_mining/fleet.py`, `custom_components/pv_surplus_mining/fleet_states.py`
- Test: `tests/test_fleet.py`

**Interfaces:**
- Consumes: `miner.MinerController`/`MinerConfig` (Task 3), `models.FleetStateTarget`/`CommandResult` (Task 2), `errors.ConfigError`/`AdapterError`.
- Produces: `fleet_states.load_fleet_states(path: Path) -> dict[int, dict[str, FleetStateTarget]]` (parses the `states:` mapping from the YAML).
- Produces: `fleet_states.validate_fleet_states(states, miner_ids: set[str]) -> None` (raises `ConfigError` if any state omits or adds a miner id; requires state `0` present).
- Produces: `fleet.FleetController(miners: dict[str, MinerController], states: dict[int, dict[str, FleetStateTarget]])`; `__init__` raises `ConfigError` if any state omits a configured miner. Async `apply_state(state_id:int, *, force=False) -> list[CommandResult]` (ascending priority); `get_state() -> dict` (`{"miners": {id: target_w|None}, "matched_state": int|None}`). Property `max_state: int` = max key.

- [ ] **Step 1: Create `custom_components/pv_surplus_mining/fleet_states.py`**

```python
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
```

- [ ] **Step 2: Create `custom_components/pv_surplus_mining/fleet.py`**

```python
"""Apply a fleet state across miners in merit order (re-homed FleetService)."""
from __future__ import annotations

from .errors import AdapterError, ConfigError
from .miner import MinerController
from .models import CommandResult, FleetStateTarget


class FleetController:
    def __init__(self, miners: dict[str, MinerController],
                 states: dict[int, dict[str, FleetStateTarget]]):
        self.miners = miners
        self.states = states
        for state_id, targets in states.items():
            missing = set(miners) - set(targets)
            if missing:
                raise ConfigError(f"fleet state {state_id} omits miner(s): {sorted(missing)}")

    @property
    def max_state(self) -> int:
        return max(self.states) if self.states else 0

    async def apply_state(self, state_id: int, *, force: bool = False) -> list[CommandResult]:
        if state_id not in self.states:
            raise KeyError(f"unknown fleet state {state_id}")
        targets = self.states[state_id]
        ordered = sorted(self.miners.values(), key=lambda m: m.cfg.priority)
        results: list[CommandResult] = []
        for svc in ordered:
            target = targets.get(svc.cfg.id)
            try:
                if target is None or target.action == "sleep" or target.power_w is None:
                    results.append(await svc.curtail("sleep"))
                else:
                    results.append(await svc.set_power_target(target.power_w, force=force))
            except AdapterError as exc:
                results.append(CommandResult(
                    miner_id=svc.cfg.id, action="apply_state", target_w=None,
                    changed=False, verified=False, result=f"error:{exc.__class__.__name__}",
                ))
        return results

    async def get_state(self) -> dict:
        miners_state: dict[str, int | None] = {}
        for mid, svc in self.miners.items():
            try:
                miners_state[mid] = (await svc.get_tuner_state()).power_target_w
            except AdapterError:
                miners_state[mid] = None
        matched = None
        for sid, targets in self.states.items():
            if all(
                (t.action == "sleep" or t.power_w is None) and miners_state.get(mid) in (None, self.miners[mid].cfg.min_power_w)
                or (t.power_w is not None and miners_state.get(mid) == t.power_w)
                for mid, t in targets.items()
            ):
                matched = sid
                break
        return {"miners": miners_state, "matched_state": matched}
```

- [ ] **Step 3: Write the failing tests — `tests/test_fleet.py`**

```python
from pathlib import Path

import pytest

from custom_components.pv_surplus_mining.errors import ConfigError
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.fleet_states import load_fleet_states, validate_fleet_states
from custom_components.pv_surplus_mining.miner import MinerConfig, MinerController
from custom_components.pv_surplus_mining.models import FleetStateTarget, TunerState, CommandResult

FLEET_YAML = """
states:
  0:
    a: { action: sleep }
    b: { action: sleep }
  1:
    a: { action: active, power_w: 2000 }
    b: { action: sleep }
"""


def test_load_and_validate(tmp_path):
    p = tmp_path / "fleet-states.yaml"
    p.write_text(FLEET_YAML)
    states = load_fleet_states(p)
    assert set(states) == {0, 1}
    validate_fleet_states(states, {"a", "b"})


def test_validate_rejects_missing_miner(tmp_path):
    p = tmp_path / "fleet-states.yaml"
    p.write_text(FLEET_YAML)
    states = load_fleet_states(p)
    with pytest.raises(ConfigError):
        validate_fleet_states(states, {"a", "b", "c"})


class StubController(MinerController):
    def __init__(self, cfg):
        self.cfg = cfg
        self.calls = []
    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.calls.append(("set", watt)); return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")
    async def curtail(self, action, wake_target_w=None):
        self.calls.append(("curtail", action)); return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")


def _ctrl(mid, prio):
    return StubController(MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000))


async def test_apply_state_in_merit_order():
    a, b = _ctrl("a", 1), _ctrl("b", 2)
    states = {0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")}}
    fc = FleetController({"a": a, "b": b}, states)
    results = await fc.apply_state(1)
    assert a.calls == [("set", 2000)] and b.calls == [("curtail", "sleep")]
    assert len(results) == 2


def test_fleetcontroller_rejects_state_missing_miner():
    a = _ctrl("a", 1)
    with pytest.raises(ConfigError):
        FleetController({"a": a}, {0: {}})
```

- [ ] **Step 4: Run tests then full suite**

Run: `python -m pytest tests/test_fleet.py -v` then `python -m pytest -v` — all green.

- [ ] **Step 5: Commit**

```bash
git add custom_components/pv_surplus_mining/fleet.py custom_components/pv_surplus_mining/fleet_states.py tests/test_fleet.py
git commit -m "feat: fleet merit-order apply + fleet-states loader/validator"
```

---

### Task 5: Grid normalization + coordinator (read → tick → apply) + wire into setup

**Files:**
- Create: `custom_components/pv_surplus_mining/normalize.py`, `custom_components/pv_surplus_mining/coordinator.py`
- Modify: `custom_components/pv_surplus_mining/fleet.py` (add `max_available_state`)
- Modify: `custom_components/pv_surplus_mining/__init__.py` (build coordinator on setup)
- Test: `tests/test_normalize.py`, `tests/test_coordinator.py`, `tests/test_init.py` (update), `tests/test_fleet.py` (add)

**Plan deviation from spec (flag for reviewer):** the spec listed an `export_buffer_w` number entity. The vendored, verbatim `decide()` gates ramp-up on `step_up_export_threshold_w`, not on a separate buffer — a live `export_buffer_w` number would be wired to nothing. Refinement: expose the thresholds (including `export_reserve_w`/`step_up_export_threshold_w`) via the **Options flow** instead, and do not create a do-nothing `export_buffer_w` entity. Entities become switches `auto_enabled`/`emergency_stop`/`manual_override` and numbers `manual_state`/`max_state` (Task 7).

**Interfaces:**
- Produces: `normalize.normalize_grid_power(raw: str | float | int | None, import_positive: bool) -> float | None` — returns `None` for `None`/`"unknown"`/`"unavailable"`/non-numeric; otherwise float with sign normalized to `+import / −export` (pass through if `import_positive`, else negate).
- Produces (modify `fleet.py`): `FleetController.max_available_state(available_ids: set[str]) -> int` — highest state id whose `active` targets all belong to `available_ids` (state 0 always qualifies).
- Produces: `coordinator.PvSurplusCoordinator(hass, config: ControlConfig, fleet: FleetController, grid_entity: str, import_positive: bool, pv_entity: str | None = None)` — a `DataUpdateCoordinator[dict]` whose `_async_update_data()` runs one control tick. Public control attributes (set by entities): `auto_enabled, emergency_stop, manual_override, manual_state, max_state`. Holds `self.loop: ControllerLoop`.
- Produces: `coordinator.async_build_coordinator(hass, entry) -> PvSurplusCoordinator` — constructs miners/clients/fleet from `entry.data` + options.

- [ ] **Step 1: Create `custom_components/pv_surplus_mining/normalize.py`**

```python
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
```

- [ ] **Step 2: Write + run `tests/test_normalize.py`**

```python
import pytest

from custom_components.pv_surplus_mining.normalize import normalize_grid_power


@pytest.mark.parametrize("raw", [None, "unknown", "unavailable", "", "n/a", "NaNx"])
def test_invalid_returns_none(raw):
    assert normalize_grid_power(raw, True) is None


def test_import_positive_passthrough():
    assert normalize_grid_power("1500", True) == 1500.0


def test_export_positive_meter_is_negated():
    # meter reports +export; internal convention is +import, so flip
    assert normalize_grid_power("1500", False) == -1500.0
    assert normalize_grid_power(-800, True) == -800.0
```

Run: `python -m pytest tests/test_normalize.py -v` — PASS.

- [ ] **Step 3: Modify `custom_components/pv_surplus_mining/fleet.py` — add `max_available_state`**

Add this method to `FleetController` (after `max_state`):

```python
    def max_available_state(self, available_ids: set[str]) -> int:
        best = 0
        for sid in sorted(self.states):
            needs = {
                mid for mid, t in self.states[sid].items()
                if t.action == "active" and t.power_w is not None
            }
            if needs <= available_ids:
                best = sid
        return best
```

- [ ] **Step 4: Add a test to `tests/test_fleet.py`**

```python
def test_max_available_state_shrinks_when_miner_unavailable():
    a, b = _ctrl("a", 1), _ctrl("b", 2)
    states = {
        0: {"a": FleetStateTarget(action="sleep"), "b": FleetStateTarget(action="sleep")},
        1: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="sleep")},
        2: {"a": FleetStateTarget(action="active", power_w=2000), "b": FleetStateTarget(action="active", power_w=1500)},
    }
    fc = FleetController({"a": a, "b": b}, states)
    assert fc.max_available_state({"a", "b"}) == 2
    assert fc.max_available_state({"a"}) == 1     # b down -> can't reach state 2
    assert fc.max_available_state(set()) == 0
```

- [ ] **Step 5: Create `custom_components/pv_surplus_mining/coordinator.py`**

```python
"""Control-loop coordinator: read sensors → tick → apply, on the control interval."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS,
    CONF_PV_ENTITY, DOMAIN,
)
from .control.loop import ControlInputs, ControllerLoop
from .errors import AdapterError
from .fleet import FleetController
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig, MinerController
from .models import ControlConfig

_LOGGER = logging.getLogger(__name__)
WARN_TEMP_C = 85.0
CRIT_TEMP_C = 95.0


class PvSurplusCoordinator(DataUpdateCoordinator):
    def __init__(self, hass: HomeAssistant, config: ControlConfig, fleet: FleetController,
                 grid_entity: str, import_positive: bool, pv_entity: str | None = None):
        super().__init__(
            hass, _LOGGER, name=DOMAIN,
            update_interval=timedelta(seconds=config.loop_interval_s),
        )
        self.config = config
        self.fleet = fleet
        self.grid_entity = grid_entity
        self.pv_entity = pv_entity
        self.import_positive = import_positive
        self.loop = ControllerLoop(config, max_available_state=fleet.max_state, current_state=0)
        # operator controls (mutated by entities)
        self.auto_enabled = config.enabled_default
        self.emergency_stop = False
        self.manual_override = False
        self.manual_state = 0
        self.max_state = config.max_state

    def _read_grid(self) -> float | None:
        from .normalize import normalize_grid_power
        state = self.hass.states.get(self.grid_entity)
        return normalize_grid_power(state.state if state else None, self.import_positive)

    async def _async_update_data(self) -> dict:
        statuses = {}
        for mid, ctrl in self.fleet.miners.items():
            try:
                statuses[mid] = await ctrl.get_status()
            except AdapterError:
                statuses[mid] = None

        available_ids = {
            mid for mid, ctrl in self.fleet.miners.items()
            if ctrl.available and statuses.get(mid) is not None and statuses[mid].online
        }
        self.loop.max_available_state = self.fleet.max_available_state(available_ids)

        temps = [s.temp_max_c for s in statuses.values() if s and s.temp_max_c is not None]
        any_warn = any(t >= WARN_TEMP_C for t in temps)
        any_crit = any(t >= CRIT_TEMP_C for t in temps)

        grid_w = self._read_grid()
        sample = grid_w if grid_w is not None else 0.0   # invalid grid -> neutral -> hold (never increase)

        inputs = ControlInputs(
            auto_enabled=self.auto_enabled,
            emergency_stop=self.emergency_stop,
            manual_override=self.manual_override,
            manual_state=self.manual_state,
            max_state=self.max_state,
            all_required_online=(available_ids == set(self.fleet.miners)),
            any_over_temp_warning=any_warn,
            any_over_temp_critical=any_crit,
        )
        decision = self.loop.tick(sample, inputs)

        if decision.changed or decision.emergency:
            try:
                await self.fleet.apply_state(decision.target_state, force=decision.emergency)
            except (AdapterError, KeyError) as exc:
                _LOGGER.warning("apply_state(%s) failed: %s", decision.target_state, exc)

        return {
            "grid_w": grid_w,
            "grid_avg_w": self.loop.grid_avg_w,
            "current_state": self.loop.current_state,
            "target_state": decision.target_state,
            "max_available_state": self.loop.max_available_state,
            "reason": decision.reason,
            "emergency": decision.emergency,
            "miners": {mid: (s.model_dump() if s else None) for mid, s in statuses.items()},
        }


async def async_build_coordinator(hass: HomeAssistant, entry: ConfigEntry) -> PvSurplusCoordinator:
    session = async_get_clientsession(hass)
    data = entry.data
    miners: dict[str, MinerController] = {}
    for m in data[CONF_MINERS]:
        cfg = MinerConfig(
            id=m["id"], model=m["model"], ip=m["ip"], priority=m["priority"],
            min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
            power_targets_w=m.get("power_targets_w", {}),
            command_cooldown_sec=m.get("command_cooldown_sec", 120),
            username=m.get("username", "root"),
        )
        client = AioBraiinsClient(cfg, m["password"], session)
        miners[cfg.id] = MinerController(cfg, client)

    states = load_fleet_states(data[CONF_FLEET_STATES_PATH])
    validate_fleet_states(states, set(miners))
    fleet = FleetController(miners, states)

    config = ControlConfig(**(entry.options or {}))
    coordinator = PvSurplusCoordinator(
        hass, config, fleet,
        grid_entity=data[CONF_GRID_ENTITY],
        import_positive=data.get(CONF_IMPORT_POSITIVE, True),
        pv_entity=data.get(CONF_PV_ENTITY),
    )
    coordinator.config_entry = entry   # explicit (version-independent) — entity.py needs entry_id
    return coordinator
```

> Setting `coordinator.config_entry` explicitly avoids depending on HA's auto-bind (only reliable on newer cores). It matches the attribute HA itself sets, so it is forward-compatible.

- [ ] **Step 6: Modify `custom_components/pv_surplus_mining/__init__.py` — build the coordinator on setup**

Replace the body of `async_setup_entry` (keep `async_unload_entry`):

```python
from .coordinator import async_build_coordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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
```

- [ ] **Step 7: Update `tests/test_init.py` to provide valid config + mock miners**

Replace the test with one that writes a fleet-states file, mocks the miner HTTP with `aioresponses`, and a grid sensor:

```python
import aiohttp  # noqa: F401  (ensures aiohttp import path used by integration)
from aioresponses import aioresponses
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

FLEET_YAML = """
states:
  0:
    s21plus_01: { action: sleep }
  1:
    s21plus_01: { action: active, power_w: 2000 }
"""


def _entry_data(path):
    return {
        CONF_MINERS: [{
            "id": "s21plus_01", "model": "S21+", "ip": "10.0.0.5", "priority": 1,
            "min_power_w": 1400, "max_power_w": 4000, "password": "pw",
            "power_targets_w": {"normal": 3000},
        }],
        CONF_GRID_ENTITY: "sensor.grid_power",
        CONF_IMPORT_POSITIVE: True,
        CONF_FLEET_STATES_PATH: str(path),
    }


def _mock_miner(m):
    base = "http://10.0.0.5/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details", payload={"status": "online"}, repeat=True)
    m.get(f"{base}/miner/stats", payload={"power": {"approx": 1400}, "temp_max_c": 60}, repeat=True)
    m.get(f"{base}/performance/tuner-state", payload={"power_target": {"watt": 1400}}, repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)


async def test_setup_and_unload_entry(hass, tmp_path):
    fleet_file = tmp_path / "fleet-states.yaml"
    fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "500")  # mild import -> hold at 0
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)

    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.data["current_state"] == 0

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
```

- [ ] **Step 8: Write `tests/test_coordinator.py` (injected stub fleet — no HTTP)**

```python
import pytest

from custom_components.pv_surplus_mining.coordinator import PvSurplusCoordinator
from custom_components.pv_surplus_mining.fleet import FleetController
from custom_components.pv_surplus_mining.miner import MinerConfig
from custom_components.pv_surplus_mining.models import (
    CommandResult, ControlConfig, FleetStateTarget, MinerStatus,
)


class StubCtrl:
    def __init__(self, mid, prio, online=True):
        self.cfg = MinerConfig(id=mid, model="m", ip="1.2.3.4", priority=prio, min_power_w=1000, max_power_w=4000)
        self.available = True
        self._online = online
        self.applied = []
    async def get_status(self):
        return MinerStatus(miner_id=self.cfg.id, online=self._online, temp_max_c=60.0, available=self.available)
    async def get_tuner_state(self):
        from custom_components.pv_surplus_mining.models import TunerState
        return TunerState(power_target_w=self.cfg.min_power_w)
    async def set_power_target(self, watt, *, force=False, audit_action=None):
        self.applied.append(watt); return CommandResult(miner_id=self.cfg.id, action="set", target_w=watt, changed=True, verified=True, result="ok")
    async def curtail(self, action, wake_target_w=None):
        self.applied.append(("curtail", action)); return CommandResult(miner_id=self.cfg.id, action="curtail", target_w=None, changed=True, verified=True, result="ok")


def _fleet():
    a = StubCtrl("a", 1)
    states = {0: {"a": FleetStateTarget(action="sleep")},
              1: {"a": FleetStateTarget(action="active", power_w=2000)}}
    return FleetController({"a": a}, states), a


def _coord(hass, cfg=None):
    fleet, a = _fleet()
    cfg = cfg or ControlConfig(loop_interval_s=10, avg_window_s=10, enabled_default=True)
    c = PvSurplusCoordinator(hass, cfg, fleet, grid_entity="sensor.grid_power", import_positive=True)
    return c, a


async def test_invalid_grid_holds_at_zero(hass):
    c, a = _coord(hass)
    hass.states.async_set("sensor.grid_power", "unknown")
    data = await c._async_update_data()
    assert data["current_state"] == 0
    assert a.applied == []   # nothing changed -> no dispatch


async def test_emergency_stop_applies_state_zero(hass):
    c, a = _coord(hass)
    c.loop.current_state = 1   # pretend the fleet is running at state 1
    c.emergency_stop = True
    hass.states.async_set("sensor.grid_power", "-3000")
    data = await c._async_update_data()
    assert data["emergency"] is True and data["target_state"] == 0
    assert a.applied[-1] == ("curtail", "sleep")
```

- [ ] **Step 9: Run the suite**

Run: `python -m pytest -v` — all green.

- [ ] **Step 10: Commit**

```bash
git add custom_components/pv_surplus_mining/normalize.py custom_components/pv_surplus_mining/coordinator.py custom_components/pv_surplus_mining/fleet.py custom_components/pv_surplus_mining/__init__.py tests/test_normalize.py tests/test_coordinator.py tests/test_init.py tests/test_fleet.py
git commit -m "feat: coordinator (read->tick->apply) + grid normalization + setup wiring"
```

---

### Task 6: Config flow + options flow + translations

**Files:**
- Create: `custom_components/pv_surplus_mining/config_flow.py`, `custom_components/pv_surplus_mining/translations/en.json`
- Modify: `custom_components/pv_surplus_mining/const.py` (add `DEFAULT_MINERS`)
- Test: `tests/test_config_flow.py`

**Interfaces:**
- Consumes: `const.*`, `miner.AioBraiinsClient`/`MinerConfig` (connectivity check), `fleet_states.load_fleet_states`/`validate_fleet_states`, `models.ControlConfig`.
- Produces: `config_flow.PvSurplusConfigFlow` (handles `user` step → creates entry with `data` = miners list + entities + paths) and `config_flow.PvSurplusOptionsFlow` (edits `ControlConfig` fields). Error keys: `cannot_connect`, `bad_fleet_states`.

- [ ] **Step 1: Modify `const.py` — append `DEFAULT_MINERS`** (fixed fleet specs, copied from `sol-miner-vs/config/miners.yaml`; IP + password come from the flow):

```python
DEFAULT_MINERS = [
    {"id": "s21plus_01", "model": "Antminer S21+", "priority": 1, "min_power_w": 1400, "max_power_w": 4000,
     "power_targets_w": {"eco": 2000, "normal": 3000, "high": 3600, "max_validated": 4000},
     "command_cooldown_sec": 120, "username": "root"},
    {"id": "s19jproplus_01", "model": "Antminer S19j Pro+", "priority": 2, "min_power_w": 1200, "max_power_w": 3300,
     "power_targets_w": {"eco": 1700, "normal": 2400, "high": 3000, "max_validated": 3300},
     "command_cooldown_sec": 180, "username": "root"},
    {"id": "s19jpro_01", "model": "Antminer S19j Pro", "priority": 3, "min_power_w": 1100, "max_power_w": 3100,
     "power_targets_w": {"eco": 1600, "normal": 2200, "high": 2800, "max_validated": 3100},
     "command_cooldown_sec": 180, "username": "root"},
]
```

- [ ] **Step 2: Create `custom_components/pv_surplus_mining/config_flow.py`**

```python
"""Config + options flow for pv_surplus_mining."""
from __future__ import annotations

import copy
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_BATTERY_ENTITY, CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE,
    CONF_MINERS, CONF_PV_ENTITY, DEFAULT_FLEET_STATES_FILENAME, DEFAULT_MINERS, DOMAIN,
)
from .errors import AdapterError, ConfigError
from .fleet_states import load_fleet_states, validate_fleet_states
from .miner import AioBraiinsClient, MinerConfig
from .models import ControlConfig

_ENTITY_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_OPTIONAL_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))


def _user_schema(defaults: dict) -> vol.Schema:
    fields: dict = {}
    for spec in DEFAULT_MINERS:
        mid = spec["id"]
        fields[vol.Required(f"{mid}_ip", default=defaults.get(f"{mid}_ip", ""))] = str
        fields[vol.Required(f"{mid}_password", default="")] = str
    fields[vol.Required(CONF_GRID_ENTITY, default=defaults.get(CONF_GRID_ENTITY))] = _ENTITY_SENSOR
    fields[vol.Optional(CONF_PV_ENTITY, default=defaults.get(CONF_PV_ENTITY, ""))] = _OPTIONAL_SENSOR
    fields[vol.Optional(CONF_BATTERY_ENTITY, default=defaults.get(CONF_BATTERY_ENTITY, ""))] = _OPTIONAL_SENSOR
    fields[vol.Required(CONF_IMPORT_POSITIVE, default=defaults.get(CONF_IMPORT_POSITIVE, True))] = bool
    fields[vol.Required(CONF_FLEET_STATES_PATH, default=defaults.get(CONF_FLEET_STATES_PATH, ""))] = str
    return vol.Schema(fields)


class PvSurplusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        default_path = self.hass.config.path(DOMAIN, DEFAULT_FLEET_STATES_FILENAME)

        if user_input is not None:
            miners = []
            for spec in DEFAULT_MINERS:
                mid = spec["id"]
                miners.append({**spec, "ip": user_input[f"{mid}_ip"], "password": user_input[f"{mid}_password"]})

            # validate fleet-states file
            try:
                states = load_fleet_states(user_input[CONF_FLEET_STATES_PATH])
                validate_fleet_states(states, {m["id"] for m in miners})
            except ConfigError:
                errors["base"] = "bad_fleet_states"

            # best-effort connectivity check (one login per miner)
            if not errors:
                session = async_get_clientsession(self.hass)
                for m in miners:
                    cfg = MinerConfig(id=m["id"], model=m["model"], ip=m["ip"], priority=m["priority"],
                                      min_power_w=m["min_power_w"], max_power_w=m["max_power_w"],
                                      username=m["username"])
                    try:
                        await AioBraiinsClient(cfg, m["password"], session).login()
                    except (AdapterError, Exception):  # noqa: BLE001 - any network error blocks setup
                        errors["base"] = "cannot_connect"
                        break

            if not errors:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_MINERS: miners,
                    CONF_GRID_ENTITY: user_input[CONF_GRID_ENTITY],
                    CONF_PV_ENTITY: user_input.get(CONF_PV_ENTITY) or None,
                    CONF_BATTERY_ENTITY: user_input.get(CONF_BATTERY_ENTITY) or None,
                    CONF_IMPORT_POSITIVE: user_input[CONF_IMPORT_POSITIVE],
                    CONF_FLEET_STATES_PATH: user_input[CONF_FLEET_STATES_PATH],
                }
                return self.async_create_entry(title="PV-Surplus Mining", data=data, options={})

        defaults = user_input or {CONF_FLEET_STATES_PATH: default_path, CONF_IMPORT_POSITIVE: True}
        return self.async_show_form(step_id="user", data_schema=_user_schema(defaults), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PvSurplusOptionsFlow(config_entry)


_OPTION_KEYS = list(ControlConfig.model_fields.keys())


class PvSurplusOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**ControlConfig().model_dump(), **(self.config_entry.options or {})}
        schema = vol.Schema({
            vol.Required(k, default=current[k]): (bool if isinstance(current[k], bool)
                                                  else (int if isinstance(current[k], int) else float))
            for k in _OPTION_KEYS
        })
        return self.async_show_form(step_id="init", data_schema=schema)
```

- [ ] **Step 3: Create `custom_components/pv_surplus_mining/translations/en.json`**

```json
{
  "config": {
    "step": {
      "user": {
        "title": "PV-Surplus Mining",
        "description": "Enter each miner's IP and password, choose your grid/PV sensors, and point to the fleet-states file.",
        "data": {
          "s21plus_01_ip": "S21+ IP address",
          "s21plus_01_password": "S21+ password",
          "s19jproplus_01_ip": "S19j Pro+ IP address",
          "s19jproplus_01_password": "S19j Pro+ password",
          "s19jpro_01_ip": "S19j Pro IP address",
          "s19jpro_01_password": "S19j Pro password",
          "grid_entity": "Grid power sensor",
          "pv_entity": "Inverter PV sensor (optional)",
          "battery_entity": "Battery SOC sensor (optional)",
          "grid_import_positive": "Grid sensor reports import as positive",
          "fleet_states_path": "Path to fleet-states.yaml"
        }
      }
    },
    "error": {
      "cannot_connect": "Could not reach/authenticate with one or more miners.",
      "bad_fleet_states": "The fleet-states file is missing, invalid, or does not match the configured miners."
    },
    "abort": {
      "already_configured": "PV-Surplus Mining is already configured."
    }
  },
  "options": {
    "step": {
      "init": {
        "title": "Control tuning",
        "description": "Thresholds and durations for the control loop (mirrors control.yaml)."
      }
    }
  }
}
```

- [ ] **Step 4: Write `tests/test_config_flow.py`**

```python
from aioresponses import aioresponses
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pv_surplus_mining.const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, DOMAIN,
)

FLEET_YAML = """
states:
  0:
    s21plus_01: { action: sleep }
    s19jproplus_01: { action: sleep }
    s19jpro_01: { action: sleep }
  1:
    s21plus_01: { action: active, power_w: 2000 }
    s19jproplus_01: { action: sleep }
    s19jpro_01: { action: sleep }
"""

IPS = {"s21plus_01": "10.0.0.21", "s19jproplus_01": "10.0.0.22", "s19jpro_01": "10.0.0.23"}


def _form_input(path):
    out = {CONF_GRID_ENTITY: "sensor.grid_power", CONF_IMPORT_POSITIVE: True, CONF_FLEET_STATES_PATH: str(path)}
    for mid, ip in IPS.items():
        out[f"{mid}_ip"] = ip
        out[f"{mid}_password"] = "pw"
    return out


def _mock_logins(m, ok=True):
    for ip in IPS.values():
        if ok:
            m.post(f"http://{ip}/api/v1/auth/login", payload={"token": "T"})
        else:
            m.post(f"http://{ip}/api/v1/auth/login", status=403)


async def test_full_flow_creates_entry(hass, tmp_path):
    fleet = tmp_path / "fleet-states.yaml"; fleet.write_text(FLEET_YAML)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    with aioresponses() as m:
        _mock_logins(m, ok=True)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(fleet))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"]["miners"]) == 3


async def test_bad_fleet_states_errors(hass, tmp_path):
    missing = tmp_path / "nope.yaml"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(missing))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "bad_fleet_states"


async def test_cannot_connect_errors(hass, tmp_path):
    fleet = tmp_path / "fleet-states.yaml"; fleet.write_text(FLEET_YAML)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with aioresponses() as m:
        _mock_logins(m, ok=False)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(fleet))
    assert result["errors"]["base"] == "cannot_connect"
```

- [ ] **Step 5: Run the suite**

Run: `python -m pytest tests/test_config_flow.py -v` then `python -m pytest -v` — all green.

- [ ] **Step 6: Commit**

```bash
git add custom_components/pv_surplus_mining/config_flow.py custom_components/pv_surplus_mining/translations custom_components/pv_surplus_mining/const.py tests/test_config_flow.py
git commit -m "feat: config flow + options flow + translations"
```

---

### Task 7: Entity platforms (sensor, switch, number)

**Files:**
- Create: `custom_components/pv_surplus_mining/entity.py`, `sensor.py`, `switch.py`, `number.py`
- Modify: `custom_components/pv_surplus_mining/const.py` (`PLATFORMS = ["sensor", "switch", "number"]`)
- Test: `tests/test_entities.py`

**Interfaces:**
- Consumes: `PvSurplusCoordinator` (`.data`, control attributes, `.fleet`, `.async_request_refresh()`).
- Produces: entity classes registered on the three platforms. Setting a switch/number mutates the coordinator control attribute and requests a refresh.

- [ ] **Step 1: Set `PLATFORMS` in `const.py`**

Change `PLATFORMS: list[str] = []` to:
```python
PLATFORMS: list[str] = ["sensor", "switch", "number"]
```

- [ ] **Step 2: Create `custom_components/pv_surplus_mining/entity.py`**

```python
from __future__ import annotations

from homeassistant.helpers.device_info import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PvSurplusCoordinator


class PvSurplusEntity(CoordinatorEntity[PvSurplusCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: PvSurplusCoordinator, key: str, name: str):
        super().__init__(coordinator)
        self._key = key
        self._attr_name = name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name="PV-Surplus Mining",
            manufacturer="Bitmain / Braiins",
        )
```

> Note: `CoordinatorEntity` exposes `coordinator.config_entry` when the coordinator was created during entry setup (HA ≥ 2024.x sets it). If `config_entry` is unset in tests, construct the coordinator via the entry-setup path (Task 5 test) or set `coordinator.config_entry = entry` in the fixture.

- [ ] **Step 3: Create `custom_components/pv_surplus_mining/sensor.py`**

```python
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
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
    _attr_native_unit_of_measurement = "°C"
    _attr_state_class = SensorStateClass.MEASUREMENT
```

- [ ] **Step 4: Create `custom_components/pv_surplus_mining/switch.py`**

```python
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import PvSurplusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add_entities: AddEntitiesCallback):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    add_entities([
        _ControlSwitch(coordinator, "auto_enabled", "Automation enabled"),
        _ControlSwitch(coordinator, "emergency_stop", "Emergency stop"),
        _ControlSwitch(coordinator, "manual_override", "Manual override"),
    ])


class _ControlSwitch(PvSurplusEntity, SwitchEntity):
    @property
    def is_on(self) -> bool:
        return bool(getattr(self.coordinator, self._key))

    async def async_turn_on(self, **kwargs):
        setattr(self.coordinator, self._key, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        setattr(self.coordinator, self._key, False)
        await self.coordinator.async_request_refresh()
```

- [ ] **Step 5: Create `custom_components/pv_surplus_mining/number.py`**

```python
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
    add_entities([
        _ControlNumber(coordinator, "manual_state", "Manual state", 0, top),
        _ControlNumber(coordinator, "max_state", "Max state", 0, top),
    ])


class _ControlNumber(PvSurplusEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator, key, name, lo, hi):
        super().__init__(coordinator, key, name)
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi

    @property
    def native_value(self) -> float:
        return float(getattr(self.coordinator, self._key))

    async def async_set_native_value(self, value: float) -> None:
        setattr(self.coordinator, self._key, int(value))
        await self.coordinator.async_request_refresh()
```

- [ ] **Step 6: Write `tests/test_entities.py`** (full entry setup + assert entities + control write-back)

```python
from aioresponses import aioresponses
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pv_surplus_mining.const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, CONF_MINERS, DOMAIN,
)

FLEET_YAML = """
states:
  0:
    s21plus_01: { action: sleep }
  1:
    s21plus_01: { action: active, power_w: 2000 }
"""


def _entry_data(path):
    return {
        CONF_MINERS: [{"id": "s21plus_01", "model": "S21+", "ip": "10.0.0.5", "priority": 1,
                       "min_power_w": 1400, "max_power_w": 4000, "password": "pw",
                       "power_targets_w": {"normal": 3000}}],
        CONF_GRID_ENTITY: "sensor.grid_power", CONF_IMPORT_POSITIVE: True,
        CONF_FLEET_STATES_PATH: str(path),
    }


def _mock_miner(m):
    base = "http://10.0.0.5/api/v1"
    m.post(f"{base}/auth/login", payload={"token": "T"}, repeat=True)
    m.get(f"{base}/miner/details", payload={"status": "online"}, repeat=True)
    m.get(f"{base}/miner/stats", payload={"power": {"approx": 1400}, "temp_max_c": 60}, repeat=True)
    m.get(f"{base}/performance/tuner-state", payload={"power_target": {"watt": 1400}}, repeat=True)
    m.put(f"{base}/performance/power-target", payload={}, repeat=True)


async def test_entities_created_and_switch_writes_back(hass, tmp_path):
    fleet_file = tmp_path / "fleet-states.yaml"; fleet_file.write_text(FLEET_YAML)
    hass.states.async_set("sensor.grid_power", "100")
    entry = MockConfigEntry(domain=DOMAIN, data=_entry_data(fleet_file), title="PV-Surplus Mining")
    entry.add_to_hass(hass)
    with aioresponses() as m:
        _mock_miner(m)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get("sensor.pv_surplus_mining_fleet_state") is not None
        coordinator = hass.data[DOMAIN][entry.entry_id]
        assert coordinator.auto_enabled is False
        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": "switch.pv_surplus_mining_automation_enabled"}, blocking=True,
        )
    assert coordinator.auto_enabled is True
```

> The exact entity_ids depend on slugified names; if an id differs, look it up via `hass.states.async_all()` in the test rather than hard-coding. The assertion that matters is `coordinator.auto_enabled` flips to `True`.

- [ ] **Step 7: Run the suite**

Run: `python -m pytest -v` — all green.

- [ ] **Step 8: Commit**

```bash
git add custom_components/pv_surplus_mining/entity.py custom_components/pv_surplus_mining/sensor.py custom_components/pv_surplus_mining/switch.py custom_components/pv_surplus_mining/number.py custom_components/pv_surplus_mining/const.py tests/test_entities.py
git commit -m "feat: entity platforms (sensor, switch, number)"
```

---

### Task 8: CI (pytest + hassfest + HACS validation) + install/usage docs

**Files:**
- Create: `.github/workflows/test.yml`, `.github/workflows/validate.yml`
- Modify: `README.md`
- Test: CI runs green on push (verify locally with `python -m pytest -v`).

**Interfaces:** none (CI + docs only).

- [ ] **Step 1: Create `.github/workflows/test.yml`**

```yaml
name: tests
on:
  push:
  pull_request:
jobs:
  pytest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install -r requirements_test.txt
      - run: python -m pytest -v
```

- [ ] **Step 2: Create `.github/workflows/validate.yml`** (hassfest + HACS action)

```yaml
name: validate
on:
  push:
  pull_request:
  schedule:
    - cron: "0 6 * * 1"
jobs:
  hassfest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: home-assistant/actions/hassfest@master
  hacs:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: hacs/action@main
        with:
          category: integration
```

- [ ] **Step 3: Replace `README.md` with install + usage docs**

```markdown
# PV-Surplus Mining — Home Assistant integration (all-in-one)

Consume excess solar by modulating a Braiins OS+ Antminer fleet (S21+, S19j Pro+,
S19j Pro) — entirely inside Home Assistant. No Node-RED, no separate adapter.

## Install (HACS custom repository)

1. HACS → ⋮ → **Custom repositories** → add `https://github.com/Solar-TechNick/pv-surplus-mining-ha`, category **Integration**.
2. Install **PV-Surplus Mining**, then restart Home Assistant.

## Configure

1. Create the fleet-states file the integration reads. Generate it with the
   commissioning sweep from the companion `sol-miner-vs` project, or hand-write
   it in the same format. Default location: `<config>/pv_surplus_mining/fleet-states.yaml`.
   Example (states 0..N, each mapping every miner to `sleep` or an `active` watt target):

       states:
         0:
           s21plus_01: { action: sleep }
           s19jproplus_01: { action: sleep }
           s19jpro_01: { action: sleep }
         1:
           s21plus_01: { action: active, power_w: 2000 }
           s19jproplus_01: { action: sleep }
           s19jpro_01: { action: sleep }

2. Settings → Devices & Services → **Add Integration** → *PV-Surplus Mining*.
   Enter each miner's IP + password, pick your **grid-power sensor** (and PV /
   battery if you have them), set whether the grid sensor reports **import as
   positive**, and confirm the fleet-states path.
3. Tune the control loop later via the integration's **Configure** (options).

Miner passwords are stored in Home Assistant's encrypted `.storage` — never in
a repository file.

## Entities

- Switches: **Automation enabled**, **Emergency stop**, **Manual override**
- Numbers: **Manual state**, **Max state**
- Sensors: **Fleet state**, **Target state**, **Max available state**,
  **Grid power** (+import/−export, `unknown` when the source is invalid),
  **Grid power (avg)**, and per-miner **power** / **temperature**

## Safety

- Grid sensor `unknown`/`unavailable` → the loop holds, never ramps up.
- On HA restart the controller starts at state 0 and reads real miner state
  before ramping.
- Emergency stop (and sustained hard grid import) forces every miner to the
  safe state immediately, bypassing dwell.
- Every miner write is idempotent, rate-limited, verified by re-read, and a
  miner is marked unavailable after repeated failures (the loop then refuses to
  target any state that needs it).

## Fleet & merit order

Antminer **S21+** → **S19j Pro+** → **S19j Pro** (most-efficient first).
```

- [ ] **Step 4: Verify the suite is green locally**

Run: `python -m pytest -v`
Expected: all tests across `tests/` pass.

- [ ] **Step 5: Commit**

```bash
git add .github README.md
git commit -m "ci: pytest + hassfest + HACS validation; docs: install/usage README"
```

---

## Notes for the executor

- **Run tests from the repo root** with the project venv after `pip install -r requirements_test.txt`. `pytest-homeassistant-custom-component` pulls in a matching Home Assistant; the first install is large.
- **Do not import from `sol-miner-vs`.** Every needed module is vendored here.
- **Verbatim ports (Task 2):** `control/decision.py` and `control/loop.py` must match the source except the one import line in `loop.py`. The review should diff them against the source.
- **`config_entry` on the coordinator:** HA sets `coordinator.config_entry` automatically when the coordinator is built during `async_setup_entry` (the path Task 5 uses). The `entity.py` base relies on it for `unique_id`/device. If a unit test constructs the coordinator directly and then builds entities, set `coordinator.config_entry = entry` first.
- **Plan deviation already flagged:** no `export_buffer_w` entity (Task 5 note); thresholds live in the Options flow.

