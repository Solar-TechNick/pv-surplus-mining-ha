from custom_components.pv_surplus_mining import async_migrate_entry
from custom_components.pv_surplus_mining.const import CONF_GRID_ENTITY, CONF_MINERS, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_migrate_v1_to_v2(hass):
    v1 = MockConfigEntry(domain=DOMAIN, version=1, data={
        CONF_MINERS: [
            {"id": "s21plus_01", "model": "Antminer S21+", "priority": 1, "min_power_w": 2457, "max_power_w": 6435,
             "power_targets_w": {"normal": 3878}, "command_cooldown_sec": 120, "username": "root",
             "ip": "10.0.0.5", "password": "pw"},
        ],
        CONF_GRID_ENTITY: "sensor.grid", "grid_import_positive": True, "fleet_states_path": "",
    }, options={"loop_interval_s": 10})
    v1.add_to_hass(hass)

    assert await async_migrate_entry(hass, v1) is True
    assert v1.version == 2
    assert v1.data == {}
    miners = v1.options[CONF_MINERS]
    assert miners[0]["id"] == "s21plus_01" and miners[0]["default_power_w"] == 3878
    assert v1.options[CONF_GRID_ENTITY] == "sensor.grid"
    assert v1.options["loop_interval_s"] == 10


async def test_migrate_v1_miners_in_options(hass):
    """Shape-robust: miners already in options (edge case) are handled."""
    v1 = MockConfigEntry(domain=DOMAIN, version=1, data={
        CONF_GRID_ENTITY: "sensor.grid", "grid_import_positive": True, "fleet_states_path": "",
    }, options={
        CONF_MINERS: [
            {"id": "s19j_01", "model": "S19j Pro", "priority": 1, "min_power_w": 1000, "max_power_w": 3000,
             "power_targets_w": {}, "command_cooldown_sec": 120, "username": "root",
             "ip": "10.0.0.6", "password": "pw2"},
        ],
        "loop_interval_s": 30,
    })
    v1.add_to_hass(hass)

    assert await async_migrate_entry(hass, v1) is True
    assert v1.version == 2
    assert v1.data == {}
    miners = v1.options[CONF_MINERS]
    # No power_targets_w["normal"] → falls back to max_power_w
    assert miners[0]["default_power_w"] == 3000
    assert v1.options["loop_interval_s"] == 30


async def test_migrate_v2_is_idempotent(hass):
    """Calling async_migrate_entry on a v2 entry is a no-op and returns True."""
    v2 = MockConfigEntry(domain=DOMAIN, version=2, data={}, options={
        CONF_MINERS: [{"id": "s21plus_01", "min_power_w": 2457, "max_power_w": 6435,
                       "default_power_w": 3878, "model": "S21+", "ip": "10.0.0.5",
                       "password": "pw", "priority": 1}],
        CONF_GRID_ENTITY: "sensor.grid",
    })
    v2.add_to_hass(hass)

    assert await async_migrate_entry(hass, v2) is True
    assert v2.version == 2
    assert v2.data == {}


async def test_migrate_derives_default_from_max_when_no_power_targets(hass):
    """When power_targets_w is absent entirely, default_power_w falls back to max_power_w."""
    v1 = MockConfigEntry(domain=DOMAIN, version=1, data={
        CONF_MINERS: [
            {"id": "s19_01", "model": "S19", "priority": 1, "min_power_w": 1200, "max_power_w": 3250,
             "command_cooldown_sec": 120, "username": "root", "ip": "10.0.0.7", "password": "pw"},
        ],
        CONF_GRID_ENTITY: "sensor.grid", "grid_import_positive": True, "fleet_states_path": "",
    }, options={})
    v1.add_to_hass(hass)

    assert await async_migrate_entry(hass, v1) is True
    miners = v1.options[CONF_MINERS]
    assert miners[0]["default_power_w"] == 3250


async def test_migrate_recomputes_priorities(hass):
    """recompute_priorities is called: miners are re-sorted by min_power_w."""
    v1 = MockConfigEntry(domain=DOMAIN, version=1, data={
        CONF_MINERS: [
            {"id": "big", "model": "Big", "priority": 1, "min_power_w": 3000, "max_power_w": 6000,
             "power_targets_w": {"normal": 4000}, "command_cooldown_sec": 120,
             "username": "root", "ip": "10.0.0.8", "password": "pw"},
            {"id": "small", "model": "Small", "priority": 2, "min_power_w": 1000, "max_power_w": 3000,
             "power_targets_w": {"normal": 2000}, "command_cooldown_sec": 120,
             "username": "root", "ip": "10.0.0.9", "password": "pw"},
        ],
        CONF_GRID_ENTITY: "sensor.grid", "grid_import_positive": True, "fleet_states_path": "",
    }, options={})
    v1.add_to_hass(hass)

    assert await async_migrate_entry(hass, v1) is True
    miners = v1.options[CONF_MINERS]
    # small has lower min_power_w → priority 1 after recompute
    assert miners[0]["id"] == "small" and miners[0]["priority"] == 1
    assert miners[1]["id"] == "big" and miners[1]["priority"] == 2
