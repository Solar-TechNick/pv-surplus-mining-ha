import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.pv_surplus_mining.errors import (
    MinerUnavailableError, OutOfRangeError, RateLimitedError, UpstreamError,
)
from custom_components.pv_surplus_mining.miner import AioBraiinsClient, MinerConfig, MinerController, parse_power_constraints

CFG = MinerConfig(id="s21plus_01", model="S21+", ip="10.0.0.5", priority=1,
                  min_power_w=1400, max_power_w=4000, command_cooldown_sec=120,
                  power_targets_w={"normal": 3000})
BASE = "http://10.0.0.5/api/v1"


class FakeClock:
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t


async def _client(session):
    c = AioBraiinsClient(CFG, "pw", session)
    return c


async def test_login_and_set_power_target_verifies():
    clock = FakeClock()
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        # current read: different watt → not idempotent, proceed with write
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 2000}}}})
        m.put(f"{BASE}/performance/power-target", payload={})
        # verify read: target watt → verified=True
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 3000}}}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), clock=clock)
            res = await ctrl.set_power_target(3000)
    assert res.changed is True and res.verified is True and res.result == "ok"


async def test_idempotent_skip_when_already_at_target():
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        # current read reports the same watt as requested → idempotent skip
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 3000}}}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.set_power_target(3000)
    assert res.changed is False and res.result == "skipped_idempotent"


async def test_out_of_range_rejected():
    async with aiohttp.ClientSession() as session:
        ctrl = MinerController(CFG, await _client(session))
        with pytest.raises(OutOfRangeError):
            await ctrl.set_power_target(999)


async def test_rate_limited_within_cooldown():
    clock = FakeClock()
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 2000}}}})
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 3000}}}})
        # 2nd call current: !=2000 so idempotent skip is bypassed; but rate limit fires first
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 1500}}}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), clock=clock)
            await ctrl.set_power_target(3000)        # sets _last_command_ts
            clock.t += 10                            # < 120 cooldown
            with pytest.raises(RateLimitedError):
                await ctrl.set_power_target(2000)


async def test_marked_unavailable_after_repeated_failures():
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        # 3 failed writes: each does current-read (200) then PUT (500)
        for _ in range(3):
            m.get(f"{BASE}/performance/tuner-state",
                  payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 2000}}}})
            m.put(f"{BASE}/performance/power-target", status=500)
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), max_failures=3)
            for _ in range(3):
                with pytest.raises(Exception):
                    await ctrl.set_power_target(3000, force=True)
    assert ctrl.available is False
    with pytest.raises(MinerUnavailableError):
        await ctrl.set_power_target(3000, force=True)


# ── Pause / resume tests ──────────────────────────────────────────────────────

async def test_pause_sets_paused_and_verifies():
    """pause() sends PUT /actions/pause, sets paused=True, does a lenient status verify."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.put(f"{BASE}/actions/pause", payload=True)
        # verify: status=3 (paused enum) → verified=True
        m.get(f"{BASE}/miner/details",
              payload={"miner_identity": {"miner_model": "Antminer S21+", "name": "s21plus_01"}, "status": 3})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.pause()
    assert res.changed is True and res.verified is True and res.result == "ok"
    assert ctrl.paused is True


async def test_pause_idempotent_when_already_paused():
    """pause() when already paused returns skipped_idempotent without any HTTP call."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            ctrl.paused = True  # already paused
            res = await ctrl.pause()
    assert res.changed is False and res.result == "skipped_idempotent"
    assert ctrl.paused is True


async def test_pause_failure_increments_and_marks_unavailable():
    """A failing pause PUT (500) increments failure_count and marks unavailable at threshold."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        for _ in range(3):
            m.put(f"{BASE}/actions/pause", status=500)
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), max_failures=3)
            for _ in range(3):
                with pytest.raises(Exception):
                    await ctrl.pause()
    assert ctrl.available is False


async def test_curtail_sleep_calls_pause():
    """curtail('sleep') must call PUT /actions/pause (true off), not set min power."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.put(f"{BASE}/actions/pause", payload=True)
        # status=3 (paused enum) → pause verify returns verified=True
        m.get(f"{BASE}/miner/details",
              payload={"miner_identity": {"miner_model": "Antminer S21+", "name": "s21plus_01"}, "status": 3})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.curtail("sleep")
    assert res.action == "pause"
    assert ctrl.paused is True


async def test_set_power_target_auto_resumes_paused_miner():
    """set_power_target on a paused miner must first PUT /actions/resume, then set power."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.put(f"{BASE}/actions/resume", payload=True)
        # current read: different watt → not idempotent
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 2000}}}})
        m.put(f"{BASE}/performance/power-target", payload={})
        # verify read: target watt → verified=True
        m.get(f"{BASE}/performance/tuner-state",
              payload={"mode_state": {"powertargetmodestate": {"current_target": {"watt": 3000}}}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            ctrl.paused = True  # simulate paused state
            res = await ctrl.set_power_target(3000)
    assert ctrl.paused is False   # resume cleared the flag
    assert res.changed is True and res.verified is True


async def test_resume_failure_increments_and_marks_unavailable():
    """A paused miner whose set_power_target triggers resume(), but /actions/resume
    returns HTTP 500 repeatedly → failure_count increments and available=False at max_failures."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        # Each set_power_target attempt: resume PUT fails (500), no further calls
        for _ in range(3):
            m.put(f"{BASE}/actions/resume", status=500)
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), max_failures=3)
            ctrl.paused = True  # simulate already-paused miner
            for _ in range(3):
                with pytest.raises(Exception):
                    await ctrl.set_power_target(3000, force=True)
    assert ctrl.failure_count == 3
    assert ctrl.available is False


async def test_pause_verify_lenient_on_unexpected_status():
    """If the details status is not in the paused set, verified=False but no exception."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.put(f"{BASE}/actions/pause", payload=True)
        # status=2 (normal/running) is not in (3, 4) → verified=False
        m.get(f"{BASE}/miner/details",
              payload={"miner_identity": {"miner_model": "Antminer S21+", "name": "s21plus_01"}, "status": 2})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.pause()
    assert res.result == "ok"   # no exception
    assert res.verified is False
    assert ctrl.paused is True  # flag still set; pause PUT succeeded


# ── Constraints tests ─────────────────────────────────────────────────────────

def test_parse_power_constraints_nested_watt():
    raw = {"tuner_constraints": {"power_target": {"min": {"watt": 817}, "max": {"watt": 6435}, "step": {"watt": 100}}}}
    assert parse_power_constraints(raw) == (817, 6435, 100)


def test_parse_power_constraints_flat():
    raw = {"tuner_constraints": {"power_target": {"min": 944, "max": 6435}}}
    assert parse_power_constraints(raw) == (944, 6435, 100)   # step defaults to 100


def test_parse_power_constraints_missing_returns_none():
    assert parse_power_constraints({}) is None
    assert parse_power_constraints({"tuner_constraints": {}}) is None


async def test_get_constraints_calls_endpoint():
    cfg = MinerConfig(id="m", model="x", ip="10.0.0.9", priority=1, min_power_w=800, max_power_w=6435)
    base = "http://10.0.0.9/api/v1"
    with aioresponses() as m:
        m.post(f"{base}/auth/login", payload={"token": "T"})
        m.get(f"{base}/configuration/constraints",
              payload={"tuner_constraints": {"power_target": {"min": {"watt": 800}, "max": {"watt": 6435}}}})
        async with aiohttp.ClientSession() as session:
            raw = await AioBraiinsClient(cfg, "pw", session).get_constraints()
    assert parse_power_constraints(raw) == (800, 6435, 100)
