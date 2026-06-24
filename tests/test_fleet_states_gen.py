from custom_components.pv_surplus_mining.fleet_states import generate_fleet_states, validate_fleet_states


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
