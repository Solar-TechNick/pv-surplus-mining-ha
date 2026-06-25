from custom_components.pv_surplus_mining.control.decision import DecisionContext, decide


def test_emergency_stop_forces_fallback_state():
    d = decide(DecisionContext(current_state=5, emergency_stop=True, fallback_state=0))
    assert d.target_state == 0 and d.emergency is True and d.changed is True


def test_hard_import_emergency_after_duration():
    d = decide(DecisionContext(current_state=3, import_emergency_s=5, emergency_required_duration_s=5))
    assert d.target_state == 0 and d.emergency is True


def test_disabled_holds_current_state():
    d = decide(DecisionContext(current_state=4, auto_enabled=False))
    assert d.target_state == 4 and d.changed is False and d.reason == "automation disabled"


def test_manual_override_targets_manual_state():
    d = decide(DecisionContext(current_state=1, auto_enabled=True, manual_override=True, manual_state=7, max_state=14, max_available_state=14))
    assert d.target_state == 7 and d.emergency is False


def test_ramp_up_requires_dwell_and_sustained_export():
    ctx = DecisionContext(
        current_state=2, auto_enabled=True, max_state=14, max_available_state=14,
        export_sustained_s=180, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
        surplus_target_state=3,
    )
    assert decide(ctx).target_state == 3


def test_ramp_up_without_sustained_export_holds():
    ctx = DecisionContext(
        current_state=2, auto_enabled=True, max_state=14, max_available_state=14,
        export_sustained_s=10, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
        surplus_target_state=9,
    )
    assert decide(ctx).target_state == 2 and decide(ctx).reason == "hold"


def test_ramp_up_holds_when_surplus_target_not_above_current():
    # Export gate satisfied but the surplus only covers the current state (the
    # gate threshold sits below the reserve buffer) -> do not overshoot.
    ctx = DecisionContext(
        current_state=5, auto_enabled=True, max_state=33, max_available_state=33,
        export_sustained_s=180, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
        surplus_target_state=5,
    )
    assert decide(ctx).target_state == 5 and decide(ctx).reason == "hold"


def test_ramp_up_snaps_directly_to_surplus_target():
    # With a sustained export the controller jumps straight to the state that
    # matches the surplus (not one step at a time).
    ctx = DecisionContext(
        current_state=2, auto_enabled=True, max_state=33, max_available_state=33,
        export_sustained_s=180, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
        surplus_target_state=18,
    )
    assert decide(ctx).target_state == 18


def test_ramp_up_snap_clamped_to_effective_max():
    ctx = DecisionContext(
        current_state=2, auto_enabled=True, max_state=33, max_available_state=6,
        export_sustained_s=180, step_up_required_duration_s=180,
        seconds_since_last_transition=120, min_state_dwell_s=120,
        surplus_target_state=30,
    )
    assert decide(ctx).target_state == 6


def test_ramp_down_snaps_toward_surplus_target():
    # Sustained import: shed straight down to the surplus-matching state.
    ctx = DecisionContext(
        current_state=20, auto_enabled=True, max_state=33, max_available_state=33,
        import_sustained_s=30, step_down_required_duration_s=30,
        surplus_target_state=8,
    )
    assert decide(ctx).target_state == 8


def test_ramp_down_moves_at_least_one_state():
    # Even if the surplus target equals current, the ramp-down branch must make
    # progress downward (never stall while importing).
    ctx = DecisionContext(
        current_state=5, auto_enabled=True, max_state=33, max_available_state=33,
        import_sustained_s=30, step_down_required_duration_s=30,
        surplus_target_state=5,
    )
    assert decide(ctx).target_state == 4


def test_ramp_down_when_current_exceeds_available():
    # A miner went unavailable (effective_max dropped); reduce toward it even
    # though surplus is plentiful.
    ctx = DecisionContext(
        current_state=20, auto_enabled=True, max_state=33, max_available_state=6,
        surplus_target_state=30,
    )
    assert decide(ctx).target_state == 6


def test_target_clamped_to_effective_max():
    d = decide(DecisionContext(current_state=10, auto_enabled=True, manual_override=True, manual_state=99, max_state=14, max_available_state=6))
    assert d.target_state == 6
