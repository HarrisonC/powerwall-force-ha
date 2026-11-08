"""Shared device identity and event-driven updates for Powerwall entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import update_signal
from .const import DOMAIN, NAME


class PowerwallEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry, key: str) -> None:
        self._entry = entry
        self._controller = entry.runtime_data.controller
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=NAME,
            manufacturer="Tesla",
            model="Powerwall 3",
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, update_signal(self._entry.entry_id), self.async_write_ha_state
            )
        )


class TelemetryEntity(CoordinatorEntity, PowerwallEntity):
    def __init__(self, entry, key: str) -> None:
        CoordinatorEntity.__init__(self, entry.runtime_data.telemetry)
        PowerwallEntity.__init__(self, entry, key)

    @property
    def available(self) -> bool:
        return self.coordinator.available
