from pydantic import BaseModel


class DecisionContext(BaseModel):
    grid_avg_w: float = 0.0
    current_state: int = 0
    seconds_since_last_transition: float = 0.0
    export_sustained_s: float = 0.0
    import_sustained_s: float = 0.0
    import_emergency_s: float = 0.0
    auto_enabled: bool = False
    emergency_stop: bool = False
    manual_override: bool = False
    manual_state: int = 0
    max_state: int = 14
    all_required_online: bool = True
    any_fault: bool = False
    any_over_temp_warning: bool = False
    any_over_temp_critical: bool = False
    telemetry_stale: bool = False
    repeated_failures: bool = False
    step_up_required_duration_s: float = 180.0
    step_down_required_duration_s: float = 30.0
    emergency_required_duration_s: float = 5.0
    min_state_dwell_s: float = 120.0
    fallback_state: int = 0
    max_available_state: int = 14
    # The fleet state whose total power best matches the current sustained
    # surplus (computed from the matrix in the loop). Ramp-up/down snap toward
    # this instead of moving one state at a time, so a fine matrix tracks the
    # surplus quickly while the time gates below keep it tuner-safe.
    surplus_target_state: int = 0


class Decision(BaseModel):
    target_state: int
    changed: bool
    emergency: bool
    reason: str


def decide(ctx: DecisionContext) -> Decision:
    effective_max = min(ctx.max_state, ctx.max_available_state)

    def out(target: int, emergency: bool, reason: str) -> Decision:
        t = max(0, min(target, effective_max))
        return Decision(
            target_state=t, changed=(t != ctx.current_state),
            emergency=emergency, reason=reason,
        )

    # 1. Emergency (bypasses all dwell)
    if ctx.emergency_stop:
        return out(ctx.fallback_state, True, "emergency stop")
    if ctx.import_emergency_s >= ctx.emergency_required_duration_s:
        return out(ctx.fallback_state, True, "hard grid import sustained")
    if ctx.any_over_temp_critical:
        return out(ctx.fallback_state, True, "critical temperature")
    if ctx.telemetry_stale:
        return out(ctx.fallback_state, True, "telemetry stale")
    if ctx.repeated_failures:
        return out(ctx.fallback_state, True, "repeated command failures")

    # 2. Automation disabled -> hold
    if not ctx.auto_enabled:
        return out(ctx.current_state, False, "automation disabled")

    # 3. Manual override
    if ctx.manual_override:
        return out(ctx.manual_state, False, "manual override")

    # 4. Automatic: fast ramp-down, then gated ramp-up, else hold.
    # Both directions SNAP toward surplus_target_state (the state matching the
    # current surplus) rather than stepping one state at a time, but always make
    # at least one state of progress so the loop can't stall.
    if (
        ctx.import_sustained_s >= ctx.step_down_required_duration_s
        or ctx.any_fault
        or ctx.any_over_temp_warning
        or ctx.current_state > effective_max
    ):
        return out(min(ctx.surplus_target_state, ctx.current_state - 1), False, "ramp down")

    if (
        ctx.export_sustained_s >= ctx.step_up_required_duration_s
        and ctx.seconds_since_last_transition >= ctx.min_state_dwell_s
        and ctx.all_required_online
        and not ctx.any_fault
        and not ctx.any_over_temp_warning
        and ctx.current_state < effective_max
        and ctx.surplus_target_state > ctx.current_state
    ):
        return out(ctx.surplus_target_state, False, "ramp up")

    return out(ctx.current_state, False, "hold")
