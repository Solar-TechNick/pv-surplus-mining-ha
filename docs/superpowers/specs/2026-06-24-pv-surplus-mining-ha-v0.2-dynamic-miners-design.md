# PV-Surplus Mining HA Integration v0.2 — Dynamic Miners + Auto-Generated Matrix + Dashboard — Design

**Date:** 2026-06-24
**Status:** Approved (design gate passed; awaiting spec review before plan)
**Project:** `pv-surplus-mining-ha` (extends the merged v0.1.0 integration)
**Builds on:** [v0.1.0 design](2026-06-24-pv-surplus-mining-ha-integration-design.md)

---

## 1. Goal

Remove the two rigid parts of v0.1.0 — the **hard-coded three-miner fleet**
(`DEFAULT_MINERS`) and the **mandatory hand-written `fleet-states.yaml`** — so a
user can add/remove miners individually in the HA UI and have the fleet-state
matrix generated automatically from each miner's power range. Ship a ready-made
Lovelace dashboard. Released as **v0.2.0**.

The control core (decision/loop, miner safety incl. true-off pause, coordinator
tick, safe-by-default) is unchanged; this reshapes *configuration* and adds the
matrix generator + dashboard.

## 2. Locked decisions

| # | Decision |
|---|---|
| D1 | Miners are **dynamic**, added/removed via an **options-flow menu** (not config subentries). `DEFAULT_MINERS` is removed. |
| D2 | Each miner's power range + model are **auto-detected** from the Braiins API on add, with manual fallback. |
| D3 | Ramp order is **smallest-minimum-first**, auto-assigned, with an optional per-miner override. |
| D4 | The fleet-state matrix is **auto-generated** from the miners by default; a custom `fleet-states.yaml` file, if provided, overrides it. |
| D5 | A **Lovelace dashboard** ships as repo YAML using built-in cards only (no extra HACS frontend dependency); per-miner cards are a documented copy-per-miner template. |
| D6 | Existing v0.1.0 config entries are **migrated** to the new shape. |

## 3. Confirmed Braiins REST endpoints (auto-detect)

- `GET /api/v1/configuration/constraints` → `tuner_constraints` with power-target
  **min / max / step** (exact nested field names undocumented → parse leniently).
