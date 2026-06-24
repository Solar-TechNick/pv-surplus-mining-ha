"""aiohttp Braiins REST client + per-miner safe writer (re-homed from the adapter)."""
from __future__ import annotations

import json
import logging
import time
from typing import Callable, Literal

import aiohttp
from pydantic import BaseModel, Field

from .errors import (
    AdapterError, AuthError, MinerUnavailableError, OutOfRangeError,
    RateLimitedError, UpstreamError,
)
from .models import CommandResult, MinerStatus, TunerState

_AUDIT = logging.getLogger(f"{__package__}.audit")


class MinerConfig(BaseModel):
    id: str
    model: str
    ip: str
    priority: int
    min_power_w: int
    max_power_w: int
    power_targets_w: dict[str, int | None] = Field(default_factory=dict)
    command_cooldown_sec: int = 120
    username: str = "root"


class AioBraiinsClient:
    def __init__(self, cfg: MinerConfig, password: str, session: aiohttp.ClientSession):
        self.cfg = cfg
        self._password = password
        self._session = session
        self._base = f"http://{cfg.ip}/api/v1"
        self.token: str | None = None

    async def login(self) -> None:
        async with self._session.post(
            f"{self._base}/auth/login",
            json={"username": self.cfg.username, "password": self._password},
        ) as resp:
            if resp.status != 200:
                raise AuthError(f"{self.cfg.id}: login failed ({resp.status})")
            data = await resp.json(content_type=None)
        self.token = (data or {}).get("token")
        if not self.token:
            raise AuthError(f"{self.cfg.id}: login response had no token")

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request_json(self, method: str, path: str, **kwargs) -> dict:
        if self.token is None:
            await self.login()
        for attempt in (1, 2):
            async with self._session.request(
                method, f"{self._base}{path}", headers=self._auth_headers(), **kwargs
            ) as resp:
                if resp.status == 401:
                    if attempt == 1:
                        await self.login()
                        continue
                    raise AuthError(f"{self.cfg.id}: re-auth failed for {path}")
                if resp.status >= 500:
                    raise UpstreamError(f"{self.cfg.id}: {method} {path} -> {resp.status}")
                text = await resp.text()
                return json.loads(text) if text else {}
        raise UpstreamError(f"{self.cfg.id}: {method} {path} exhausted retries")

    async def get_miner_details(self) -> dict:
        return await self._request_json("GET", "/miner/details")

    async def get_stats(self) -> dict:
        return await self._request_json("GET", "/miner/stats")

    async def get_tuner_state(self) -> TunerState:
        raw = await self._request_json("GET", "/performance/tuner-state")
        target = (raw.get("power_target") or {}).get("watt")
        return TunerState(power_target_w=target, mode=raw.get("mode"), profile=raw.get("profile"), raw=raw)

    async def set_power_target(self, watt: int) -> None:
        await self._request_json("PUT", "/performance/power-target", json={"watt": watt})


class MinerController:
    """Idempotent, rate-limited, verified, mark-unavailable-after-N writer.
    Assumes a single serialized writer per miner (the coordinator's loop)."""

    def __init__(self, cfg: MinerConfig, client: AioBraiinsClient,
                 clock: Callable[[], float] = time.monotonic, max_failures: int = 3):
        self.cfg = cfg
        self.client = client
        self._clock = clock
        self._max_failures = max_failures
        self.available = True
        self.failure_count = 0
        self._last_command_ts: float | None = None

    def _audit(self, payload: dict) -> None:
        _AUDIT.info("%s", json.dumps(payload))

    async def get_tuner_state(self) -> TunerState:
        return await self.client.get_tuner_state()

    async def get_status(self) -> MinerStatus:
        try:
            details = await self.client.get_miner_details()
            stats = await self.client.get_stats()
            tuner = await self.client.get_tuner_state()
        except AdapterError:
            return MinerStatus(miner_id=self.cfg.id, online=False, available=self.available)
        return MinerStatus(
            miner_id=self.cfg.id,
            online=str(details.get("status", "")).lower() != "offline",
            power_target_w=tuner.power_target_w,
            actual_power_w=(stats.get("power") or {}).get("approx"),
            hashrate_ths=stats.get("hashrate_ths"),
            temp_max_c=stats.get("temp_max_c"),
            tuner_mode=tuner.mode,
            available=self.available,
        )

    def _check_rate_limit(self, force: bool) -> None:
        if force or self._last_command_ts is None:
            return
        if self._clock() - self._last_command_ts < self.cfg.command_cooldown_sec:
            raise RateLimitedError(f"{self.cfg.id}: within command cooldown")

    async def set_power_target(self, watt: int, *, force: bool = False,
                               audit_action: str | None = None) -> CommandResult:
        action = audit_action or "set_power_target"
        if not self.available:
            raise MinerUnavailableError(f"{self.cfg.id}: marked unavailable")
        if not (self.cfg.min_power_w <= watt <= self.cfg.max_power_w):
            raise OutOfRangeError(
                f"{self.cfg.id}: {watt}W outside [{self.cfg.min_power_w},{self.cfg.max_power_w}]"
            )

        current = await self.client.get_tuner_state()
        if current.power_target_w == watt:
            self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "skipped_idempotent"})
            return CommandResult(miner_id=self.cfg.id, action=action, target_w=watt, changed=False, verified=True, result="skipped_idempotent")

        self._check_rate_limit(force)

        try:
            await self.client.set_power_target(watt)
        except AdapterError as exc:
            self.failure_count += 1
            if self.failure_count >= self._max_failures:
                self.available = False
            self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "error", "error": str(exc), "failures": self.failure_count})
            raise

        self.failure_count = 0
        self._last_command_ts = self._clock()
        verified = (await self.client.get_tuner_state()).power_target_w == watt
        self._audit({"miner": self.cfg.id, "action": action, "target_w": watt, "result": "ok", "verified": verified})
        return CommandResult(miner_id=self.cfg.id, action=action, target_w=watt, changed=True, verified=verified, result="ok")

    async def curtail(self, action: Literal["sleep", "wakeup"], wake_target_w: int | None = None) -> CommandResult:
        if action == "sleep":
            target = self.cfg.min_power_w
        elif wake_target_w is not None:
            target = wake_target_w
        else:
            target = self.cfg.power_targets_w.get("normal") or self.cfg.min_power_w
        return await self.set_power_target(int(target), force=(action == "sleep"), audit_action=f"curtail:{action}")
