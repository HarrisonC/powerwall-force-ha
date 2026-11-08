"""Real RSA keys and Home Assistant fixtures; no connection to real hardware."""

import asyncio
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.resolver import ThreadedResolver
from cryptography.hazmat.primitives.asymmetric import rsa


@pytest.fixture
def telemetry_payload():
    return {
        "control": {
            "meterAggregates": [
                {"location": "site", "realPowerW": 100},
                {"location": "solar", "realPowerW": 2500},
                {"location": "battery", "realPowerW": -1000},
                {"location": "load", "realPowerW": 1600},
            ],
            "systemStatus": {
                "nominalFullPackEnergyWh": 13500,
                "nominalEnergyRemainingWh": 6750,
            },
            "islanding": {"contactorClosed": True, "gridOK": True, "microGridOK": True},
        }
    }


@pytest.fixture(scope="session")
def upstream_queries(tmp_path_factory):
    output = tmp_path_factory.mktemp("query_proto")
    source = Path(__file__).parent / "fixtures"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{source}",
            f"--python_out={output}",
            str(source / "upstream_queries.proto"),
        ],
        check=True,
        capture_output=True,
    )
    spec = importlib.util.spec_from_file_location(
        "upstream_queries_pb2", output / "upstream_queries_pb2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=4096)


@pytest.fixture
async def hass(tmp_path):
    from homeassistant import auth, loader
    from homeassistant.config_entries import ConfigEntries
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import area_registry, device_registry, entity_registry

    instance = HomeAssistant(str(tmp_path))
    (tmp_path / "custom_components").symlink_to(
        Path(__file__).resolve().parents[1] / "custom_components", target_is_directory=True
    )
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    await asyncio.gather(
        area_registry.async_load(instance),
        device_registry.async_load(instance),
        entity_registry.async_load(instance),
    )
    instance.auth = await auth.auth_manager_from_config(instance, [{"type": "homeassistant"}], [])
    # Avoid starting multicast discovery on the test host.
    with patch(
        "homeassistant.helpers.aiohttp_client._async_make_resolver",
        side_effect=lambda hass: ThreadedResolver(),
    ):
        yield instance
        await instance.async_stop(force=True)


@pytest.fixture
async def loaded(hass, rsa_key, telemetry_payload):
    from homeassistant.config_entries import ConfigEntry, ConfigEntryState
    from homeassistant.setup import async_setup_component

    from custom_components.powerwall_force.const import DOMAIN
    from custom_components.powerwall_force.telemetry import parse_telemetry

    entry = ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Powerwall Force",
        data={
            "host": "192.168.1.50",
            "gateway_din": "DIN",
            "private_key_path": "/config/key.pem",
            "max_charge_power_w": 5000,
            "max_discharge_power_w": 11000,
        },
        options={},
        source="user",
        unique_id="DIN",
        discovery_keys={},
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    with (
        patch("custom_components.powerwall_force.load_private_key", return_value=rsa_key),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_connect",
            AsyncMock(return_value="DIN"),
        ),
        patch(
            "custom_components.powerwall_force.client.LocalPowerwall.async_read_telemetry",
            AsyncMock(side_effect=lambda: parse_telemetry(telemetry_payload)),
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
        yield entry, dispatch, resume
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
