DOMAIN = "pv_surplus_mining"

PLATFORMS: list[str] = ["sensor", "switch", "number"]

# Config-entry data keys
CONF_MINERS = "miners"            # list[dict]: {id, model, ip, priority, username, password, min_power_w, max_power_w}
CONF_GRID_ENTITY = "grid_entity"
CONF_PV_ENTITY = "pv_entity"
CONF_BATTERY_ENTITY = "battery_entity"
CONF_IMPORT_POSITIVE = "grid_import_positive"
CONF_FLEET_STATES_PATH = "fleet_states_path"

# Options keys — match the control.yaml schema documented in the design spec.
OPT_LOOP_INTERVAL_S = "loop_interval_s"
OPT_AVG_WINDOW_S = "avg_window_s"
OPT_EXPORT_RESERVE_W = "export_reserve_w"
OPT_STEP_UP_EXPORT_THRESHOLD_W = "step_up_export_threshold_w"
OPT_STEP_UP_REQUIRED_DURATION_S = "step_up_required_duration_s"
OPT_STEP_DOWN_IMPORT_THRESHOLD_W = "step_down_import_threshold_w"
OPT_STEP_DOWN_REQUIRED_DURATION_S = "step_down_required_duration_s"
OPT_EMERGENCY_IMPORT_THRESHOLD_W = "emergency_import_threshold_w"
OPT_EMERGENCY_REQUIRED_DURATION_S = "emergency_required_duration_s"
OPT_MIN_STATE_DWELL_S = "min_state_dwell_s"
OPT_FALLBACK_STATE = "fallback_state"
OPT_MAX_STATE = "max_state"

DEFAULT_FLEET_STATES_FILENAME = "fleet-states.yaml"

DEFAULT_MINERS = [
    {"id": "s21plus_01", "model": "Antminer S21+", "priority": 1, "min_power_w": 1400, "max_power_w": 4000,
     "power_targets_w": {"eco": 2000, "normal": 3000, "high": 3600, "max_validated": 4000},
     "command_cooldown_sec": 120, "username": "root"},
    {"id": "s19jproplus_01", "model": "Antminer S19j Pro+", "priority": 2, "min_power_w": 1200, "max_power_w": 3300,
     "power_targets_w": {"eco": 1700, "normal": 2400, "high": 3000, "max_validated": 3300},
     "command_cooldown_sec": 180, "username": "root"},
    {"id": "s19jpro_01", "model": "Antminer S19j Pro", "priority": 3, "min_power_w": 1100, "max_power_w": 3100,
     "power_targets_w": {"eco": 1600, "normal": 2200, "high": 2800, "max_validated": 3100},
     "command_cooldown_sec": 180, "username": "root"},
]
