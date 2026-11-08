"""Timed dispatch sessions backed by short gateway-side leases."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from .client import LocalPowerwall
from .const import LEASE_SECONDS, MAX_DURATION_SECONDS, RENEW_SECONDS, RENEW_TIMEOUT
from .protocol import CommandRejected, PowerwallError

_LOGGER = logging.getLogger(__name__)


class DispatchController:
    def __init__(
        self,
        client: LocalPowerwall,
        max_charge: float,
        max_discharge: float,
        create_task: Callable,
        notify: Callable[[], None],
    ) -> None:
        self.client = client
        self.max_charge = max_charge
        self.max_discharge = max_discharge
        self._create_task = create_task
        self._notify = notify
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._deadline = 0.0
        self._closed = False
        self.mode = "idle"
        self.power_w: float | None = None
        self.ends_at: datetime | None = None
        self.last_error: str | None = None
        self._uses_default_power = False

    @staticmethod
    def _dispatch_error(err: PowerwallError, mode: str, power: float, default: bool) -> str:
        if isinstance(err, CommandRejected) and err.status_code == 11:
            source = " (configured maximum; power_w was omitted)" if default else ""
            return (
                f"{err}. Requested {mode} power: {power:g} W{source}. "
                f"Choose a lower {mode} power. If power_w is omitted, reduce the maximum "
                f"{mode} power in Reconfigure to a value accepted by the gateway."
            )
        return str(err)

    def _cancel_task(self) -> None:
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
        self._task = None

    def _clear(self, error: str | None = None) -> None:
        self.mode = "error" if error else "idle"
        self.power_w = None
        self.ends_at = None
        self.last_error = error
        self._notify()

    async def _release(self) -> str | None:
        try:
            await self.client.async_resume()
        except PowerwallError as err:
            _LOGGER.warning("Cannot release dispatch; last lease will expire: %s", err)
            return str(err)
        return None

    async def async_start(self, mode: str, duration: float, power: float | None) -> None:
        if mode not in ("charge", "discharge"):
            raise ValueError("Mode must be charge or discharge")
        if not math.isfinite(duration) or not 1 <= duration <= MAX_DURATION_SECONDS:
            raise ValueError("Duration must be between 1 second and 24 hours")
        limit = self.max_charge if mode == "charge" else self.max_discharge
        uses_default = power is None
        power = limit if uses_default else power
        if not math.isfinite(power) or not 0 < power <= limit:
            raise ValueError(f"Power must be greater than 0 and at most {limit:g} W")
        watts = -power if mode == "charge" else power
        async with self._lock:
            if self._closed:
                raise PowerwallError("Integration is unloading")
            self._cancel_task()
            # Include network time in the requested period, never renew past it.
            sent_at = asyncio.get_running_loop().time()
            deadline = sent_at + duration
            lease = min(LEASE_SECONDS, math.ceil(duration))
            try:
                await self.client.async_dispatch(watts, lease)
            except PowerwallError as err:
                # A lost acknowledgement may still mean the gateway accepted it.
                await self._release()
                message = self._dispatch_error(err, mode, power, uses_default)
                self._clear(message)
                if message != str(err):
                    raise PowerwallError(message) from err
                raise
            self.mode, self.power_w = mode, watts
            self._uses_default_power = uses_default
            self._deadline = deadline
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            self.ends_at = datetime.now(UTC) + timedelta(seconds=remaining)
            self.last_error = None
            self._notify()
            self._task = self._create_task(self._run(sent_at, lease), "Powerwall dispatch lease")

    async def _run(self, sent_at: float, lease: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            while True:
                # The gateway can start its timer before we receive the acknowledgement.
                renew_at = min(sent_at + RENEW_SECONDS, self._deadline)
                await asyncio.sleep(max(0, renew_at - loop.time()))
                async with self._lock:
                    now = loop.time()
                    remaining = self._deadline - now
                    if remaining <= 0:
                        error = await self._release()
                        self._task = None
                        self._clear(error)
                        return
                    try:
                        time_budget = min(RENEW_TIMEOUT, remaining, sent_at + lease - now)
                        if time_budget <= 0:
                            raise PowerwallError("Previous dispatch lease may have expired")
                        lease = min(LEASE_SECONDS, math.ceil(remaining))
                        sent_at = now
                        await self.client.async_dispatch(
                            self.power_w, lease, time_budget=time_budget
                        )
                    except PowerwallError as err:
                        await self._release()
                        self._task = None
                        message = self._dispatch_error(
                            err, self.mode, abs(self.power_w), self._uses_default_power
                        )
                        self._clear(message)
                        _LOGGER.error("Dispatch stopped after renewal failed: %s", message)
                        return
        except asyncio.CancelledError:
            # Replacement / stop owns release; this task must not stop a new session.
            return

    async def async_stop(self, *, close: bool = False) -> None:
        async with self._lock:
            if close:
                self._closed = True
            active = self._task is not None or self.mode in ("charge", "discharge", "error")
            self._cancel_task()
            if not close or active:
                error = await self._release()
                self._clear(error)
                if error and not close:
                    raise PowerwallError(error)
            else:
                self._clear()
