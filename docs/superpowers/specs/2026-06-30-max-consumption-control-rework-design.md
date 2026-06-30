# Max-consumption control rework — design

- **Date:** 2026-06-30
- **Status:** Approved (pending written-spec review)
- **Component:** `custom_components/pv_surplus_mining`
- **Target version:** v0.5.0

## 1. Problem

The fleet does not consume available PV surplus. Observed in the field: "in the
morning only one miner runs even though there is surplus for more than one." The
operator earns **nothing** for exported energy, so every exported watt is pure waste.

Two independent root causes were confirmed live (HA at 192.168.1.168, fleet of
S19j Pro+ `.211`, S19j Pro `.210`, S21+ `.212`) on 2026-06-30:

### 1a. Engagement does not survive restarts (the "morning" trigger)

`auto_enabled` and every mode switch are reset to `enabled_default` (`False`) on
**every** coordinator build — i.e. on every HA restart, integration reload, *and*
options edit (`coordinator.py:46`). The switches have no `RestoreEntity` and persist
nothing (`switch.py:27`). At inspection, automation had been **off for 33 h+**; the
controller was observe-only, commanding nothing, while ~2 kW exported. Nothing in the
UI signals that the controller is disengaged. So after any overnight restart/update,
control silently dies until a human notices.

### 1b. Matrix minimum-gap (the wasted export)

The auto-generated matrix (`generate_s21_priority_states`) keeps the **lowest-minimum**
miner (the "pilot", S19j Pro+, min 817 W) running in every active state and only adds
the efficient S21+ **on top**. The S21+'s minimum is **2457 W**, so the matrix jumps
from state 1 = pilot alone (817 W) to state 2 = pilot + S21+ (817 + 2457 = **3274 W**),
with nothing between. The snap budget is `measured_draw + export − reserve`.

Live test (automation ON 5 min, ~2 kW surplus throughout): budget ≈ 705 + 2000 − 300 ≈
**2405 W**. State 2 (3274 W) is unaffordable, so the controller snapped to **state 1
(pilot @ 817 W) and stayed there for the full 5 minutes**, exporting 1.7–2.2 kW while
the far-more-efficient S21+ sat idle. Below ~3.6 kW of surplus the fleet can only ever
run the least-efficient miner and export the rest, even though ~2.5 kW is plenty to run
the S21+ alone.

## 2. Goal

> **Run the fleet to produce the most hashes possible without importing, keeping only a
> small export reserve.**

This single objective is correct precisely because export earns nothing:

- Prefer the efficient S21+ — more hashes per free watt.
- But use the S19j units to soak surplus the S21+ *cannot* (below its 2457 W minimum,
  or above its cap) — free hashes beat exported watts.
- Efficiency is only a tiebreaker between allocations of equal power; **filling the
  surplus is the primary goal.**

Plus: the controller must **reliably stay engaged across restarts**, and its
engaged/disengaged status must be **visible**.

### Target behaviour for this fleet (reserve 300 W)

| Surplus available | Today | Proposed |
|---|---|---|
| ~1.5 kW | pilot @ 817 (export ~700) | S19j+ ramped to ~1.2 kW |
| ~2.7 kW (tested) | **pilot @ 817 (export ~1.9 kW)** | pilot @ ~2.4 kW, then S21+ alone as it grows |
| ~2.8 kW | pilot @ 817 (export ~2.0 kW) | **S21+ alone @ ~2.46 kW** |
| ~3.5 kW | pilot @ 817 (export ~2.7 kW) | S21+ @ ~3.2 kW |
| ~5 kW | pilot + S21+ ramping | S21+ @ cap + S19j soaking the remainder |

## 3. Non-goals (YAGNI)

- **No continuous per-miner allocation.** This was tried (v0.2.9) and deliberately
  retired (v0.3.0) because it thrashed the slow Braiins tuner. We keep the discrete
  matrix + snap-to-surplus + dwell machinery.
- No change to the safety ladder in `decide()` (emergency / stale / temp / failure
  handling stay exactly as they are).
- No battery-aware control, no PV-forecast scheduling, no tariff logic.
- No automatic re-tuning of per-miner caps; caps remain operator-set.

## 4. Design

The architecture stays **sensors → loop → decision → fleet → miners**. Only the matrix
*generator*, the snap/commit logic in the loop, the persistence of operator state, and
a status surface change. `decide()`'s safety ordering is untouched.

