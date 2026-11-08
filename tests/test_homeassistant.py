"""Use real HA config flows, action schemas, and status entities."""

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.data_entry_flow import FlowManagerResourceView
from homeassistant.setup import async_setup_component

from custom_components.powerwall_force import DISPATCH_SCHEMA, async_setup
from custom_components.powerwall_force.const import DOMAIN
from custom_components.powerwall_force.protocol import AuthenticationError, PowerwallError

DATA = {
    "host": "192.168.1.50",
    "password": "abcde",
    "private_key_path": "/config/key.pem",
    "max_charge_power_w": 5000,
    "max_discharge_power_w": 10000,
}


def make_entry(**kwargs):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Powerwall Force",
        data=DATA,
        options={},
        source="user",
        unique_id="DIN",
        discovery_keys={},
        subentries_data=None,
        **kwargs,
    )


async def test_config_flow_validates_read_only(hass, rsa_key):
    with (
        patch(
            "custom_components.powerwall_force.config_flow.load_private_key", return_value=rsa_key
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_connect",
            AsyncMock(return_value="DIN"),
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_dispatch", AsyncMock()
        ) as dispatch,
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_resume", AsyncMock()
        ) as resume,
        patch("homeassistant.config_entries.ConfigEntry.async_setup", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] == "menu"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "existing_key"}
        )
        assert result["type"] == "form"
        view = FlowManagerResourceView(hass.config_entries.flow)
        assert len(view._prepare_result_json(result)["data_schema"]) == 5
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        await hass.async_block_till_done()
        assert result["type"] == "create_entry"
        assert result["result"].unique_id == "DIN"
        assert result["data"] == DATA
        dispatch.assert_not_awaited()
        resume.assert_not_awaited()


@pytest.mark.parametrize(
    "failure,error",
    [
        (AuthenticationError("Key rejected"), "invalid_auth"),
        (PowerwallError("Offline"), "cannot_connect"),
    ],
)
async def test_flow_handles_gateway_errors(hass, rsa_key, failure, error):
    with (
        patch(
            "custom_components.powerwall_force.config_flow.load_private_key", return_value=rsa_key
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_connect",
            AsyncMock(side_effect=failure),
        ),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"next_step_id": "existing_key"}
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        assert result["type"] == "form"
        assert result["errors"] == {"base": error}


async def test_setup_actions_sensor_and_unload(hass, rsa_key):
    entry = make_entry()
    # Register without ConfigEntries.async_add's automatic setup so mocks cover setup.
    hass.config_entries._entries[entry.entry_id] = entry
    with (
        patch("custom_components.powerwall_force.load_private_key", return_value=rsa_key),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_connect",
            AsyncMock(return_value="DIN"),
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_read_telemetry",
            AsyncMock(side_effect=PowerwallError("Telemetry unsupported")),
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_dispatch", AsyncMock()
        ) as dispatch,
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_resume", AsyncMock()
        ) as resume,
    ):
        assert await async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        state = hass.states.get("sensor.powerwall_force_dispatch_status")
        assert state is not None
        assert state.state == "idle"
        await hass.services.async_call(DOMAIN, "charge", {"duration": "00:01:00"}, blocking=True)
        await hass.async_block_till_done()
        dispatch.assert_awaited_once_with(-5000, 30)
        assert hass.states.get(state.entity_id).state == "charge"
        assert hass.states.get(state.entity_id).attributes["requested_power_w"] == -5000
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        resume.assert_awaited_once()
        assert getattr(entry, "runtime_data", None) is None
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, "charge", {"duration": 60}, blocking=True)


async def test_services_select_entry_and_propagate_rejection(hass):
    await async_setup(hass, {})
    entries = [make_entry(state=ConfigEntryState.LOADED), make_entry(state=ConfigEntryState.LOADED)]
    for entry in entries:
        entry.runtime_data = AsyncMock()
        hass.config_entries._entries[entry.entry_id] = entry
    with pytest.raises(ServiceValidationError, match="Select"):
        await hass.services.async_call(DOMAIN, "charge", {"duration": 60}, blocking=True)
    entries[0].runtime_data.controller.async_start.side_effect = PowerwallError("Rejected")
    with pytest.raises(HomeAssistantError, match="Rejected"):
        await hass.services.async_call(
            DOMAIN,
            "discharge",
            {
                "config_entry_id": entries[0].entry_id,
                "duration": {"minutes": 2},
                "power_w": 1200,
            },
            blocking=True,
        )
    entries[0].runtime_data.controller.async_start.assert_awaited_once_with("discharge", 120, 1200)
    entries[1].runtime_data.controller.async_start.assert_not_awaited()


@pytest.mark.parametrize(
    "data",
    [
        {"duration": 0},
        {"duration": 86401},
        {"duration": 60, "power_w": 0},
        {"duration": 60, "power_w": float("nan")},
        {"duration": 60, "power_w": True},
    ],
)
def test_service_schema_rejects_bad_requests(data):
    with pytest.raises(vol.Invalid):
        DISPATCH_SCHEMA(data)
