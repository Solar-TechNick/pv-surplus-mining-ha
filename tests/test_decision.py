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
    )
    assert decide(ctx).target_state == 3


def test_ramp_down_is_fast():
    ctx = DecisionContext(
        current_state=5, auto_enabled=True, max_state=14, max_available_state=14,
        import_sustained_s=30, step_down_required_duration_s=30,
    )
    assert decide(ctx).target_state == 4


def test_target_clamped_to_effective_max():
    d = decide(DecisionContext(current_state=10, auto_enabled=True, manual_override=True, manual_state=99, max_state=14, max_available_state=6))
    assert d.target_state == 6
