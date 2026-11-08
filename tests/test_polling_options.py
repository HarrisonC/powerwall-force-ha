"""Persist and apply polling preferences without restarting an active dispatch."""

from dataclasses import replace
from datetime import timedelta

import pytest
import voluptuous as vol

from custom_components.powerwall_force.const import (
    CONF_MANUAL_CHARGE_POWER,
    CONF_MANUAL_DISCHARGE_POWER,
    CONF_MANUAL_DURATION,
    CONF_POLL_INTERVAL,
    DOMAIN,
)
from custom_components.powerwall_force.polling import (
    poll_interval_seconds,
    validate_poll_interval,
)


@pytest.mark.parametrize("value", [0, -1, 301, 1.5, True, None, "x", float("nan"), float("inf")])
def test_invalid_interval_rejected_and_stored_corruption_defaults_to_two(value):
    with pytest.raises(vol.Invalid):
        validate_poll_interval(value)
    assert poll_interval_seconds({CONF_POLL_INTERVAL: value}) == 2


@pytest.mark.parametrize("value,expected", [(1, 1), (2.0, 2), (300, 300), ("15", 15)])
def test_whole_second_intervals(value, expected):
    assert validate_poll_interval(value) == expected


async def test_configure_saves_and_applies_interval_without_stopping_charge(hass, loaded):
    entry, dispatch, resume = loaded
    runtime = entry.runtime_data
    assert runtime.telemetry.update_interval == timedelta(seconds=2)
    preferences = {
        CONF_MANUAL_DURATION: 15,
        CONF_MANUAL_CHARGE_POWER: 2500,
        CONF_MANUAL_DISCHARGE_POWER: 9000,
    }
    hass.config_entries.async_update_entry(entry, options=preferences)
    await hass.async_block_till_done()
    read = runtime.telemetry.client.async_read_telemetry
    read.assert_awaited_once()  # Saving manual preferences does not poll or reload.

    await hass.services.async_call(DOMAIN, "charge", {"duration": 60}, blocking=True)
    dispatch.assert_awaited_once_with(-5000, 30)
    lease = runtime.controller._task
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    assert result["step_id"] == "init"
    assert result["data_schema"]({})[CONF_POLL_INTERVAL] == 2
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 15}
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options == {**preferences, CONF_POLL_INTERVAL: 15}
    assert entry.runtime_data is runtime
    assert runtime.telemetry.update_interval == timedelta(seconds=15)
    assert runtime.controller._task is lease
    assert not lease.done()
    assert runtime.controller.mode == "charge"
    assert read.await_count == 2
    dispatch.assert_awaited_once()
    resume.assert_not_awaited()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["data_schema"]({})[CONF_POLL_INTERVAL] == 15
    hass.config_entries.options.async_abort(result["flow_id"])
    # Later edits to a manual setting preserve the polling option too.
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.powerwall_force_manual_duration", "value": 20},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_POLL_INTERVAL] == 15
    assert read.await_count == 2
    assert entry.runtime_data is runtime

    await hass.services.async_call(DOMAIN, "stop", {}, blocking=True)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.telemetry.update_interval == timedelta(seconds=15)
    assert entry.options[CONF_MANUAL_DURATION] == 20


async def test_slow_interval_keeps_sample_available_until_three_intervals(hass, loaded):
    entry, _, _ = loaded
    coordinator = entry.runtime_data.telemetry
    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_POLL_INTERVAL: 60})
    await hass.async_block_till_done()
    coordinator.async_set_updated_data(
        replace(coordinator.data, observed_at=coordinator.data.observed_at - 61)
    )
    assert coordinator.available
    assert hass.states.get("sensor.powerwall_force_battery_level").state != "unavailable"
    coordinator.async_set_updated_data(
        replace(coordinator.data, observed_at=coordinator.data.observed_at - 121)
    )
    assert not coordinator.available


async def test_fractional_interval_form_error_does_not_save_options(hass, loaded):
    entry, _, _ = loaded
    previous = dict(entry.options)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 1.5}
    )
    assert result["type"] == "form"
    assert result["errors"] == {CONF_POLL_INTERVAL: "invalid_poll_interval"}
    assert entry.options == previous
    assert entry.runtime_data.telemetry.update_interval == timedelta(seconds=2)
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_options_change_without_listeners_does_not_start_polling(hass, loaded):
    entry, _, _ = loaded
    coordinator = entry.runtime_data.telemetry
    # Unload removes all listeners; this also exercises shutdown's no-read path.
    assert await hass.config_entries.async_unload(entry.entry_id)
    read = coordinator.client.async_read_telemetry
    count = read.await_count
    await coordinator.async_apply_options({CONF_POLL_INTERVAL: 30})
    assert coordinator.update_interval == timedelta(seconds=30)
    assert read.await_count == count