- `GET /api/v1/miner/details` → `platform` / `miner_identity` (model string).
- `GET /api/v1/performance/tuner-state` → current `power_target.watt` (used to
  seed each miner's `default_power_w`).
- Existing: `POST /auth/login`, `PUT /performance/power-target`,
  `PUT /actions/pause`, `PUT /actions/resume`.

## 4. Config model

A single config entry. **Editable config lives in `entry.options`** so the
options flow can change it; the coordinator reads the merged view
(`{**entry.data, **entry.options}`) and reloads on options update (already wired
in v0.1.0). Shape:

```
options = {
  "grid_entity": "sensor....",
  "pv_entity": "sensor...." | None,
  "battery_entity": "sensor...." | None,
  "grid_import_positive": bool,
  "fleet_states_path": "" | "<path to custom matrix>",   # optional override
  "miners": [ MinerCfg, ... ],
  # control-tuning keys (ControlConfig fields), as in v0.1.0 options
}
```

`MinerCfg` (one per miner):

```
{
  "id": "<slug, unique, derived from name>",   # key in entities + matrix
  "name": "<user label>",
  "model": "<auto-detected or entered>",
  "ip": "<host>",
  "password": "<stored in HA encrypted .storage>",
  "username": "root",
  "min_power_w": int,
  "max_power_w": int,
  "default_power_w": int,                        # top operating point used by the matrix
  "command_cooldown_sec": int,                   # default 120
  "priority": int                                # auto = rank by ascending min_power_w
}
```

`id` is `slugify(name)` made unique within the list (append `_2`, `_3` … on
collision). `priority` is recomputed (ascending `min_power_w`) whenever the miner
list changes, unless a miner carries an explicit user override.

## 5. Setup + management flow (options menu)

**Initial config flow** (`async_step_user`): grid sensor (entity selector),
`grid_import_positive`, optional PV/battery sensors, then **add the first miner**
(so setup yields a working entry). Connectivity-checked as in v0.1.0.

**Options flow** = a **menu** (`async_step_init` → `async_show_menu`):

- **Add miner** — two steps: (1) name / IP / password; (2) the integration logs
  in and queries `configuration/constraints` + `miner/details` + `tuner-state`,
  then shows a confirm form **pre-filled** with detected `model`,
  `min_power_w`, `max_power_w`, `default_power_w` (all editable). On detection
  failure, the confirm form is shown with blank/sensible values and a note. On
  submit, the miner is appended (unique `id`), priorities recomputed.
- **Edit miner** — select from list → pre-filled form → save.
- **Remove miner** — select from list → confirm → remove, recompute priorities.
- **Hub settings** — grid/PV/battery entity, import-sign, optional custom
  fleet-states path.
- **Control tuning** — the `ControlConfig` thresholds (as v0.1.0 options).

Error keys: `cannot_connect`, `bad_fleet_states` (custom file), `duplicate_name`.

## 6. Auto-detect (`miner.py`)

`AioBraiinsClient` gains:

- `get_constraints() -> dict` — `GET /configuration/constraints`; a helper
  `parse_power_constraints(raw) -> (min_w, max_w, step_w)|None` walks
  `tuner_constraints` defensively for the power-target min/max/step (returns
  `None` if not found → caller falls back to manual entry).
- model is read from the existing `get_miner_details()` (`platform` /
  `miner_identity`).

`default_power_w` seeds from `get_tuner_state().power_target_w` (the miner's
current operating point) when available, else the midpoint of min/max.

## 7. Auto-generated matrix (`fleet_states.py`)

New pure function:

```
generate_fleet_states(miners: list[MinerCfg], step_w: int) -> dict[int, dict[str, FleetStateTarget]]
```

Algorithm (reproduces the validated hand-built matrix):
1. Sort miners by `priority` (ascending min).
2. State 0 = every miner `action: sleep` (true-off).
3. For each miner in order, ramp `min_power_w → default_power_w` in increments of
   ~`step_w` (final level = `default_power_w`); earlier miners pinned at their
   `default_power_w`, later miners `sleep`.
4. Each produced state lists **every** miner (sleep or active) so it validates.

`step_w` defaults to the entry's `step_up_export_threshold_w`. The result is
clamped to the 12–18-ish practical range only implicitly (it falls out of the
miners' ranges); no artificial cap. State count is `1 + Σ steps_per_miner`.

**Coordinator** (`async_build_coordinator`): build `MinerController`s from
`options["miners"]`; if `fleet_states_path` is set and the file exists, load +
validate it (override); otherwise `states = generate_fleet_states(miners,
step_up_export_threshold_w)`. Build `FleetController(miners, states)`. The rest
of the tick is unchanged.

## 8. Migration

`async_migrate_entry` (entry **VERSION 1 → 2**): a v0.1.0 entry stored
`data[CONF_MINERS]` (list built from `DEFAULT_MINERS`) + grid/path keys. Migrate
to the new `options` shape: copy miners (deriving `default_power_w` from the old
`power_targets_w["normal"]`, else `max_power_w`; `id`/`name` from the old `id`),
move hub keys into options, recompute priorities. Idempotent and logged.

## 9. Dashboard

`dashboards/pv-surplus-mining.yaml` — built-in cards only:

- **Controls** (`entities` card): `switch.…_normal_mode`, `…_automation_enabled`,
  `…_emergency_stop`, `…_manual_override`, `number.…_manual_state`,
  `number.…_max_state`.
- **System status**: a `gauge` for `sensor.…_grid_power_w` (+import/−export) plus
  an `entities` card for `…_grid_avg_w`, `…_fleet_state`, `…_target_state`,
  `…_max_available_state`.
- **History**: `history-graph` of grid power + fleet state.
- **Per-miner**: a documented **template block** (one `entities`/`gauge` card the
  user copies per miner, substituting `<miner_id>` →
  `sensor.pv_surplus_mining_<miner_id>_power_w` / `_temp_c`). README explains the
  pattern; an `auto-entities`-based variant is noted as an optional HACS add-on.

The README gains a "Dashboard" section (YAML-mode include or manual paste).
Verification: the YAML parses and references only entities the integration
creates.

## 10. Reuse / removals

Reused unchanged: `control/*`, `MinerController` (pause/resume), `FleetController`,
`PvSurplusCoordinator` tick, entity platforms (per-miner sensors already iterate
`fleet.miners`). Changed: `AioBraiinsClient` (+constraints), `config_flow.py`
(initial + options menu), `fleet_states.py` (+generator), `coordinator.py`
(build from dynamic miners + generated matrix), `const.py` (remove
`DEFAULT_MINERS`; keep keys), `__init__.py` (+`async_migrate_entry`, VERSION 2).

## 11. Error handling

| Condition | Behavior |
|---|---|
| Add-miner connectivity/auth fails | `cannot_connect`; miner not added |
| Auto-detect unreachable / unparseable | confirm form falls back to manual entry (note shown) |
| Duplicate miner name | `duplicate_name` |
| Custom fleet-states file missing/invalid/mismatched | `bad_fleet_states`; not saved |
| Zero miners | setup/save blocked (need ≥1) |
| Old (v1) entry | migrated to v2 on load |

Safe-by-default behavior from v0.1.0 (invalid grid → hold, restart → state 0,
mark-unavailable + recovery, emergency bypasses dwell, true-off pause) is
untouched.

## 12. Testing

- Pure: `generate_fleet_states` (smallest-first order, state 0 all-sleep, step
  sizing, cap at `default_power_w`, all-miners-present-per-state); `slugify`/uniq
  id; `parse_power_constraints` (valid/missing/odd shapes).
- Client: `get_constraints` via `aioresponses`.
- Options flow (HA harness): add (auto-detect mocked + manual-fallback), edit,
  remove, duplicate-name, custom-file override + mismatch.
- Coordinator (HA harness): builds from dynamic miners with generated matrix;
  custom-file override path.
- Migration: a synthetic v1 entry → v2 shape.
- Dashboard: YAML parses; entity ids referenced exist in the integration's keyset
  (fixed ones) — per-miner template excluded from that check (documented).

## 13. Build phases (plan will detail)

1. `AioBraiinsClient.get_constraints` + `parse_power_constraints` + tests.
2. `generate_fleet_states` + `slugify`/unique-id helpers + tests.
3. `const.py` keys/cleanup (remove `DEFAULT_MINERS`); MinerCfg shape helpers.
4. Coordinator: build from dynamic miners + generated/override matrix + tests.
5. Config flow rework: initial step + options menu (add/edit/remove/hub/tuning) + tests.
6. `async_migrate_entry` (V1→V2) + tests.
7. Dashboard YAML + README section + parse check.
8. Version bump → 0.2.0; CI; cut `v0.2.0` release.

## 14. Risks

- **Constraints field names undocumented** — mitigated by lenient parsing +
  manual fallback + the confirm form (operator validates).
- **Generated matrix step count** for very wide ranges could exceed ~18 states —
  acceptable (more granularity); could add an optional max-states cap later.
- **Options-flow menu** is more flow code than v0.1.0 — covered by harness tests;
  chosen over subentries specifically for build/test robustness.
