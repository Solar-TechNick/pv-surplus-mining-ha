from unittest.mock import AsyncMock, MagicMock, patch

from aioresponses import aioresponses
from homeassistant.data_entry_flow import FlowResultType

from custom_components.pv_surplus_mining.const import (
    CONF_FLEET_STATES_PATH, CONF_GRID_ENTITY, CONF_IMPORT_POSITIVE, DOMAIN,
)

FLEET_YAML = """
states:
  0:
    s21plus_01: { action: sleep }
    s19jproplus_01: { action: sleep }
    s19jpro_01: { action: sleep }
  1:
    s21plus_01: { action: active, power_w: 2000 }
    s19jproplus_01: { action: sleep }
    s19jpro_01: { action: sleep }
"""

IPS = {"s21plus_01": "10.0.0.21", "s19jproplus_01": "10.0.0.22", "s19jpro_01": "10.0.0.23"}


def _form_input(path):
    out = {CONF_GRID_ENTITY: "sensor.grid_power", CONF_IMPORT_POSITIVE: True, CONF_FLEET_STATES_PATH: str(path)}
    for mid, ip in IPS.items():
        out[f"{mid}_ip"] = ip
        out[f"{mid}_password"] = "pw"
    return out


def _mock_logins(m, ok=True):
    for ip in IPS.values():
        if ok:
            m.post(f"http://{ip}/api/v1/auth/login", payload={"token": "T"})
        else:
            m.post(f"http://{ip}/api/v1/auth/login", status=403)


async def test_full_flow_creates_entry(hass, tmp_path):
    fleet = tmp_path / "fleet-states.yaml"; fleet.write_text(FLEET_YAML)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    # Fully isolate this flow-only test from real aiohttp and from entry setup:
    # mock the connectivity check (client + shared session) so no HA client
    # session is created (which otherwise leaves a lingering shutdown thread the
    # HA harness fails on in teardown), and stub setup/unload so creating the
    # entry doesn't auto-start the coordinator. The real login/setup paths are
    # covered by test_cannot_connect_errors and test_init / test_entities.
    mock_client = MagicMock()
    mock_client.return_value.login = AsyncMock()
    with patch(
        "custom_components.pv_surplus_mining.config_flow.async_get_clientsession",
        return_value=MagicMock(),
    ), patch(
        "custom_components.pv_surplus_mining.config_flow.AioBraiinsClient", mock_client
    ), patch(
        "custom_components.pv_surplus_mining.async_setup_entry", return_value=True
    ), patch(
        "custom_components.pv_surplus_mining.async_unload_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(fleet))
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(result["data"]["miners"]) == 3


async def test_bad_fleet_states_errors(hass, tmp_path):
    missing = tmp_path / "nope.yaml"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(missing))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"]["base"] == "bad_fleet_states"


async def test_cannot_connect_errors(hass, tmp_path):
    fleet = tmp_path / "fleet-states.yaml"; fleet.write_text(FLEET_YAML)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    with aioresponses() as m:
        _mock_logins(m, ok=False)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], _form_input(fleet))
    assert result["errors"]["base"] == "cannot_connect"
