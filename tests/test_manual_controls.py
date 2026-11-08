"""Operate native button/number entities through real Home Assistant actions."""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry

from custom_components.powerwall_force.const import (
    CONF_MANUAL_CHARGE_POWER,
    CONF_MANUAL_DISCHARGE_POWER,
    CONF_MANUAL_DURATION,
    DOMAIN,
)
from custom_components.powerwall_force.protocol import CommandRejected

STATUS = "sensor.powerwall_force_dispatch_status"
CHARGE = "button.powerwall_force_force_charge"
DISCHARGE = "button.powerwall_force_force_discharge"
STOP = "button.powerwall_force_stop"
DURATION = "number.powerwall_force_manual_duration"
CHARGE_POWER = "number.powerwall_force_manual_charge_power"
DISCHARGE_POWER = "number.powerwall_force_manual_discharge_power"


async def set_number(hass, entity_id, value):
    await hass.services.async_call(
        "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
    )


async def press(hass, entity_id):
    await hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)


async def test_controls_share_device_and_start_stop_timed_sessions(hass, loaded):
    entry, dispatch, resume = loaded
    entities = (STATUS, CHARGE, DISCHARGE, STOP, DURATION, CHARGE_POWER, DISCHARGE_POWER)
    assert all(hass.states.get(entity) is not None for entity in entities)
    registry = entity_registry.async_get(hass)
    devices = {registry.async_get(entity).device_id for entity in entities}
    assert len(devices) == 1
    assert None not in devices
    assert float(hass.states.get(DURATION).state) == 30
    assert float(hass.states.get(DISCHARGE_POWER).state) == 11000
    dispatch.assert_not_awaited()
    resume.assert_not_awaited()

    await set_number(hass, DURATION, 2)
    await set_number(hass, CHARGE_POWER, 2300)
    await set_number(hass, DISCHARGE_POWER, 9000)
    assert entry.options[CONF_MANUAL_DURATION] == 2
    assert entry.options[CONF_MANUAL_CHARGE_POWER] == 2300
    assert entry.options[CONF_MANUAL_DISCHARGE_POWER] == 9000
    dispatch.assert_not_awaited()  # Editing preferences never starts a dispatch.

    with patch.object(
        entry.runtime_data.controller,
        "async_start",
        wraps=entry.runtime_data.controller.async_start,
    ) as start:
        await press(hass, CHARGE)
        start.assert_awaited_once_with("charge", 120, 2300)
    dispatch.assert_awaited_once_with(-2300, 30)
    assert hass.states.get(STATUS).state == "charge"

    await press(hass, DISCHARGE)
    dispatch.assert_awaited_with(9000, 30)
    assert hass.states.get(STATUS).state == "discharge"
    await press(hass, STOP)
    resume.assert_awaited_once()
    assert hass.states.get(STATUS).state == "idle"


async def test_manual_values_survive_reload_and_respect_reconfigured_maximum(hass, loaded):
    entry, dispatch, _ = loaded
    await set_number(hass, DURATION, 15)
    await set_number(hass, DISCHARGE_POWER, 9000)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "max_discharge_power_w": 8000}
    )
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert float(hass.states.get(DURATION).state) == 15
    assert float(hass.states.get(DISCHARGE_POWER).state) == 8000
    assert hass.states.get(DISCHARGE_POWER).attributes["max"] == 8000
    await press(hass, DISCHARGE)
    dispatch.assert_awaited_once_with(8000, 30)


async def test_manual_values_do_not_change_automation_defaults(hass, loaded):
    _, dispatch, _ = loaded
    await set_number(hass, DISCHARGE_POWER, 9000)
    await hass.services.async_call(DOMAIN, "discharge", {"duration": 60}, blocking=True)
    dispatch.assert_awaited_once_with(11000, 30)


async def test_button_rejection_updates_status_and_returns_error(hass, loaded):
    _, dispatch, _ = loaded
    dispatch.side_effect = CommandRejected(11, "Power outside allowed range")
    with pytest.raises(HomeAssistantError, match="11000 W"):
        await press(hass, DISCHARGE)
    assert hass.states.get(STATUS).state == "error"
    assert "Power outside allowed range" in hass.states.get(STATUS).attributes["last_error"]
    assert hass.states.get(STATUS).attributes["restore_status"] == "confirmed"


@pytest.mark.parametrize(
    "entity_id,value", [(DURATION, 0), (DISCHARGE_POWER, 12000), (CHARGE_POWER, float("nan"))]
)
async def test_invalid_manual_setting_cannot_trigger_hardware(hass, loaded, entity_id, value):
    entry, dispatch, resume = loaded
    with pytest.raises(HomeAssistantError):
        await set_number(hass, entity_id, value)
    assert not entry.options
    dispatch.assert_not_awaited()
    resume.assert_not_awaited()
