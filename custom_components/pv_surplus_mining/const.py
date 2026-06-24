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
