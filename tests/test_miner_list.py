from custom_components.pv_surplus_mining.miner_list import (
    slugify_id, ensure_unique_id, build_miner, recompute_priorities,
)


def test_slugify_id():
    assert slugify_id("Antminer S21+ #1") == "antminer_s21_1"
    assert slugify_id("  ") == "miner"


def test_ensure_unique_id():
    assert ensure_unique_id("s21", set()) == "s21"
    assert ensure_unique_id("s21", {"s21"}) == "s21_2"
    assert ensure_unique_id("s21", {"s21", "s21_2"}) == "s21_3"


def test_build_miner_assigns_unique_id_and_normal_target():
    m = build_miner("S21+", "10.0.0.5", "pw", "Antminer S21+", 2457, 6435, 3878, taken_ids=set())
    assert m["id"] == "s21" and m["ip"] == "10.0.0.5" and m["default_power_w"] == 3878
    assert m["username"] == "root" and m["command_cooldown_sec"] == 120


def test_recompute_priorities_by_ascending_min():
    miners = [
        {"id": "big", "min_power_w": 2457},
        {"id": "small", "min_power_w": 817},
        {"id": "mid", "min_power_w": 944},
    ]
    out = recompute_priorities(miners)
    by_id = {m["id"]: m["priority"] for m in out}
    assert by_id == {"small": 1, "mid": 2, "big": 3}
