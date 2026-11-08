"""One shared, low-priority LAN read for all live telemetry entities."""

import logging
from datetime import timedelta
from time import monotonic

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import LocalPowerwall
from .const import NAME, TELEMETRY_MAX_AGE
from .polling import poll_interval_seconds
from .protocol import PowerwallError, TelemetryBusy
from .telemetry import Telemetry

_LOGGER = logging.getLogger(__name__)


class TelemetryCoordinator(DataUpdateCoordinator[Telemetry | None]):
    def __init__(self, hass, entry, client: LocalPowerwall) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{NAME} local telemetry",
            update_interval=timedelta(
                seconds=poll_interval_seconds(entry.options if entry else {})
            ),
        )
        self.client = client

    @property
    def available(self) -> bool:
        return (
            self.last_update_success
            and self.data is not None
            and monotonic() - self.data.observed_at
            <= max(TELEMETRY_MAX_AGE, self.update_interval.total_seconds() * 3)
        )

    async def async_apply_options(self, options) -> None:
        interval = timedelta(seconds=poll_interval_seconds(options))
        if interval == self.update_interval:
            return
        self.update_interval = interval
        # Refresh/reschedule through HA's debouncer without reloading the entry.
        # Manual number changes keep the same interval and skip this path.
        if self._listeners:
            await self.async_request_refresh()

    async def _async_update_data(self) -> Telemetry | None:
        try:
            return await self.client.async_read_telemetry()
        except TelemetryBusy:
            # Keep the last read through a command preemption, with an age limit.
            return self.data
        except PowerwallError as err:
            raise UpdateFailed(str(err)) from err

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.client.async_cancel_telemetry()
