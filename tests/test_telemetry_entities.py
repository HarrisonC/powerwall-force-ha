"""Native HA readings, control grouping, independent failure and polling cleanup."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import entity_registry

from custom_components.powerwall_force.const import DOMAIN
from custom_components.powerwall_force.coordinator import TelemetryCoordinator
from custom_components.powerwall_force.protocol import PowerwallError, TelemetryBusy
from custom_components.powerwall_force.telemetry import parse_telemetry

CONTROLS = (
    "button.powerwall_force_force_charge",
    "button.powerwall_force_force_discharge",
    "button.powerwall_force_stop",
    "number.powerwall_force_manual_duration",
    "number.powerwall_force_manual_charge_power",
    "number.powerwall_force_manual_discharge_power",
)


async def test_shared_device_native_sensor_metadata_and_controls_first(hass, loaded):
    entry, dispatch, resume = loaded
    states = {
        "battery_level": (50, "%", "battery"),
        "battery_energy_remaining": (6.75, "kWh", "energy_storage"),
        "battery_reported_capacity": (13.5, "kWh", "energy_storage"),
        "battery_power": (-1000, "W", "power"),
        "solar_power": (2500, "W", "power"),
        "home_consumption": (1600, "W", "power"),
        "grid_power": (100, "W", "power"),
        "grid_import_power": (100, "W", "power"),
        "grid_export_power": (0, "W", "power"),
    }
    registry = entity_registry.async_get(hass)
    device_id = registry.async_get(CONTROLS[0]).device_id
    for name, (value, unit, device_class) in states.items():
        entity_id = f"sensor.powerwall_force_{name}"
        state = hass.states.get(entity_id)
        assert state is not None
        assert float(state.state) == value
        assert state.attributes["unit_of_measurement"] == unit
        assert state.attributes["device_class"] == device_class
        assert state.attributes["state_class"] == "measurement"
        registered = registry.async_get(entity_id)
        assert registered.device_id == device_id
        assert not registered.disabled
        assert registered.entity_category == ("diagnostic" if "capacity" in name else None)
    grid = "binary_sensor.powerwall_force_grid_connected"
    assert hass.states.get(grid).state == "on"
    assert registry.async_get(grid).device_id == device_id
    assert hass.states.get("sensor.powerwall_force_dispatch_status").state == "idle"

    # Frontend categorizes button/number without entity_category as Controls,
    # rendered before sensor/binary_sensor in Sensors, and then Diagnostics.
    for entity_id in CONTROLS:
        assert hass.states.get(entity_id) is not None
        assert registry.async_get(entity_id).entity_category is None
        assert registry.async_get(entity_id).device_id == device_id
    entry.runtime_data.telemetry.client.async_read_telemetry.assert_awaited_once()
    dispatch.assert_not_awaited()
    resume.assert_not_awaited()


async def test_partial_read_failure_recovery_and_command_independence(
    hass, loaded, telemetry_payload
):
    entry, dispatch, _ = loaded
    coordinator = entry.runtime_data.telemetry
    read = coordinator.client.async_read_telemetry
    read.side_effect = PowerwallError("Read rejected")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.powerwall_force_battery_level").state == "unavailable"
    assert hass.states.get("binary_sensor.powerwall_force_grid_connected").state == "unavailable"
    assert hass.states.get(CONTROLS[0]).state != "unavailable"
    assert hass.states.get("sensor.powerwall_force_dispatch_status").state == "idle"

    await hass.services.async_call(DOMAIN, "charge", {"duration": 60}, blocking=True)
    dispatch.assert_awaited_once_with(-5000, 30)
    assert hass.states.get("sensor.powerwall_force_dispatch_status").state == "charge"
    read.side_effect = None
    telemetry_payload["control"]["meterAggregates"] = [
        {"location": "site", "realPowerW": -800},
        {"location": "battery", "realPowerW": 0},
    ]
    telemetry_payload["control"]["islanding"]["contactorClosed"] = False
    read.return_value = parse_telemetry(telemetry_payload)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("sensor.powerwall_force_grid_import_power").state == "0.0"
    assert hass.states.get("sensor.powerwall_force_grid_export_power").state == "800.0"
    assert hass.states.get("sensor.powerwall_force_battery_power").state == "0.0"
    assert hass.states.get("sensor.powerwall_force_solar_power").state == "unavailable"
    assert hass.states.get("binary_sensor.powerwall_force_grid_connected").state == "off"
    # The measured 0 W does not change the accepted -5000 W command.
    assert (
        hass.states.get("sensor.powerwall_force_dispatch_status").attributes["requested_power_w"]
        == -5000
    )


async def test_preempted_samples_expire_without_affecting_controls(hass, loaded):
    entry, _, _ = loaded
    coordinator = entry.runtime_data.telemetry
    coordinator.client.async_read_telemetry.side_effect = TelemetryBusy("Command in progress")
    previous = coordinator.data
    await coordinator.async_refresh()
    assert coordinator.data is previous
    assert coordinator.available
    coordinator.async_set_updated_data(replace(previous, observed_at=previous.observed_at - 31))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.available
    assert hass.states.get("sensor.powerwall_force_grid_power").state == "unavailable"
    assert hass.states.get(CONTROLS[0]).state != "unavailable"


async def test_polls_share_one_read_and_stop_without_listeners(hass, telemetry_payload):
    client = AsyncMock()
    client.async_read_telemetry.side_effect = lambda: parse_telemetry(telemetry_payload)
    coordinator = TelemetryCoordinator(hass, None, client)
    remove = coordinator.async_add_listener(lambda: None)
    try:
        assert coordinator.update_interval == timedelta(seconds=2)
        assert coordinator._unsub_refresh is not None
        # Exercise the scheduled callback deterministically, without a wall-clock wait.
        coordinator._async_unsub_refresh()
        await coordinator._handle_refresh_interval()
        assert client.async_read_telemetry.await_count == 1
        assert coordinator._unsub_refresh is not None
        remove()
        await hass.async_block_till_done()
        assert coordinator._unsub_refresh is None
    finally:
        await coordinator.async_shutdown()


async def test_unload_stops_polling_and_releases_active_dispatch(hass, loaded):
    entry, _, resume = loaded
    coordinator = entry.runtime_data.telemetry
    await hass.services.async_call(DOMAIN, "discharge", {"duration": 60}, blocking=True)
    read = coordinator.client.async_read_telemetry
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    count = read.await_count
    await coordinator.async_refresh()
    assert read.await_count == count
    resume.assert_awaited_once()
    assert not coordinator._listeners
    assert coordinator._unsub_refresh is None
    assert getattr(entry, "runtime_data", None) is None


async def test_initial_telemetry_failure_still_loads_controls(hass, loaded):
    entry, _, _ = loaded
    with patch(
        "custom_components.powerwall_force.client.LocalPowerwall.async_read_telemetry",
        AsyncMock(side_effect=PowerwallError("Query unsupported")),
    ):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
        assert hass.states.get(CONTROLS[0]).state != "unavailable"
        assert hass.states.get("sensor.powerwall_force_battery_level").state == "unavailable"
