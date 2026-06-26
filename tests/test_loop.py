from custom_components.pv_surplus_mining.models import ControlConfig
from custom_components.pv_surplus_mining.control.loop import ControllerLoop, ControlInputs


def _cfg(**kw):
    base = dict(loop_interval_s=10, avg_window_s=10, step_up_export_threshold_w=700,
               step_down_import_threshold_w=250, emergency_import_threshold_w=1200,
               step_up_required_duration_s=20, step_down_required_duration_s=10,
               emergency_required_duration_s=10, min_state_dwell_s=10, max_state=14)
    base.update(kw)
    return ControlConfig(**base)


def test_emergency_timer_uses_raw_sample_not_average():
    # window maxlen = round(100/10) = 10. Prime with 9 zeros so the rolling
    # average stays well below the emergency threshold while the raw sample spikes.
    loop = ControllerLoop(_cfg(avg_window_s=100, loop_interval_s=10), max_available_state=14, current_state=3)
    for _ in range(9):
        loop.tick(0, ControlInputs(auto_enabled=True))
    d = loop.tick(5000, ControlInputs(auto_enabled=True))   # window=[0×9, 5000] -> avg 500
    assert loop.grid_avg_w == 500                            # average alone would NOT fire emergency
    assert loop.import_emergency_s == 10                     # but raw 5000 >= 1200 accrues one dt
    assert d.target_state == 0 and d.emergency is True       # emergency fires off the RAW sample


def test_rolling_average_suppresses_single_export_spike():
    # window maxlen = round(100/10) = 10. One -5000 spike among 9 zeros averages to
    # -500, which is ABOVE the -700 export threshold -> no export accrual, no ramp-up.
    loop = ControllerLoop(_cfg(avg_window_s=100, loop_interval_s=10, step_up_export_threshold_w=700),
                          max_available_state=14, current_state=0)
    for _ in range(9):
        loop.tick(0, ControlInputs(auto_enabled=True))
    loop.tick(-5000, ControlInputs(auto_enabled=True))
    assert loop.grid_avg_w == -500
    assert loop.export_sustained_s == 0   # spike suppressed by the average; no sustained export


def test_dwell_resets_on_transition_timers_persist():
    loop = ControllerLoop(_cfg(), max_available_state=14, current_state=0)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000}
    loop.export_sustained_s = 999
    loop.seconds_since_last_transition = 999
    loop.tick(-5000, ControlInputs(auto_enabled=True))  # ramp up
    assert loop.current_state > 0                         # a transition happened
    assert loop.seconds_since_last_transition == 0.0


def test_surplus_target_picks_highest_state_within_budget():
    # Budget = current draw (state 0 = 0 W) + export 5000 - reserve 300 = 4700 W.
    # Highest state whose total <= 4700 is state 4 (4000 W); state 5 (5000) is too big.
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=300),
                          max_available_state=14, current_state=0)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000, 4: 4000, 5: 5000}
    loop.tick(-5000, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 4


def test_surplus_target_counts_current_draw_as_available():
    # Measured draw 4000 W, now near balance (export 200). Budget =
    # 4000 + 200 - 300 = 3900 -> highest state <= 3900 is state 3. Snap down a bit.
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=300),
                          max_available_state=14, current_state=4)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000, 4: 4000, 5: 5000}
    loop.actual_draw_w = 4000
    loop.tick(-200, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 3


def test_surplus_target_uses_measured_draw_not_matrix_total():
    # The fleet is parked at state 5 (matrix total 5000) but is only DRAWING 2000 W
    # (tuner still ramping). With 1000 W export the real budget is 2000+1000-300 =
    # 2700 -> state 2, NOT something near state 5. This is what stops the controller
    # over-committing to a high state and overshooting the surplus.
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=300),
                          max_available_state=14, current_state=5)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000, 4: 4000, 5: 5000}
    loop.actual_draw_w = 2000
    loop.tick(-1000, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 2


def test_surplus_target_zero_when_importing():
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10, export_reserve_w=300),
                          max_available_state=14, current_state=2)
    loop.state_power_w = {0: 0, 1: 1000, 2: 2000, 3: 3000}
    loop.tick(1500, ControlInputs(auto_enabled=True))   # importing
    assert loop.surplus_target_state == 0


def test_surplus_target_defaults_to_current_without_matrix():
    # No matrix info -> no snap target; hold at current (decide() then holds).
    loop = ControllerLoop(_cfg(avg_window_s=10, loop_interval_s=10), max_available_state=14, current_state=3)
    loop.tick(-5000, ControlInputs(auto_enabled=True))
    assert loop.surplus_target_state == 3
