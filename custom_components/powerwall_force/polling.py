"""Shared validation and defaults for the saved telemetry polling interval."""

import math
from collections.abc import Mapping

import voluptuous as vol

from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)


def validate_poll_interval(value) -> int:
    try:
        if isinstance(value, bool):
            raise ValueError
        seconds = float(value)
        if (
            not math.isfinite(seconds)
            or not seconds.is_integer()
            or not MIN_POLL_INTERVAL <= seconds <= MAX_POLL_INTERVAL
        ):
            raise ValueError
    except (TypeError, ValueError, OverflowError) as err:
        raise vol.Invalid(
            f"Polling interval must be a whole number from {MIN_POLL_INTERVAL} "
            f"to {MAX_POLL_INTERVAL} seconds"
        ) from err
    return int(seconds)


def poll_interval_seconds(options: Mapping) -> int:
    try:
        return validate_poll_interval(options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))
    except vol.Invalid:
        return DEFAULT_POLL_INTERVAL
