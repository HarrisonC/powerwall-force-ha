"""Home Assistant actions for timed, local Powerwall battery dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .client import LocalPowerwall, load_private_key
from .const import (
    CONF_GATEWAY_DIN,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_PRIVATE_KEY_PATH,
    DOMAIN,
    MAX_DURATION_SECONDS,
)
from .controller import DispatchController
from .coordinator import TelemetryCoordinator
from .protocol import AuthenticationError, PowerwallError

PLATFORMS = [Platform.NUMBER, Platform.BUTTON, Platform.SENSOR, Platform.BINARY_SENSOR]


@dataclass
class PowerwallRuntime:
    controller: DispatchController
    telemetry: TelemetryCoordinator


def _duration(value) -> timedelta:
    duration = cv.positive_time_period(value)
    if not 1 <= duration.total_seconds() <= MAX_DURATION_SECONDS:
        raise vol.Invalid("Duration must be between 1 second and 24 hours")
    return duration


def _power(value) -> float:
    import math

    if isinstance(value, bool):
        raise vol.Invalid("Power must be a number of watts")
    power = vol.Coerce(float)(value)
    if not math.isfinite(power) or power <= 0:
        raise vol.Invalid("Power must be a positive, finite number of watts")
    return power


BASE_SCHEMA = {vol.Optional("config_entry_id"): cv.string}
DISPATCH_SCHEMA = vol.Schema(
    {
        **BASE_SCHEMA,
        vol.Required("duration"): _duration,
        vol.Optional("power_w"): _power,
    }
)
STOP_SCHEMA = vol.Schema(BASE_SCHEMA)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def update_signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_updated"


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def handle_action(call: ServiceCall) -> None:
        entries = hass.config_entries.async_entries(DOMAIN)
        entry_id = call.data.get("config_entry_id")
        if entry_id is None:
            if not entries:
                raise ServiceValidationError("No Powerwall Force integration is configured")
            if len(entries) != 1:
                raise ServiceValidationError(
                    "Select a config_entry_id when multiple Powerwalls exist"
                )
            entry = entries[0]
        else:
            entry = next((item for item in entries if item.entry_id == entry_id), None)
        if (
            entry is None
            or entry.state is not ConfigEntryState.LOADED
            or getattr(entry, "runtime_data", None) is None
        ):
            raise ServiceValidationError("The selected Powerwall integration is not loaded")
        controller = entry.runtime_data.controller
        try:
            if call.service == "stop":
                await controller.async_stop()
            else:
                await controller.async_start(
                    call.service, call.data["duration"].total_seconds(), call.data.get("power_w")
                )
        except (PowerwallError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

    for action in ("charge", "discharge"):
        hass.services.async_register(DOMAIN, action, handle_action, schema=DISPATCH_SCHEMA)
    hass.services.async_register(DOMAIN, "stop", handle_action, schema=STOP_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    try:
        key = await hass.async_add_executor_job(load_private_key, entry.data[CONF_PRIVATE_KEY_PATH])
    except (OSError, ValueError, TypeError) as err:
        raise ConfigEntryAuthFailed("Cannot load the registered RSA-4096 private key") from err
    client = LocalPowerwall(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data.get(CONF_PASSWORD),
        key,
        din=entry.data.get(CONF_GATEWAY_DIN),
    )
    try:
        din = await client.async_connect()
    except AuthenticationError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except PowerwallError as err:
        raise ConfigEntryNotReady(str(err)) from err
    if entry.unique_id != din:
        raise ConfigEntryAuthFailed(
            "Gateway identity changed; reconfigure with the original gateway"
        )
    controller = DispatchController(
        client,
        entry.data[CONF_MAX_CHARGE_POWER],
        entry.data[CONF_MAX_DISCHARGE_POWER],
        hass.async_create_background_task,
        lambda: async_dispatcher_send(hass, update_signal(entry.entry_id)),
    )
    telemetry = TelemetryCoordinator(hass, entry, client)
    entry.runtime_data = PowerwallRuntime(controller, telemetry)

    async def shutdown(event) -> None:
        await telemetry.async_shutdown()
        await controller.async_stop(close=True)

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, shutdown))
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    # A rejected/unsupported telemetry query must not prevent manual control.
    await telemetry.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if (runtime := getattr(entry, "runtime_data", None)) is not None:
        await runtime.telemetry.async_apply_options(entry.options)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime: PowerwallRuntime = entry.runtime_data
        await runtime.telemetry.async_shutdown()
        await runtime.controller.async_stop(close=True)
        entry.runtime_data = None
    return unloaded
