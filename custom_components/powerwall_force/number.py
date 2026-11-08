"""Editable duration and power for the device page's manual buttons."""

import math

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfPower, UnitOfTime
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import update_signal
from .const import CONF_MANUAL_CHARGE_POWER, CONF_MANUAL_DISCHARGE_POWER, CONF_MANUAL_DURATION
from .entity import PowerwallEntity
from .manual import manual_bounds, manual_value

SETTINGS = (
    (CONF_MANUAL_DURATION, "Manual duration", UnitOfTime.MINUTES, "mdi:timer-outline"),
    (CONF_MANUAL_CHARGE_POWER, "Manual charge power", UnitOfPower.WATT, "mdi:battery-arrow-up"),
    (
        CONF_MANUAL_DISCHARGE_POWER,
        "Manual discharge power",
        UnitOfPower.WATT,
        "mdi:battery-arrow-down",
    ),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ManualSetting(entry, *setting) for setting in SETTINGS])


class ManualSetting(PowerwallEntity, NumberEntity):
    _attr_entity_category = None
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, entry, key: str, name: str, unit: str, icon: str) -> None:
        super().__init__(entry, key)
        self._key = key
        self._attr_name = name
        self._attr_native_unit_of_measurement = unit
        self._attr_icon = icon
        self._attr_native_min_value, self._attr_native_max_value = manual_bounds(entry, key)

    @property
    def native_value(self) -> float:
        return manual_value(self._entry, self._key)

    async def async_set_native_value(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not math.isfinite(value)
            or not (self.native_min_value <= value <= self.native_max_value)
        ):
            raise ServiceValidationError(
                f"{self.name} must be between {self.native_min_value:g} and "
                f"{self.native_max_value:g} {self.native_unit_of_measurement}"
            )
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, self._key: value}
        )
        async_dispatcher_send(self.hass, update_signal(self._entry.entry_id))