### Part 1 — Engagement persistence

Persist operator-control state and restore it **before the first control tick**.

- Add a small persistence helper backed by `homeassistant.helpers.storage.Store`
  (key per config entry, e.g. `pv_surplus_mining.<entry_id>.operator`).
- Persisted keys: `auto_enabled`, `normal_mode`, `manual_override`, `pv_mode`,
  `manual_state`, `max_state`, and per-miner `miner_enabled` / `miner_power_w` /
  `miner_max_w`. **Not** persisted: `emergency_stop`, `simulate_grid`,
  `simulated_grid_w` (transient/test controls — always start clear/false).
- `async_build_coordinator` loads the store and seeds the coordinator's operator state
  *before* the coordinator's first refresh, so the first tick uses the restored values
  rather than the defaults.
- Every operator mutation (switch/number entity) writes back through a coordinator
  setter that persists (debounced save). This also fixes the "editing a tuning value
  silently disables automation" footgun, because an options-edit reload now restores
  the saved operator state instead of falling back to `enabled_default`.
- `enabled_default` flips to **`True`** so a *fresh* install runs out of the box; once
  state has been persisted, the stored value wins.

### Part 2 — Surplus-fill matrix (`generate_surplus_fill_states`)

Replace the auto-generated matrix with an **efficiency-aware fill ladder**. Pure
function; no HA, no IO. Keeps the contract the rest of the code expects: a dict
`{state_id: {miner_id: FleetStateTarget}}` with **state 0 = all sleep**, totals
**monotonic non-decreasing** in `state_id`, every miner present in every state.

**Inputs:** per enabled miner `(id, min_power_w, cap_w, efficiency_rank)`, plus
`fleet_state_step_w`.

**Efficiency rank:** lower rank = more hashes per watt. Sourced (in order):
1. an explicit per-miner config value (new optional `efficiency_rank`, or derived from
   a configured nominal TH/s ÷ nominal W), else
2. a built-in model-name ordering (S21+ > S19j Pro+ > S19j Pro), else
3. fall back to today's heuristic (higher `min_power_w` ⇒ more efficient).

**Generation:** enumerate achievable total-power levels and, for each, keep the
allocation that **maximises estimated total hashrate** subject to: each ON miner within
`[min_i, cap_i]`, each OFF miner at 0, total ≤ the level. Because hashrate is
monotonic in power within a miner's tuner range, the per-level optimum is "load the
most-efficient runnable miner toward its cap first, then the next, respecting minimums";
a miner whose minimum exceeds the remaining headroom stays off. With only 3 miners this
is computed by direct enumeration of the 7 non-empty subsets × `step_w` power levels,
then deduped to a monotonic ladder (keeping the max-hashrate allocation per total).

The coordinator publishes per-state totals to the loop exactly as today
(`_sync_loop_state_power`); snap-to-surplus then selects the rung that best fills the
live surplus, and the rung's allocation is already efficiency-optimal. Regeneration
triggers (per-miner enable/cap change) are unchanged.

### Part 3 — Ramp stability (commitment + hysteresis)

The new ladder introduces **miner swaps** (e.g. "S19j alone @ 2.4 kW" → "S21+ alone @
2.46 kW") at adjacent rungs. Two safeguards keep this from flapping or stalling, given
the slow tuner and the S19j null-consumption under-read:

- **Ramp commitment.** When the controller commands a miner to come up from paused, it
  records the commanded target and a settle deadline (`ramp_commit_settle_s`, default
  ≈ `min_state_dwell_s`). Until the miner's actual draw approaches its target or the
  deadline passes, the budget counts that miner's **committed target** instead of its
  lagging measured draw. This prevents an in-progress ramp/swap from being abandoned
  because reported power hasn't caught up. (Builds on the v0.4.2 null-consumption
  fallback; that handles steady state, this handles the ramp window.)
- **Snap hysteresis.** Add `snap_hysteresis_w` (default ~½ `fleet_state_step_w`):
  step **up** only when `budget` exceeds the next rung's total by the margin; the
  existing import-gated ramp-down and per-state dwell handle the down direction. This
  damps flapping when surplus hovers near a swap boundary (e.g. ~2457 W).

