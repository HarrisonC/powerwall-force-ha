"""Timed dispatch sessions backed by short gateway-side leases."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from homeassistant.helpers.storage import Store

from .client import LocalPowerwall
from .const import (
    LEASE_SECONDS,
    MAX_DURATION_SECONDS,
    RENEW_SECONDS,
    RENEW_TIMEOUT,
    REQUEST_TIMEOUT,
    RESTORE_ATTEMPTS,
    RESTORE_INTERVAL,
)
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
        store: Store[bool],
    ) -> None:
        self.client = client
        self.max_charge = max_charge
        self.max_discharge = max_discharge
        self._create_task = create_task
        self._notify = notify
        self._store = store
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._recovery_task: asyncio.Task | None = None
        self._resume_required = False
        self._deadline = 0.0
        self._closed = False
        self._gateway_limits: dict[str, float] = {}
        self.mode = "idle"
        self.power_w: float | None = None
        self.ends_at: datetime | None = None
        self._dispatch_failure: str | None = None
        self.restore_status: str | None = None
        self.restore_error: str | None = None
        self._uses_default_power = False

    @property
    def last_error(self) -> str | None:
        return self._dispatch_failure or self.restore_error

    @property
    def dispatch_power_w(self) -> float | None:
        if self.power_w is not None:
            return self._limited_watts(self.mode, self.power_w)
        return None

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
        self._dispatch_failure = error
        self.mode = "error" if self.last_error else "idle"
        self.power_w = None
        self.ends_at = None
        self._notify()

    async def async_recover(self) -> None:
        """Release a previously owned dispatch when Home Assistant starts."""
        if await self._store.async_load() is True:
            self._resume_required = True
            self.restore_status = "pending"
            self._schedule_recovery(RESTORE_ATTEMPTS, delay=0)

    async def _record_dispatch(self) -> None:
        if self._resume_required:
            return
        try:
            # Keep the Store's write lock held even if the service is cancelled.
            await asyncio.shield(self._store.async_save(True))
            # Store can log a write failure instead of raising it.
            self._resume_required = await self._store.async_load() is True
        except OSError as err:
            raise PowerwallError("Could not save the dispatch recovery record") from err
        if not self._resume_required:
            raise PowerwallError("Could not save the dispatch recovery record")

    async def _release(self, *, keep_record: bool = False) -> bool:
        self.restore_status = "pending"
        try:
            await self.client.async_resume()
        except PowerwallError as err:
            self.restore_error = str(err)
            _LOGGER.warning("Cannot release dispatch; resume remains pending: %s", err)
            return False

        self.restore_status = "confirmed"
        self.restore_error = None
        if self._resume_required and not keep_record:
            self._resume_required = False
            try:
                await asyncio.shield(self._store.async_save(False))
            except OSError:
                _LOGGER.exception("Could not clear the dispatch recovery record")
        return True

    def _schedule_recovery(self, attempts: int, *, delay: float) -> None:
        if not self._closed and self._recovery_task is None:
            self._recovery_task = self._create_task(
                self._recover(attempts, delay), "Powerwall dispatch recovery"
            )

    async def _recover(self, attempts: int, delay: float) -> None:
        try:
            for _ in range(attempts):
                await asyncio.sleep(delay)
                async with self._lock:
                    if self._closed or self.restore_status != "pending":
                        return
                    released = await self._release()
                    self._clear(self._dispatch_failure)
                    if released:
                        return
                delay = RESTORE_INTERVAL
            _LOGGER.error("Powerwall resume remains unconfirmed; use Stop to retry")
        finally:
            if self._recovery_task is asyncio.current_task():
                self._recovery_task = None

    async def _finish(self, error: str | None = None, *, resume: bool = True) -> None:
        try:
            if resume:
                await self._release()
        finally:
            self._clear(error)
            if self.restore_status == "pending":
                self._schedule_recovery(RESTORE_ATTEMPTS - 1, delay=RESTORE_INTERVAL)

    def _limited_watts(self, mode: str, watts: float) -> float:
        return math.copysign(min(abs(watts), self._gateway_limits.get(mode, math.inf)), watts)

    async def _send_lease(
        self, watts: float, deadline: float, previous_expiry: float | None, *, retry: bool = False
    ) -> tuple[float, int]:
        if self._store.hass.is_stopping:
            raise PowerwallError("Home Assistant is shutting down")
        sent_at = asyncio.get_running_loop().time()
        remaining = deadline - sent_at
        if remaining <= 0:
            raise PowerwallError("Dispatch window expired before sending power")
        lease = min(LEASE_SECONDS, math.ceil(remaining))
        self.restore_status = None
        if previous_expiry is not None:
            budget = min(RENEW_TIMEOUT, remaining, previous_expiry - sent_at)
            if budget <= 0:
                raise PowerwallError("Previous dispatch lease may have expired")
            await self.client.async_dispatch(watts, lease, time_budget=budget)
        elif retry:
            budget = min(REQUEST_TIMEOUT, remaining)
            await self.client.async_dispatch(watts, lease, time_budget=budget)
        else:
            await self.client.async_dispatch(watts, lease)
        return sent_at, lease

    async def _dispatch(
        self, mode: str, watts: float, deadline: float, previous_expiry: float | None = None
    ) -> tuple[float, int]:
        watts = self._limited_watts(mode, watts)
        try:
            return await self._send_lease(watts, deadline, previous_expiry)
        except CommandRejected as err:
            limit = err.lower_power_limit(watts)
            if limit is None:
                raise
            self._gateway_limits[mode] = abs(limit)
            _LOGGER.info("Gateway reported a %s limit of %g W", mode, abs(limit))
            if not await self._release(keep_record=True):
                raise
            return await self._send_lease(limit, deadline, None, retry=True)

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
            if self._closed or self._store.hass.is_stopping:
                raise PowerwallError("Integration is unloading")
            if self.restore_status == "pending":
                raise PowerwallError("Powerwall resume is still pending; use Stop to retry")
            self.restore_status = None
            try:
                await self._record_dispatch()
            except PowerwallError as err:
                self._clear(str(err))
                raise
            self._cancel_task()
            # Include network time in the requested period, never renew past it.
            deadline = asyncio.get_running_loop().time() + duration
            try:
                sent_at, lease = await self._dispatch(mode, watts, deadline)
            except asyncio.CancelledError:
                self.restore_status = "pending"
                self._clear("Dispatch start was cancelled")
                self._schedule_recovery(RESTORE_ATTEMPTS, delay=0)
                raise
            except PowerwallError as err:
                # A lost acknowledgement may still mean the gateway accepted it.
                message = self._dispatch_error(err, mode, power, uses_default)
                await self._finish(message, resume=self.restore_status != "pending")
                if message != str(err):
                    raise PowerwallError(message) from err
                raise
            self.mode, self.power_w = mode, watts
            self._uses_default_power = uses_default
            self._deadline = deadline
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            self.ends_at = datetime.now(UTC) + timedelta(seconds=remaining)
            self._dispatch_failure = None
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
                    if now >= self._deadline:
                        self._task = None
                        await self._finish()
                        return
                    old_power = self.dispatch_power_w
                    try:
                        sent_at, lease = await self._dispatch(
                            self.mode, self.power_w, self._deadline, sent_at + lease
                        )
                    except PowerwallError as err:
                        self._task = None
                        message = self._dispatch_error(
                            err, self.mode, abs(self.power_w), self._uses_default_power
                        )
                        await self._finish(message, resume=self.restore_status != "pending")
                        _LOGGER.error("Dispatch stopped after renewal failed: %s", message)
                        return
                    if self.dispatch_power_w != old_power:
                        self._notify()
        except asyncio.CancelledError:
            # Replacement / stop owns release; this task must not stop a new session.
            return

    async def async_stop(self, *, close: bool = False) -> None:
        async with self._lock:
            if self._closed:
                return
            if close:
                self._closed = True
            self._cancel_task()
            if self._recovery_task is not None:
                self._recovery_task.cancel()
                self._recovery_task = None
            if not close or self._resume_required:
                await self._finish()
                if self.restore_error and not close:
                    raise PowerwallError(self.restore_error)
            else:
                self._clear()
