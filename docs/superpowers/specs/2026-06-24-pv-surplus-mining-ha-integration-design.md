# PV-Surplus Mining — All-in-One Home Assistant Integration — Design

**Date:** 2026-06-24
**Status:** Approved (design gate passed; awaiting spec review before plan)
**Project:** `pv-surplus-mining-ha` (standalone repo; HACS custom integration)
**Relationship to prior work:** A second, independent variant of the PV-surplus
mining controller. The original multi-component system — Home Assistant +
Node-RED + a standalone Braiins adapter service — lives in the `sol-miner-vs`
repo and is **left untouched**. This project re-homes the same domain logic
into one native HA integration.

---

## 1. Goal

One Home Assistant custom integration that consumes excess solar power by
modulating a fixed Antminer fleet (Braiins OS+) instead of exporting to the
grid — **entirely inside Home Assistant**. No Node-RED flow, no separate
adapter process, no MQTT bridge. Installed via HACS, configured in the HA UI.

The control behavior is identical to the original system; only the *packaging
and runtime* change. Same fleet, same merit order, same asymmetric ramping,
same safe-by-default guarantees.

## 2. Locked decisions

These were settled during brainstorming and are not open for re-litigation in
the plan:

| # | Decision | Rationale |
|---|---|---|
| D1 | **Single native HA integration** does everything (read sensors, decide, command miners). The miner-write safety layer is absorbed into the integration, not kept as a separate service. | User chose maximal consolidation ("absorb everything into one HA component"). |
| D2 | **HACS install + UI config flow** (no YAML configuration of the integration). | Native/polished; config-flow is the modern HA standard; passwords land in HA's encrypted `.storage`. |
| D3 | **Standalone repo**, HACS-ready layout: `custom_components/pv_surplus_mining/` at repo root + `hacs.json`. | User chose a separate project; this is the clean HACS distribution path. |
| D4 | **Vendor (copy) the pure logic; re-home the HTTP layer onto `aiohttp`.** Do not import from `sol-miner-vs`. | HA integrations must be self-contained and use HA's bundled aiohttp session; the two projects are intentionally independent. |
| D5 | **Fleet-states matrix loaded from a `fleet-states.yaml` file** in the HA config dir (same format the original project's commissioning sweep produces). The sweep is **not** rebuilt inside HA. | A 12–18-row state→watt matrix is unsuitable for a config dialog; reuse the existing artifact. |

## 3. Domain logic preserved (unchanged from the original)

- **Control on grid power** at the connection point. Sign convention fixed:
  positive = import, negative = export/surplus.
- **Profile/state-machine control**, not a fast PID loop. A finite set of
  discrete fleet states (target 12–18), each mapping to explicit per-miner
  watt targets.
- **Merit order fixed:** S21+ → S19j Pro+ → S19j Pro (most efficient first).
- **Asymmetric ramping:** ramp-up slow and gated (sustained rolling-average
  export must clear the next-state threshold while keeping an export buffer;
  dwell on the order of minutes); ramp-down fast; **emergency ramp-down
  bypasses dwell** on hard import or manual emergency stop.
- **Decision precedence** (from the ported `decide()`):
  emergency → disabled-hold → manual override → ramp-down → ramp-up → hold.
- **Safe-by-default:** sensor loss / invalid data / restart → hold or reduce,
  never increase. Writes are idempotent, rate-limited, and verified by
  re-reading miner state. A miner is marked unavailable after N failed writes;
  ramp-up is never blindly retried.

## 4. Architecture

```
Existing HA sensors (grid / PV / battery)
        │  (read each tick from hass.states)
        ▼
DataUpdateCoordinator  ── ControllerLoop (rolling avg, timers, dwell, current_state)
        │                        │ calls
        │                        ▼
        │                   decide(ctx) -> Decision        (ported pure logic)
        ▼                        │
Native HA entities  ◄────────────┤  exposes state + telemetry
(sensors, switches, numbers)     │
                                 ▼
                          MinerController (per miner)
                          aiohttp Braiins REST client +
                          idempotent / rate-limit / verify / mark-unavailable
                                 │
                                 ▼
                          Antminer fleet (Braiins OS+)
                          S21+ (primary) · S19j Pro+ (secondary) · S19j Pro (tertiary)
                          (IPs/credentials supplied at runtime via the config flow)
```

A single `DataUpdateCoordinator` ticks on the configured control interval. Each
tick: read the selected grid/PV/battery entity states → normalize (sign, valid)
→ `ControllerLoop.tick(grid_w, inputs)` → apply the resulting `Decision` to the
fleet via the per-miner controllers → publish updated entity state.

## 5. Repository layout

```
pv-surplus-mining-ha/
├── hacs.json                         # HACS integration manifest
├── README.md
├── custom_components/
│   └── pv_surplus_mining/
│       ├── manifest.json             # domain, version, deps, iot_class
│       ├── __init__.py               # async_setup_entry / unload; build coordinator
│       ├── const.py                  # DOMAIN, defaults, config keys
│       ├── config_flow.py            # UI setup + options flow
│       ├── coordinator.py            # DataUpdateCoordinator: read → decide → apply
│       ├── control/
│       │   ├── decision.py           # vendored pure decide() + DecisionContext/Decision
│       │   └── loop.py               # vendored ControllerLoop + ControlInputs
│       ├── miner.py                  # aiohttp Braiins client + safety (MinerController)
│       ├── fleet.py                  # apply a fleet state in merit order
│       ├── fleet_states.py           # load + validate fleet-states.yaml
│       ├── sensor.py                 # current state + per-miner telemetry entities
│       ├── switch.py                 # auto_enabled / emergency_stop / manual_override
│       ├── number.py                 # manual_state / max_state / export_buffer_w
│       └── translations/en.json
├── docs/superpowers/{specs,plans}/
└── tests/                            # pure-logic tests + HA-harness tests
```

## 6. Reuse vs. re-home

**Ports directly (dependency-free):** `decision.py` and `loop.py` from the
original `adapter/app/control/`. Their existing unit tests come with them.

- `decide(ctx: DecisionContext) -> Decision`
- `ControllerLoop(config, max_available_state, current_state=0)` with
  `tick(grid_w, inputs: ControlInputs | None) -> Decision`

**Re-homed (real work):** the miner-write safety layer
(`braiins_client.py` + `miner_service.py` + `fleet_service.py`). One
substantive change: the HTTP client moves from **`httpx`** to **`aiohttp`**
using HA's shared `async_get_clientsession(hass)` — integrations must not pull
in their own HTTP stack. Token auth, re-auth-once-on-401, the idempotent /
rate-limited / verify-after-write / mark-unavailable-after-N semantics, and the
audit logging are preserved.

**Accepted cost:** the pure logic is duplicated between the two projects. This
is the deliberate price of keeping them independent (D4); they may diverge.

## 7. Configuration (config flow + options)

**Initial config flow (one entry):**
- **Miners:** for each of the three, IP, username (default `root`), password.
  Passwords stored in the config entry (HA encrypted `.storage`).
- **Source entities:** pick the grid-power sensor (required), inverter PV
  sensor (optional), battery SOC sensor (optional).
- **Sign convention:** `grid_import_positive` boolean for the chosen meter.
- **Fleet-states file:** path to `fleet-states.yaml` (default
  `<config>/pv_surplus_mining/fleet-states.yaml`).

**Options flow (editable later, mirrors `control.yaml`):** control interval,
`avg_window_s`, export/import/emergency thresholds and durations, min-state
dwell, fallback/max state. Sensible defaults shipped; all overridable in the UI.

Validation at setup: each miner reachable + authenticates; the fleet-states
file parses and references exactly the configured miners.

## 8. Entities

**Consumed (referenced, not created):** the grid / PV / battery source entities
chosen in the config flow.

**Provided (created by the integration — these replace the old `mining.yaml`
package):**

| Platform | Entity | Role |
|---|---|---|
| switch | `auto_enabled` | enable/disable automatic control |
| switch | `emergency_stop` | force fleet to state 0, overrides everything |
| switch | `manual_override` | take manual control of fleet state |
| number | `manual_state` | the state to force when override is on (0–max) |
| number | `max_state` | cap on how high automation may go |
| number | `export_buffer_w` | export reserve to preserve |
| sensor | `fleet_state` | current fleet state index |
| sensor | per-miner `power_w` / `temp_c` / `available` | live telemetry |
| sensor | `grid_power_w` (normalized) | sign-corrected, `unknown` when source invalid |

Normalization that the original system did in template sensors (sign flip,
unknown-not-zero) moves **into** the integration.

## 9. Control loop & safety behavior

- Coordinator `update_interval` = configured control interval. The
  `ControllerLoop` instance lives in the coordinator and persists rolling
  average, sustained-condition timers, dwell, and `current_state` across ticks.
- **Export/import timers run off the filtered rolling average; the emergency
  timer runs off the raw grid sample** (fast reaction) — exactly as the
  original `ControllerLoop`.
- **On HA start / integration reload:** `current_state` initializes to **0**,
  and the coordinator reads real miner state before any ramp-up. Never assume a
  prior state.
- **Sensor `unknown`/unavailable, parse failure, or stale data:** treated as
  loss → hold-or-reduce, never increase.
- **Miner write failures:** not blind-retried; the affected miner is marked
  unavailable after N failures and excluded; `max_available_state` shrinks so
  the loop cannot target a state that needs an unavailable miner.
- Every write is idempotent (skip if already at target within tolerance),
  rate-limited per `command_cooldown_sec`, and verified by re-reading tuner
  state. One JSON audit line per command via a dedicated logger.

## 10. Testing strategy

- **Pure logic** (`control/decision.py`, `control/loop.py`): plain `pytest`,
  the ported scenario + invariant tests (rising/falling surplus, cloud-edge
  transient, hard-import debounce, emergency-to-zero, dwell respected, state in
  range). No hardware, no HA.
- **Miner layer** (`miner.py`, `fleet.py`): `pytest` with `aioresponses` (or
  equivalent) mocking the Braiins REST endpoints + an injected clock — same
  approach as the original `respx` tests, adapted to aiohttp.
- **HA glue** (`config_flow.py`, `coordinator.py`, entity platforms):
  `pytest-homeassistant-custom-component` — the standard custom-integration
  test harness (a heavier dev dependency, accepted).
- **Static:** `hassfest`/HACS-action validation of `manifest.json` + `hacs.json`
  in CI.

## 11. Error handling

| Condition | Behavior |
|---|---|
| Grid sensor unknown/unavailable/stale | hold or reduce; never ramp up |
| Miner unreachable / auth fails at setup | config flow surfaces an error; setup fails cleanly |
| Miner write fails at runtime | no retry; mark unavailable after N; shrink reachable max state |
| Write target already satisfied | no-op (idempotent) |
| HA restart / reload | start at state 0, read real state before ramping |
| `emergency_stop` on, or hard import sustained past debounce | drop to state 0, bypass dwell |

## 12. Scope

**In scope:** the integration end-to-end — HACS layout, `manifest.json` /
`hacs.json`, config flow + options, coordinator + control loop, vendored
decision/loop logic, aiohttp miner safety layer, fleet-state loading, all
entities, normalization, and the full test suite.

**Out of scope / reused as-is:**
- The **commissioning sweep** stays in the `sol-miner-vs` project; it produces
  the `fleet-states.yaml` this integration consumes.
- **Battery control:** designed as a no-op gate until a battery entity exists
  (same as the original).
- **InfluxDB/Grafana** long-term metrics.
- Distributing to the HACS *default* store (this is a private custom
  repository install).

## 13. Build phases (high level — detailed plan via writing-plans)

1. Repo scaffold: `manifest.json`, `hacs.json`, `const.py`, empty integration
   that loads and unloads cleanly under the HA test harness.
2. Vendor + test the pure decision/loop logic.
3. aiohttp Braiins client + per-miner safety (`miner.py`), mocked tests.
4. Fleet apply in merit order (`fleet.py`) + fleet-states loader/validator.
5. Coordinator: read → normalize → tick → apply; reload/restart safety.
6. Config flow + options flow (+ translations), with setup validation.
7. Entity platforms: sensors, switches, numbers.
8. CI: pytest matrix + hassfest/HACS validation; README install docs.

## 14. Risks / open considerations

- **aiohttp port fidelity:** the safety semantics must survive the httpx→aiohttp
  move intact; the ported safety tests are the guard. Mitigated by porting the
  tests first.
- **Coordinator timing:** HA's coordinator interval has jitter; the loop's
  timers use measured elapsed time (not tick count) to stay correct — carried
  over from the original `ControllerLoop`.
- **HACS-in-subdir:** not applicable here — standalone repo with
  `custom_components/` at root is exactly what HACS expects.

## 15. References

- Original controller design: `sol-miner-vs/docs/superpowers/specs/2026-06-20-pv-surplus-mining-controller-design.md`
- Ported logic source: `sol-miner-vs/adapter/app/control/{decision,loop}.py`
- Safety source: `sol-miner-vs/adapter/app/{braiins_client,miner_service,fleet_service}.py`
- Config formats: `sol-miner-vs/config/{control,miners,fleet-states}.yaml`
- Requirements spec: `sol-miner-vs/Customer-Requirements-PV-Surplus-Mining-Braiins.md`
