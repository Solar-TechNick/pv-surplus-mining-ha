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

- Switches: **Automation enabled**, **Emergency stop**, **Manual override**,
  **Normal mode**
- Numbers: **Manual state**, **Max state**
- Sensors: **Fleet state**, **Target state**, **Max available state**,
  **Grid power** (+import/−export, `unknown` when the source is invalid),
  **Grid power (avg)**, and per-miner **power** / **temperature**

### Modes

- **PV surplus** (default): the controller follows grid surplus, ramping miners
  up/down to consume export without importing.
- **Normal mode** (`Normal mode` switch on): every available miner runs at its
  default power 24/7, ignoring surplus. **Emergency stop still overrides it.**

## Safety

- A slept miner is **truly paused** (Braiins `actions/pause`, ~0 W), not idled at
  its minimum — so the fleet can fully stop when there's no surplus.
- Grid sensor `unknown`/`unavailable` → the loop holds, never ramps up.
- On HA restart the controller starts at state 0 and reads real miner state
  before ramping.
- Emergency stop (and sustained hard grid import) forces every miner to the
  safe state immediately, bypassing dwell.
- Every miner write is idempotent, rate-limited, verified by re-read, and a
  miner is marked unavailable after repeated failures (the loop then refuses to
  target any state that needs it).

## Dashboard

A ready-made dashboard is in [`dashboards/pv-surplus-mining.yaml`](dashboards/pv-surplus-mining.yaml).
Add it via **Settings → Dashboards → ⋮ → New dashboard from YAML** (or paste the
cards into an existing dashboard in YAML edit mode). It shows the mode/control
switches, a grid-power gauge (+import / −export), fleet/target/max-available
state, and a 24 h surplus-tracking graph. Per-miner power/temperature cards are
a copy-per-miner template in the YAML's trailing comment — duplicate one block
per miner, replacing `<id>` with your miner's slug.

## Fleet & ramp order

Ramps **lowest-minimum first** to capture small surpluses — Antminer
**S19j Pro+** → **S19j Pro** → **S21+** (the S21+'s high minimum power means it
only joins once there's a large, stable surplus). See
[`examples/fleet-states.yaml`](examples/fleet-states.yaml) for a complete
state matrix built from real tuner ranges.
