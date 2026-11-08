"""Manual button settings, persisted in the config entry's options."""

import math

from .const import (
    CONF_MANUAL_CHARGE_POWER,
    CONF_MANUAL_DISCHARGE_POWER,
    CONF_MANUAL_DURATION,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    DEFAULT_MANUAL_DURATION,
    MAX_DURATION_SECONDS,
)


def manual_bounds(entry, key: str) -> tuple[float, float]:
    if key == CONF_MANUAL_DURATION:
        return 1, MAX_DURATION_SECONDS / 60
    limit_key = (
        CONF_MAX_CHARGE_POWER if key == CONF_MANUAL_CHARGE_POWER else CONF_MAX_DISCHARGE_POWER
    )
    maximum = entry.data[limit_key]
    return min(1, maximum), maximum


def manual_value(entry, key: str) -> float:
    minimum, maximum = manual_bounds(entry, key)
    default = DEFAULT_MANUAL_DURATION if key == CONF_MANUAL_DURATION else maximum
    raw = entry.options.get(key, default)
    try:
        value = float(raw)
    except (ValueError, TypeError, OverflowError):
        value = default
    if isinstance(raw, bool) or not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


MANUAL_POWER_KEYS = {"charge": CONF_MANUAL_CHARGE_POWER, "discharge": CONF_MANUAL_DISCHARGE_POWER}
