# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single Home Assistant custom integration (HACS, domain `pv_surplus_mining`) that
consumes excess solar PV by modulating a Braiins OS+ Antminer fleet (S21+, S19j
Pro+, S19j Pro) — no Node-RED, no external adapter. All code lives in
[custom_components/pv_surplus_mining/](custom_components/pv_surplus_mining/).

## Commands

```bash
# Tests MUST run on Python 3.13 — pytest-homeassistant-custom-component pins an HA
# version that requires it; on 3.12 pip backtracks to a harness that fails teardown.
pip install -r requirements_test.txt        # installs the HA + pytest stack
python -m pytest                            # full suite (config in pyproject.toml: asyncio_mode=auto, -q)
python -m pytest tests/test_decision.py     # one file
python -m pytest tests/test_decision.py::test_ramp_up_snaps_directly_to_surplus_target  # one test
python -m pytest -k snap                     # by keyword

# scripts/gen_s21_priority_matrix.py is legacy — the matrix is now auto-generated
# by generate_surplus_fill_states at startup. Keep the script for reference only.
```

CI: `.github/workflows/test.yml` (pytest on 3.13) and `validate.yml`
(`hassfest` + HACS validation; `brands` is intentionally ignored since this is a
custom-repo install, not the HACS default store).

## Architecture — the control pipeline

Data flows **sensors → loop → decision → fleet → miners**, split into a pure core
wrapped by stateful/IO layers so the hard logic is testable without HA or network:

1. **`control/decision.py` — `decide(ctx) -> Decision`** — a pure function, no
   state, no IO. A strict safe-by-default priority ladder: emergency (stop / hard
   import / critical temp / stale telemetry / repeated failures, all bypassing
   dwell) → automation-disabled hold → manual override → fast ramp-down → gated
   ramp-up → hold. **All safety lives here.** (Note: `ControlInputs`/`DecisionContext`
   field *defaults* are fail-open for test convenience — never rely on them; real
   callers populate every field.)

2. **`control/loop.py` — `ControllerLoop`** — holds the temporal state (rolling grid
   average, sustained-export/import timers, per-state dwell, `current_state`) and
   calls `decide()` once per tick. Still no IO. Computes `surplus_target_state` via
   **snap-to-surplus**: the highest fleet state whose total watts fit the current
   surplus budget, so ramps jump straight to the matching state instead of stepping
   one at a time (time gates + `snap_hysteresis_w` still apply). The budget is the
   rolling average of the **CONSERVED available power = `actual_draw_w` (MEASURED
   fleet draw) + instantaneous export − reserve** (`avail_avg_w`) — NOT the matrix
   total, and NOT instantaneous draw mixed with *averaged* export. Two failure modes
   this avoids: (a) anchoring on the matrix total over-states real draw (the slow
   Braiins tuner only reaches a target after minutes) → over-commit → overshoot →
   import → emergency cutoff; (b) mixing instantaneous draw with the 60 s-averaged
   export breaks conservation during a transient (a miner dropping out then
   recovering) — measured draw jumps while the export average still lags high, so the
   budget double-counts headroom and over-commits → import. Averaging the conserved
   `draw + instantaneous export` keeps it internally consistent. (`grid_avg_w` still
   gates the sustained-export/import timers — averaged grid is correct there.)

3. **`coordinator.py` — `PvSurplusCoordinator(DataUpdateCoordinator)`** — the IO
   orchestrator, ticking every `loop_interval_s`. Reads grid/PV sensors, builds
   `ControlInputs`, ticks the loop, and applies the decision to the fleet. Owns all
   **operator-control state** mutated by entities: `auto_enabled`, `emergency_stop`,
   `manual_override`, `manual_state`, `normal_mode`, `pv_mode`, `simulate_grid`, and
   per-miner `miner_enabled` / `miner_power_w` (24-7 power) / `miner_max_w` (surplus
   cap) / `miner_steady` (run ON/OFF at fixed `miner_power_w`, ranked LAST, NOT
   power-modulated — for tuner-sensitive miners that thrash on target changes; see
   the S19j preheat/re-tune gotcha). Regenerates the fleet matrix when per-miner
   enable/cap/steady changes.

4. **`fleet.py` — `FleetController`** — applies a state across miners in priority
   (merit) order. `apply_state(sid)` drives the matrix; `apply_targets({id: w})`
   drives 24-7 mode; `max_available_state()` shrinks the reachable range when miners
   drop out.

5. **`miner.py`** — `AioBraiinsClient` (Braiins OS+ REST at `http://<ip>/api/v1`) +
   `MinerController`, an **idempotent, rate-limited, verified-by-re-read** writer
   that marks a miner unavailable after N consecutive failures. Assumes a single
   serialized writer per miner (the coordinator loop).

