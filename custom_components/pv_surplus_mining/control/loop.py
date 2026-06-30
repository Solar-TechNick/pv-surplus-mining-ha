from collections import deque
from statistics import mean

from pydantic import BaseModel

from ..models import ControlConfig
from .decision import DecisionContext, Decision, decide


# NOTE: these defaults are fail-OPEN (healthy/enabled) so a caller sets only what
# it exercises. A production caller MUST populate every field from real telemetry
# and never rely on these defaults; the safe-by-default posture lives in
# decide()/DecisionContext, not here.
class ControlInputs(BaseModel):
    auto_enabled: bool = True
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


class ControllerLoop:
    """Pure stateful controller. Holds the temporal state (rolling average,
    sustained-condition timers, dwell, current_state) and calls the pure
    decide() each tick. No I/O. Time advances by dt = loop_interval_s per tick."""

    def __init__(self, config: ControlConfig, max_available_state: int, current_state: int = 0):
        self.config = config
        self.max_available_state = max_available_state
        self.current_state = current_state
        self.seconds_since_last_transition = 0.0
        self.export_sustained_s = 0.0
        self.import_sustained_s = 0.0
        self.import_emergency_s = 0.0
        self.grid_avg_w = 0.0
        self.surplus_target_state = current_state
        # Total miner watts per fleet-state index, set by the coordinator from the
        # active matrix (and refreshed when it is rebuilt). Empty => no snapping.
        self.state_power_w: dict[int, float] = {}
        # MEASURED fleet draw (sum of actual miner watts), set by the coordinator
        # each tick. None => fall back to the current state's matrix total. Using
        # the measured draw is critical: the Braiins tuner ramps toward a target
        # over minutes and the matrix totals assume the full target is reached, so
        # anchoring the budget to the matrix total over-estimates available power
        # and over-commits the fleet to high states, which then overshoot the real
        # surplus, import, and trip the fleet off.
        self.actual_draw_w: float | None = None
        # Rolling average of the CONSERVED "available" power (draw + instantaneous
        # export) — the budget signal. Kept separate from grid_avg_w (which still
        # gates the sustained-export/import timers on averaged grid).
        self.avail_avg_w = 0.0
        dt = config.loop_interval_s
        self._window: deque[float] = deque(maxlen=max(1, round(config.avg_window_s / dt)))
        self._avail_window: deque[float] = deque(maxlen=max(1, round(config.avg_window_s / dt)))

    def _surplus_target(self) -> int:
        """Highest fleet state whose total power fits the current surplus budget.

        The budget is the rolling-average CONSERVED available power (what the fleet
        actually draws + the instantaneous exported surplus, ≈ PV − house load) minus
        the reserve we keep exporting. It is computed from `avail_avg_w` — NOT from
        instantaneous draw plus averaged export, which mix time bases and over-count
        headroom during transients (a miner dropping out then recovering), causing
        over-commit and grid import. Falls back to the current state when the matrix
        totals are unknown (no snapping)."""
        if not self.state_power_w:
            return self.current_state
        budget = self.avail_avg_w - self.config.export_reserve_w
        hys = self.config.snap_hysteresis_w
        best = 0
        for sid, total in sorted(self.state_power_w.items(), key=lambda kv: kv[1]):
            margin = hys if sid > self.current_state else 0
            if total <= budget - margin:
                best = sid
        return best

    def tick(self, grid_w: float, inputs: ControlInputs | None = None) -> Decision:
        inputs = inputs or ControlInputs()
        c = self.config
        dt = c.loop_interval_s

        self._window.append(grid_w)
        self.grid_avg_w = mean(self._window)

        # Budget signal: average the conserved available power (draw + instantaneous
        # export) over the same window, so a fast change in either term stays
        # internally consistent (see _surplus_target). -grid_w = exported surplus.
        current_draw = (self.actual_draw_w if self.actual_draw_w is not None
                        else self.state_power_w.get(self.current_state, 0.0))
        self._avail_window.append(current_draw - grid_w)
        self.avail_avg_w = mean(self._avail_window)

        self.export_sustained_s = (
            self.export_sustained_s + dt if self.grid_avg_w <= -c.step_up_export_threshold_w else 0.0
        )
        self.import_sustained_s = (
            self.import_sustained_s + dt if self.grid_avg_w >= c.step_down_import_threshold_w else 0.0
        )
        self.import_emergency_s = (
            self.import_emergency_s + dt if grid_w >= c.emergency_import_threshold_w else 0.0
        )

        self.surplus_target_state = self._surplus_target()

        ctx = DecisionContext(
            grid_avg_w=self.grid_avg_w,
            current_state=self.current_state,
            seconds_since_last_transition=self.seconds_since_last_transition,
            export_sustained_s=self.export_sustained_s,
            import_sustained_s=self.import_sustained_s,
            import_emergency_s=self.import_emergency_s,
            surplus_target_state=self.surplus_target_state,
            step_up_required_duration_s=c.step_up_required_duration_s,
            step_down_required_duration_s=c.step_down_required_duration_s,
            emergency_required_duration_s=c.emergency_required_duration_s,
            min_state_dwell_s=c.min_state_dwell_s,
            fallback_state=c.fallback_state,
            max_available_state=self.max_available_state,
            **inputs.model_dump(),
        )
        decision = decide(ctx)

        if decision.target_state != self.current_state:
            self.current_state = decision.target_state
            self.seconds_since_last_transition = 0.0
        else:
            self.seconds_since_last_transition += dt

        return decision
