"""Grid contactor readback in the device's Sensors section."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import TelemetryEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([GridConnected(entry)])


class GridConnected(TelemetryEntity, BinarySensorEntity):
    _attr_name = "Grid connected"
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, entry) -> None:
        super().__init__(entry, "grid_connected")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        return data.grid_connected if data is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.is_on is not None
