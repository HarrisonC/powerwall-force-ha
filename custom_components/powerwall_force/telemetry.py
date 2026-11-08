"""Normalize core readings from one local status query, without guessing gaps."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from time import monotonic

from .protocol import PowerwallError


@dataclass(frozen=True)
class Telemetry:
    battery_level: float | None
    battery_energy: float | None
    battery_capacity: float | None
    battery_power: float | None
    solar_power: float | None
    home_power: float | None
    grid_power: float | None
    grid_connected: bool | None
    observed_at: float = field(default_factory=monotonic)

    @property
    def grid_import(self) -> float | None:
        return max(0.0, self.grid_power) if self.grid_power is not None else None

    @property
    def grid_export(self) -> float | None:
        return max(0.0, -self.grid_power) if self.grid_power is not None else None


def _number(value) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        value = float(value)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def parse_telemetry(data: dict) -> Telemetry:
    control = data.get("control")
    if not isinstance(control, dict):
        raise PowerwallError("Gateway returned no live control telemetry")

    powers: dict[str, float | None] = {}
    meters = control.get("meterAggregates")
    for meter in meters if isinstance(meters, list) else []:
        if not isinstance(meter, dict) or not isinstance(meter.get("location"), str):
            continue
        location = meter["location"].strip().lower()
        # An ambiguous duplicate is missing data, rather than last-value-wins.
        powers[location] = None if location in powers else _number(meter.get("realPowerW"))

    status = _object(control.get("systemStatus"))
    remaining = _number(status.get("nominalEnergyRemainingWh"))
    capacity = _number(status.get("nominalFullPackEnergyWh"))
    if remaining is not None and remaining < 0:
        remaining = None
    if capacity is not None and capacity <= 0:
        capacity = None
    level = None
    if remaining is not None and capacity is not None and remaining <= capacity:
        level = remaining / capacity * 100

    islanding = _object(control.get("islanding"))
    closed, grid_ok = islanding.get("contactorClosed"), islanding.get("gridOK")
    connected = None
    if islanding.get("microGridOK") is True and type(closed) is bool and type(grid_ok) is bool:
        # A healthy utility alone does not mean the grid contactor is closed.
        if not closed:
            connected = False
        elif grid_ok:
            connected = True

    values = (
        level,
        remaining / 1000 if remaining is not None else None,
        capacity / 1000 if capacity is not None else None,
        powers.get("battery"),
        powers.get("solar"),
        powers.get("load"),
        powers.get("site"),
        connected,
    )
    if all(value is None for value in values):
        raise PowerwallError("Gateway returned no usable telemetry readings")
    return Telemetry(*values)
