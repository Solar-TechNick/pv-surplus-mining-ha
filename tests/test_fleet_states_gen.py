from custom_components.pv_surplus_mining.fleet_states import (
    generate_fleet_states, validate_fleet_states,
)


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


# --- Efficiency-aware surplus-fill generator ---

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


def test_fill_fallback_ranks_by_descending_min():
    # No efficiency_rank: higher min_power_w = treated as more efficient, fills first.
    miners = [
        {"id": "lo", "min_power_w": 500, "cap": 1000},
        {"id": "hi", "min_power_w": 1500, "cap": 3000},
    ]
    states = generate_surplus_fill_states(miners, step_w=500)
    # At a budget that fits "hi" alone, "hi" runs and "lo" sleeps
    s = next(sid for sid, tg in states.items()
             if tg["hi"].action == "active" and tg["lo"].action == "sleep")
    assert states[s]["hi"].power_w >= 1500


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
