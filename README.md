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
