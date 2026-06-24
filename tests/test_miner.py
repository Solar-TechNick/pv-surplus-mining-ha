import aiohttp
import pytest
from aioresponses import aioresponses

from custom_components.pv_surplus_mining.errors import (
    MinerUnavailableError, OutOfRangeError, RateLimitedError, UpstreamError,
)
from custom_components.pv_surplus_mining.miner import AioBraiinsClient, MinerConfig, MinerController

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
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})   # current
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})   # verify
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session), clock=clock)
            res = await ctrl.set_power_target(3000)
    assert res.changed is True and res.verified is True and res.result == "ok"


async def test_idempotent_skip_when_already_at_target():
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})
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
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 1500}})  # 2nd call current (!=2000 so idempotent skip is bypassed)
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
            m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})
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
        # verify: get_miner_details returns "paused" status
        m.get(f"{BASE}/miner/details", payload={"status": "paused"})
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
        m.get(f"{BASE}/miner/details", payload={"status": "paused"})
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
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 2000}})
        m.put(f"{BASE}/performance/power-target", payload={})
        m.get(f"{BASE}/performance/tuner-state", payload={"power_target": {"watt": 3000}})
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            ctrl.paused = True  # simulate paused state
            res = await ctrl.set_power_target(3000)
    assert ctrl.paused is False   # resume cleared the flag
    assert res.changed is True and res.verified is True


async def test_pause_verify_lenient_on_unexpected_status():
    """If the details status is not in the paused set, verified=False but no exception."""
    with aioresponses() as m:
        m.post(f"{BASE}/auth/login", payload={"token": "T"})
        m.put(f"{BASE}/actions/pause", payload=True)
        m.get(f"{BASE}/miner/details", payload={"status": "running"})  # unexpected
        async with aiohttp.ClientSession() as session:
            ctrl = MinerController(CFG, await _client(session))
            res = await ctrl.pause()
    assert res.result == "ok"   # no exception
    assert res.verified is False
    assert ctrl.paused is True  # flag still set; pause PUT succeeded
