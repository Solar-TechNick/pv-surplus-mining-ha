"""Apply a fleet state across miners in merit order (re-homed FleetService)."""
from __future__ import annotations

from .errors import AdapterError, ConfigError
from .miner import MinerController
from .models import CommandResult, FleetStateTarget


class FleetController:
    def __init__(self, miners: dict[str, MinerController],
                 states: dict[int, dict[str, FleetStateTarget]]):
        self.miners = miners
        self.states = states
        for state_id, targets in states.items():
            missing = set(miners) - set(targets)
            if missing:
                raise ConfigError(f"fleet state {state_id} omits miner(s): {sorted(missing)}")

    @property
    def max_state(self) -> int:
        return max(self.states) if self.states else 0

    def max_available_state(self, available_ids: set[str]) -> int:
        best = 0
        for sid in sorted(self.states):
            needs = {
                mid for mid, t in self.states[sid].items()
                if t.action == "active" and t.power_w is not None
            }
            if needs <= available_ids:
                best = sid
        return best

    async def apply_state(self, state_id: int, *, force: bool = False) -> list[CommandResult]:
        if state_id not in self.states:
            raise KeyError(f"unknown fleet state {state_id}")
        targets = self.states[state_id]
        ordered = sorted(self.miners.values(), key=lambda m: m.cfg.priority)
        results: list[CommandResult] = []
        for svc in ordered:
            target = targets.get(svc.cfg.id)
            try:
                if target is None or target.action == "sleep" or target.power_w is None:
                    results.append(await svc.curtail("sleep"))
                else:
                    results.append(await svc.set_power_target(target.power_w, force=force))
            except AdapterError as exc:
                results.append(CommandResult(
                    miner_id=svc.cfg.id, action="apply_state", target_w=None,
                    changed=False, verified=False, result=f"error:{exc.__class__.__name__}",
                ))
        return results

    async def get_state(self) -> dict:
        miners_state: dict[str, int | None] = {}
        for mid, svc in self.miners.items():
            try:
                miners_state[mid] = (await svc.get_tuner_state()).power_target_w
            except AdapterError:
                miners_state[mid] = None
        matched = None
        for sid, targets in self.states.items():
            if all(
                (t.action == "sleep" or t.power_w is None) and miners_state.get(mid) in (None, 0, self.miners[mid].cfg.min_power_w)
                or (t.power_w is not None and miners_state.get(mid) == t.power_w)
                for mid, t in targets.items()
            ):
                matched = sid
                break
        return {"miners": miners_state, "matched_state": matched}
