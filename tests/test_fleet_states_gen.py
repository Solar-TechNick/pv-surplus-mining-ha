from custom_components.pv_surplus_mining.fleet_states import (
    generate_fleet_states, generate_s21_priority_states, validate_fleet_states,
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


# --- S21+-priority generator (efficient high-min miner ramped to full first) ---

S21_MINERS = [
    {"id": "pp", "min_power_w": 817, "cap": 3300},    # pilot: lowest minimum
    {"id": "pr", "min_power_w": 944, "cap": 3068},    # middle
    {"id": "s",  "min_power_w": 2457, "cap": 3878},   # priority: highest minimum
]


def test_s21_state_zero_all_sleep_and_validates():
    states = generate_s21_priority_states(S21_MINERS, step_w=200)
    assert all(t.action == "sleep" for t in states[0].values())
    for targets in states.values():
        assert set(targets) == {"pp", "pr", "s"}
    validate_fleet_states(states, {"pp", "pr", "s"})


def test_s21_pilot_holds_tiny_surplus_then_priority_ramps_first():
    states = generate_s21_priority_states(S21_MINERS, step_w=200)
    # state 1: only the lowest-minimum pilot runs, at its minimum
    assert states[1]["pp"].power_w == 817
    assert states[1]["pr"].action == "sleep" and states[1]["s"].action == "sleep"
    # the priority miner (s) reaches its cap before the middle miner ever turns on
    s_at_cap = next(sid for sid in sorted(states)
                    if states[sid]["s"].action == "active" and states[sid]["s"].power_w == 3878)
    pr_on = next((sid for sid in sorted(states) if states[sid]["pr"].action == "active"), 10**9)
    assert s_at_cap < pr_on


def test_s21_totals_monotonic_and_capped():
    states = generate_s21_priority_states(S21_MINERS, step_w=200)
    totals = [_total(states[s]) for s in sorted(states)]
    assert totals == sorted(totals) and totals[0] == 0
    caps = {m["id"]: m["cap"] for m in S21_MINERS}
    for targets in states.values():
        for mid, t in targets.items():
            if t.action == "active":
                assert t.power_w <= caps[mid]
    assert totals[-1] == sum(caps.values())   # top state = every miner at its cap


def test_s21_cap_changes_lower_the_top_total():
    lowered = [
        {"id": "pp", "min_power_w": 817, "cap": 2000},   # cap the S19j Pro+ lower
        {"id": "pr", "min_power_w": 944, "cap": 3068},
        {"id": "s",  "min_power_w": 2457, "cap": 3878},
    ]
    states = generate_s21_priority_states(lowered, step_w=200)
    top = max(_total(states[s]) for s in states)
    assert top == 2000 + 3068 + 3878
    for targets in states.values():
        if targets["pp"].action == "active":
            assert targets["pp"].power_w <= 2000


def test_s21_falls_back_for_non_triple_fleets():
    two = [
        {"id": "a", "min_power_w": 800, "cap": 3000, "default_power_w": 3000, "priority": 1},
        {"id": "b", "min_power_w": 2400, "cap": 3800, "default_power_w": 3800, "priority": 2},
    ]
    states = generate_s21_priority_states(two, step_w=700)
    # same result as the lowest-minimum-first generator
    assert states.keys() == generate_fleet_states(two, step_w=700).keys()
    assert states[1]["a"].power_w == 800 and states[1]["b"].action == "sleep"
