"""Command status and measured readings below the device's manual controls."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfEnergy, UnitOfPower
from homeassistant.helpers.entity import EntityCategory

from .entity import PowerwallEntity, TelemetryEntity


@dataclass(frozen=True, kw_only=True)
class TelemetryDescription(SensorEntityDescription):
    basis: str | None = None


SENSORS = (
    TelemetryDescription(
        key="battery_level",
        name="Battery level",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        basis="Remaining energy / reported full-pack energy; before reserve adjustment",
    ),
    TelemetryDescription(
        key="battery_energy",
        name="Battery energy remaining",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
    ),
    TelemetryDescription(
        key="battery_capacity",
        name="Battery reported capacity",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TelemetryDescription(
        key="battery_power",
        name="Battery power",
        basis="Positive discharges; negative charges",
    ),
    TelemetryDescription(key="solar_power", name="Solar power"),
    TelemetryDescription(key="home_power", name="Home consumption"),
    TelemetryDescription(
        key="grid_power", name="Grid power", basis="Positive imports; negative exports"
    ),
    TelemetryDescription(key="grid_import", name="Grid import power"),
    TelemetryDescription(key="grid_export", name="Grid export power"),
)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([DispatchStatus(entry), *[TelemetrySensor(entry, desc) for desc in SENSORS]])


class TelemetrySensor(TelemetryEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, entry, description: TelemetryDescription) -> None:
        super().__init__(entry, description.key)
        self.entity_description = description
        if description.native_unit_of_measurement is None:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_suggested_display_precision = 0

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        return getattr(data, self.entity_description.key) if data is not None else None

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def extra_state_attributes(self) -> dict | None:
        basis = self.entity_description.basis
        return {"measurement_basis": basis} if basis else None


class DispatchStatus(PowerwallEntity, SensorEntity):
    _attr_name = "Dispatch status"
    _attr_icon = "mdi:battery-clock"

    def __init__(self, entry) -> None:
        super().__init__(entry, "dispatch_status")

    @property
    def native_value(self) -> str:
        return self._controller.mode

    @property
    def extra_state_attributes(self) -> dict:
        controller = self._controller
        return {
            "requested_power_w": controller.power_w,
            "dispatch_power_w": controller.dispatch_power_w,
            "ends_at": controller.ends_at.isoformat() if controller.ends_at else None,
            "last_error": controller.last_error,
            "restore_status": controller.restore_status,
            "restore_error": controller.restore_error,
            "config_entry_id": self._entry.entry_id,
            "max_charge_power_w": controller.max_charge,
            "max_discharge_power_w": controller.max_discharge,
        }
