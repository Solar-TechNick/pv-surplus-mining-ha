# PV-Surplus Mining — Home Assistant integration (all-in-one)

A single Home Assistant custom integration that consumes excess solar power by
modulating a small Antminer fleet (Braiins OS+) instead of exporting to the
grid — **all inside Home Assistant**, with no Node-RED flow and no separate
adapter service.

This is the **all-in-one variant** of the PV-surplus mining controller. The
original multi-component system (Home Assistant + Node-RED + a standalone
Braiins adapter service) lives in a separate project (`sol-miner-vs`) and is
left untouched. This project re-homes the same proven control logic into one
native HA integration.

## What it does

On a fixed control interval the integration:

1. reads your grid-power meter (and inverter PV / optional battery) from
   existing HA entities,
2. runs the deterministic decision loop (rolling average → sustained-condition
   timers → dwell → fleet-state decision; merit order, asymmetric ramping,
   safe-by-default),
3. commands the miners directly over the Braiins REST API with idempotent,
   rate-limited, verify-after-write, mark-unavailable-after-N-failures writes,
4. exposes everything as native HA entities (current fleet state, per-miner
   telemetry, and the operator controls: automation enable, emergency stop,
   manual override, max state, export buffer).

## Status

Design phase. The approved design lives in
[`docs/superpowers/specs/`](docs/superpowers/specs/). Implementation has not
started yet.

## The fleet

Antminer **S21+** (primary) → **S19j Pro+** (secondary) → **S19j Pro**
(tertiary), all on Braiins OS+. Merit order is fixed most-efficient-first.

## Credentials

Miner passwords are entered in the HA config flow and stored in Home
Assistant's encrypted `.storage`. They are **never** written to a config file
in this repository.
