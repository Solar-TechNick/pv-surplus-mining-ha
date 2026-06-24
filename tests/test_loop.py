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
    loop.export_sustained_s = 999
    loop.seconds_since_last_transition = 999
    loop.tick(-5000, ControlInputs(auto_enabled=True))  # ramp up
    assert loop.current_state == 1
    assert loop.seconds_since_last_transition == 0.0
