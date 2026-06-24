"""Helpers for the dynamic miner list (ids, building, priority ordering)."""
from __future__ import annotations

import re


def slugify_id(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s or "miner"


def ensure_unique_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def build_miner(name: str, ip: str, password: str, model: str,
                min_power_w: int, max_power_w: int, default_power_w: int,
                taken_ids: set[str], command_cooldown_sec: int = 120,
                username: str = "root") -> dict:
    mid = ensure_unique_id(slugify_id(name), taken_ids)
    return {
        "id": mid, "name": name, "model": model, "ip": ip, "password": password,
        "username": username, "min_power_w": int(min_power_w),
        "max_power_w": int(max_power_w), "default_power_w": int(default_power_w),
        "command_cooldown_sec": int(command_cooldown_sec),
    }


def recompute_priorities(miners: list[dict]) -> list[dict]:
    """Assign priority 1.. by ascending min_power_w (a miner's explicit
    'priority_override' wins and is used as the sort key when present)."""
    def key(m):
        return (m.get("priority_override", m["min_power_w"]), m["min_power_w"])
    out = sorted(miners, key=key)
    for i, m in enumerate(out, start=1):
        m["priority"] = i
    return out