`decide()`'s gates and safety ordering are unchanged; the hysteresis/commitment live in
`loop.py`'s `surplus_target`/budget computation.

### Part 4 — Visibility when disengaged

The controller already computes `reason` and an `engaged` flag but exposes neither.

- Expose `reason` and `engaged` (e.g. a `binary_sensor` "Controller engaged" plus
  `reason` as an attribute on the fleet-state or a small status sensor), so an
  observe-only controller quietly exporting kW is obvious at a glance.
- **Optional / deferred:** a persistent notification after sustained export above a
  threshold while disengaged ("PV-Surplus Mining is OFF — N kW exporting"). Specified
  but not required for v0.5.0; revisit if the status sensor proves insufficient.

## 5. Decisions

- **Export reserve:** keep `export_reserve_w = 300`.
- **Fresh-install default:** `enabled_default = True` (persisted state wins thereafter).
- **Efficiency source:** optional per-miner config, else model-name ordering, else the
  current min-power heuristic. No mandatory new operator input.
- **Notification:** status sensor is in scope; the export-while-disengaged notification
  is optional/deferred.

## 6. Affected files

| File | Change |
|---|---|
| `fleet_states.py` | New `generate_surplus_fill_states` (efficiency-aware fill ladder); route the auto-generated case to it; keep `generate_fleet_states` as the non-priority fallback. |
| `models.py` | `ControlConfig`: add `snap_hysteresis_w`, `ramp_commit_settle_s`; flip `enabled_default` default to `True`. `MinerConfig`: optional `efficiency_rank` / nominal hashrate. |
| `control/loop.py` | Budget hysteresis in `_surplus_target`; ramp-commitment (count committed targets during the settle window). |
| `coordinator.py` | Load persisted operator state before first refresh; track commanded targets + settle deadlines for commitment; expose `reason`/`engaged` in `data`. |
| persistence helper | New module using `helpers.storage.Store`; load/save operator state (debounced). |
| `switch.py` / `number.py` | Mutations persist via coordinator setters. |
| `sensor.py` / `binary_sensor.py` | Surface `engaged` + `reason`. (Adds `binary_sensor` to `PLATFORMS` in `const.py` if used.) |
| `__init__.py` | Wire persistence load into setup; migration if new config keys need defaulting. |
| `config_flow.py` | Optional per-miner efficiency input; tuning fields for the new config keys. |

## 7. Testing

Layered, matching the architecture; pure layers without HA.

- `test_fleet_states_gen.py`: the fill ladder — state 0 all-sleep; monotonic totals;
  **S21+ runs alone** at a budget that fits it but not pilot+S21+; S19j soaks
  sub-S21+-minimum surplus; per-level allocation maximises hashrate; degenerate fleets
  fall back cleanly.
- `test_loop.py`: snap hysteresis (no flap when budget straddles a rung); ramp
  commitment (budget holds during a simulated under-reading ramp so the target isn't
  abandoned).
- `test_coordinator.py`: operator state restored from the store before the first tick;
  a simulated reload restores `auto_enabled=True`; mutations are persisted.
- `test_entities.py`: `engaged`/`reason` surface reflects coordinator state.
- `test_migration.py` / `test_init.py`: new config keys default correctly; store
  absence on a fresh install yields `enabled_default=True`.

Replay value: the field scenario (~2.7 kW surplus, fleet at one miner) should, under
the new generator, select an allocation consuming ≈ budget − reserve and bring the S21+
online by ~2.8 kW — encode this as a loop-level regression test.

## 8. Risks & mitigations

- **Swap disruption:** swapping S19j↔S21+ pauses one and slow-ramps the other, dipping
  consumption for ~1–2 min. Mitigated by hysteresis (swap only when clearly worth it)
  and commitment (don't abandon mid-swap). Acceptable vs. the current permanent waste.
- **Efficiency mis-ranking:** a wrong rank only changes *which* equally-powered
  allocation is chosen, never safety; defaults are sane and the value is configurable.
- **Persistence migration:** existing installs have no store on first upgrade; absence
  must seed from current `enabled_default` semantics without surprising users — covered
  by tests.

## 9. Out of scope / future

- Export-while-disengaged notification (optional, deferred).
- Battery awareness, PV forecasting, tariff/time-of-use logic.
- Auto-tuning per-miner caps from measured efficiency.