6. **`fleet_states.py`** — load/validate a YAML matrix, or generate one.
   `generate_surplus_fill_states` (efficiency-aware "fill the surplus" ladder): each
   rung is the highest-hashrate allocation fitting the current budget; any miner may
   run alone; a less-efficient miner soaks surplus the efficient one can't. Miners are
   ranked by per-miner `efficiency_rank` (lower = more efficient), falling back to
   descending `min_power_w`. Works for any fleet size. The legacy
   `generate_fleet_states` (lowest-minimum-first) remains as a helper.
   State **0 = all miners safe/off** is mandatory.

7. **`models.py`** — pydantic models. `ControlConfig` is the single source of truth
   for every tunable and its default (loop interval, thresholds, durations, dwell,
   `fleet_state_step_w` matrix granularity, etc.).

**HA glue:** `entity.py` (shared `CoordinatorEntity` base) + `sensor.py` / `switch.py`
/ `number.py` platforms (`PLATFORMS` in `const.py`); `config_flow.py` (multi-step
dynamic miner add/edit + options flow for tuning); `__init__.py` setup/unload and the
**v1→v2 migration** (all editable config moved from `entry.data` into `entry.options`).

## Conventions & gotchas

- **Config lives in `entry.options`** (post-v2), merged over `entry.data` as
  `{**data, **options}`. Any options change triggers a full entry reload
  (`_async_reload_on_update`). New persisted config keys generally belong in options.
- **`manifest.json` `version` is the release source of truth** (check it for the
  current value); `pyproject.toml`'s version is stale/unused. Bump the manifest when
  releasing, then `gh release create vX.Y.Z` (HACS installs from the tagged release).
  Commit style: `feat:`/`fix:`/`chore:` with the version bump in the feature commit.
- **Braiins auth quirks** (`miner.py`): the raw token goes in `Authorization` (NOT
  `Bearer <token>`), and every request forces `Connection: close` (the miner drops
  pooled keep-alives, so a fresh connection per request is required for reliability).
  REST responses are parsed for *real firmware* shapes — see the status enum and
  nested `*.watt`/`degree_c` extraction in `get_status()`.
- **The Braiins tuner ramps SLOWLY**: a power-target write is not instantaneous — the
  tuner's `current_target` climbs toward the profile target over ~1–2 min, and each
  miner has a ~120 s command cooldown (`set_power_target` is rate-limited). So
  commanded target ≠ actual draw mid-ramp (use measured draw — see the loop), and
  re-targeting faster than the tuner settles thrashes it (miners report running but
  produce ~0 TH / 0 W). This is why `min_state_dwell_s` ≈ the command cooldown. The
  S19j units also tend to under-reach their targets; cap them with `<id> max power`.
- **Ramp-down / import handling is tunable** (Configure → Tuning, in `ControlConfig`):
  descending the S21+-priority matrix sheds in merit order (S19j first, S21+ last).
  `step_down_required_duration_s` is the grace before any ramp-down;
  `emergency_import_threshold_w` / `emergency_required_duration_s` are the instant
  all-off cutoff. Defaults are aggressive (fast shed); raise them for "keep mining
  through brief import dips, then ramp down gradually" behavior.
- **Grid sign convention**: `grid_import_positive` controls polarity; an
  invalid/`unknown`/`unavailable` grid reading normalizes to a neutral `0.0` sample
  → the loop **holds, never ramps up**. Surplus is negative grid (export).
- **A slept miner is truly paused** (`actions/pause`, ~0 W), not idled at minimum, so
  the fleet can fully stop. `set_power_target` auto-resumes a paused miner first.
- **"Engaged" gate** (`coordinator.py`): the controller only *commands* miners when
  engaged (auto / normal / emergency / manual override). Otherwise it's observe-only.
  The per-miner kill-switch (`miner_enabled=False`) force-pauses even when observe-only.
- **Matrix is auto-generated** from per-miner min/cap unless a `fleet_states_path`
  file exists, in which case it's loaded as-is and never regenerated
  (`_matrix_generated`).
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
- Secrets: miner passwords live only in HA's encrypted `.storage`, never in repo files.

## Tests

Layered to match the architecture — pure layers tested without HA:
`test_decision.py` / `test_loop.py` (pure control logic, no HA), `test_miner.py` /
`test_fleet.py` (mocked Braiins via `aioresponses`), `test_fleet_states_gen.py`,
`test_normalize.py`, and HA-harness tests (`test_config_flow.py`,
`test_coordinator.py`, `test_entities.py`, `test_migration.py`, `test_init.py`,
`test_dashboard.py`). `conftest.py` auto-enables custom integrations for every test.
When changing control behavior, prefer adding/adjusting `test_decision.py` or
`test_loop.py` cases over end-to-end coordinator tests.

## Reference

Design specs and plans for major versions are in
[docs/superpowers/](docs/superpowers/). User-facing docs (install, entities, modes,
safety, fleet/ramp ordering) are in [README.md](README.md). Ready-made fleet matrices
are in [examples/](examples/); the Lovelace dashboard is
[dashboards/pv-surplus-mining.yaml](dashboards/pv-surplus-mining.yaml).
