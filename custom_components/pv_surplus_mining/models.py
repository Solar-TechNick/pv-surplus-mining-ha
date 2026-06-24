from typing import Literal

from pydantic import BaseModel, Field


class FleetStateTarget(BaseModel):
    action: Literal["sleep", "active"] | None = None
    power_w: int | None = None


class TunerState(BaseModel):
    power_target_w: int | None = None
    mode: str | None = None
    profile: str | None = None
    raw: dict = Field(default_factory=dict)


class MinerStatus(BaseModel):
    miner_id: str
    online: bool
    power_target_w: int | None = None
    actual_power_w: int | None = None
    hashrate_ths: float | None = None
    temp_max_c: float | None = None
    tuner_mode: str | None = None
    active_boards: int | None = None
    available: bool = True


class CommandResult(BaseModel):
    miner_id: str
    action: str
    target_w: int | None = None
    changed: bool
    verified: bool
    result: str


class ControlConfig(BaseModel):
    enabled_default: bool = False
    loop_interval_s: float = 10.0
    export_reserve_w: int = 300
    step_up_export_threshold_w: int = 700
    step_up_required_duration_s: float = 180.0
    step_down_import_threshold_w: int = 250
    step_down_required_duration_s: float = 30.0
    emergency_import_threshold_w: int = 1200
    emergency_required_duration_s: float = 5.0
    min_state_dwell_s: float = 120.0
    fallback_state: int = 0
    max_state: int = 14
    avg_window_s: float = 60.0
