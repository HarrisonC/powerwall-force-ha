"""Wizard state transitions, including proof at the local gateway."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.application_credentials import ClientCredential
from homeassistant.helpers import config_entry_oauth2_flow as oauth
from yarl import URL

from custom_components.powerwall_force.application_credentials import async_get_auth_implementation
from custom_components.powerwall_force.config_flow import PowerwallForceConfigFlow
from custom_components.powerwall_force.const import DOMAIN
from custom_components.powerwall_force.pairing import EnergySite
from custom_components.powerwall_force.protocol import AuthenticationError


@pytest.fixture
def flow(hass, monkeypatch):
    instance = PowerwallForceConfigFlow()
    instance.hass = hass
    instance.flow_id = "pairing-test"
    instance.handler = DOMAIN
    instance.context = {"source": "user"}
    monkeypatch.setattr(
        "custom_components.powerwall_force.pairing.FleetPairing._json",
        AsyncMock(side_effect=AssertionError("A wizard unit test must not make cloud requests")),
    )
    yield instance
    if instance._verification_task and not instance._verification_task.done():
        instance._verification_task.cancel()


async def test_guided_pairing_checks_local_key_and_discards_tokens(flow):
    with (
        patch(
            "custom_components.powerwall_force.config_flow.FleetPairing.async_sites",
            AsyncMock(
                return_value=[
                    EnergySite("1", "DIN-A", "Home"),
                    EnergySite("2", "DIN-B", "Garage"),
                ]
            ),
        ),
        patch(
            "custom_components.powerwall_force.config_flow.FleetPairing.async_register", AsyncMock()
        ) as register,
        patch(
            "custom_components.powerwall_force.config_flow.FleetPairing.async_key_state",
            AsyncMock(return_value=3),
        ),
        patch(
            "custom_components.powerwall_force.config_flow.LocalPowerwall.async_connect",
            AsyncMock(
                side_effect=[
                    AuthenticationError("Not verified locally"),
                    "DIN-B",
                ]
            ),
        ) as connect,
        patch(
            "custom_components.powerwall_force.config_flow.LocalPowerwall.async_dispatch",
            AsyncMock(),
        ) as dispatch,
        patch("custom_components.powerwall_force.config_flow.PAIRING_POLL", 0),
    ):
        result = await flow.async_step_pair_gateway(
            {
                "host": "192.168.1.50",
                "region": "Europe",
                "max_charge_power_w": 5000,
                "max_discharge_power_w": 10000,
            }
        )
        assert result["step_id"] == "authorization"
        result = await flow.async_oauth_create_entry(
            {"token": {"access_token": "temporary-account-token"}}
        )
        assert result["step_id"] == "select_site"
        result = await flow.async_step_select_site({"site_id": "2"})
        assert result["step_id"] == "register_key"
        register.assert_not_awaited()
        result = await flow.async_step_register_key({})
        assert result["step_id"] == "isolator"
        assert register.await_args.args[0].site_id == "2"
        result = await flow.async_step_isolator({})
        assert result["type"] == "progress"
        assert await flow._verification_task is None
        assert connect.await_count == 2  # Cloud VERIFIED alone did not finish setup.
        result = await flow.async_step_verify_pairing()
        assert result["step_id"] == "finish_pairing"
        result = await flow.async_step_finish_pairing()
        assert result["data"]["gateway_din"] == "DIN-B"
        assert "password" not in result["data"]
        assert "token" not in json.dumps(result["data"])
        assert "temporary-account-token" not in json.dumps(result)
        assert flow._fleet is None
        dispatch.assert_not_awaited()


async def test_tesla_login_uses_tesla_owned_endpoints(hass):
    hass.config.components.add("my")
    implementation = await async_get_auth_implementation(
        hass, DOMAIN, ClientCredential("my-client", "my-secret")
    )
    url = URL(await implementation.async_generate_authorize_url("test-flow"))
    assert url.host == "auth.tesla.com"
    assert url.query["client_id"] == "my-client"
    assert set(url.query["scope"].split()) == {"openid", "energy_device_data", "energy_cmds"}
    assert "my-secret" not in str(url)
    assert url.query["state"]
    assert implementation.token_url.startswith("https://fleet-auth.prd.vn.cloud.tesla.com/")
    assert implementation.extra_token_resolve_data["audience"].startswith("https://fleet-api.")


async def test_pairing_progress_is_cancelled_on_flow_abort(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    instance = hass.config_entries.flow._progress[result["flow_id"]]
    blocker = asyncio.Event()
    task = hass.async_create_task(blocker.wait(), "Pending pairing test")
    instance._verification_task = task
    instance._fleet = object()
    instance.async_set_progress_task(task)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await asyncio.sleep(0)
    assert task.cancelled()
    assert instance._fleet is None


async def test_expired_pairing_offers_retry_without_new_key(flow):
    flow._registered_at = 0
    assert await flow._wait_for_gateway() == "pairing_timeout"
    flow._pairing_error = "pairing_timeout"
    result = await flow.async_step_pairing_retry()
    assert result["errors"]["base"] == "pairing_timeout"


async def test_developer_credentials_and_oauth_callback_use_native_flow(hass):
    hass.config.components.add("my")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    flow_id = result["flow_id"]
    try:
        await hass.config_entries.flow.async_configure(flow_id, {"next_step_id": "pair_gateway"})
        await hass.config_entries.flow.async_configure(
            flow_id,
            {
                "host": "192.168.1.50",
                "region": "Europe",
                "max_charge_power_w": 5000,
                "max_discharge_power_w": 10000,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            flow_id, {"next_step_id": "tesla_login"}
        )
        assert result["step_id"] == "application_credentials"
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                "client_id": "my-application",
                "client_secret": "application-secret",
            },
        )
        assert result["step_id"] == "pick_implementation"
        implementations = await oauth.async_get_implementations(hass, DOMAIN)
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                "implementation": next(iter(implementations)),
            },
        )
        assert result["type"] == "external"
        url = URL(result["url"])
        assert url.host == "auth.tesla.com"
        assert url.query["client_id"] == "my-application"
        assert "application-secret" not in str(url)
        instance = hass.config_entries.flow._progress[flow_id]
        assert instance.flow_impl.extra_token_resolve_data["audience"].startswith(
            "https://fleet-api.prd.eu."
        )
        # The callback view verifies OAuth state before passing this data to the flow.
        await hass.config_entries.flow.async_configure(
            flow_id,
            {
                "code": "test-code",
                "state": {"redirect_uri": oauth.MY_AUTH_CALLBACK_PATH},
            },
        )
        with (
            patch.object(
                type(instance.flow_impl),
                "async_resolve_external_data",
                AsyncMock(
                    return_value={
                        "access_token": "pairing-only-token",
                        "expires_in": 3600,
                    }
                ),
            ),
            patch(
                "custom_components.powerwall_force.config_flow.FleetPairing.async_sites",
                AsyncMock(
                    return_value=[
                        EnergySite("1", "DIN", "Home"),
                    ]
                ),
            ),
        ):
            result = await hass.config_entries.flow.async_configure(flow_id)
            assert result["step_id"] == "select_site"
            assert instance.external_data is None
            assert not hass.config_entries.async_entries(DOMAIN)
    finally:
        hass.config_entries.flow.async_abort(flow_id)


async def test_existing_fleet_authorization_is_reused(flow, hass):
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain="tesla_fleet",
        title="Existing Tesla",
        data={},
        options={},
        source="user",
        unique_id="account",
        discovery_keys={},
        subentries_data=None,
        state=ConfigEntryState.LOADED,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    with (
        patch.object(
            oauth, "async_get_config_entry_implementation", AsyncMock(return_value=object())
        ),
        patch.object(oauth, "OAuth2Session") as session,
        patch(
            "custom_components.powerwall_force.config_flow.FleetPairing.async_sites",
            AsyncMock(
                return_value=[
                    EnergySite("1", "DIN", "Home"),
                ]
            ),
        ),
    ):
        session.return_value.token = {"access_token": "existing-token"}
        session.return_value.async_ensure_token_valid = AsyncMock()
        result = await flow.async_step_authorization()
        assert "tesla_fleet" in result["menu_options"]
        result = await flow.async_step_tesla_fleet({"entry_id": entry.entry_id})
        assert result["step_id"] == "select_site"
        session.return_value.async_ensure_token_valid.assert_awaited_once()
