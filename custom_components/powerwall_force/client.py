"""Async LAN-only transport; no Tesla cloud access or background polling."""

from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import logging
import re
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .const import REQUEST_TIMEOUT, TELEMETRY_TIMEOUT
from .protocol import (
    AuthenticationError,
    PowerwallError,
    TelemetryBusy,
    config_request,
    dispatch_request,
    nested,
    require_dispatch_ack,
    response_envelope,
    resume_request,
    signed_request,
    telemetry_request,
    telemetry_response,
)
from .telemetry import Telemetry, parse_telemetry

_LOGGER = logging.getLogger(__name__)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def normalize_host(host: str) -> str:
    """Accept an IP or DNS hostname, never a URL, path, or credentials."""
    host = host.strip()
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
        return f"[{ip}]" if ip.version == 6 else str(ip)
    except ValueError:
        if len(host) > 253 or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in host.rstrip(".").split(".")
        ):
            raise ValueError("Enter only the gateway IP address or hostname") from None
    return host.lower()


def load_private_key(path: str) -> rsa.RSAPrivateKey:
    """Run in an executor: file IO and PEM decoding can block."""
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey) or key.key_size != 4096:
        raise ValueError("A registered, unencrypted RSA-4096 PEM key is required")
    return key


class LocalPowerwall:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        password: str | None,
        key: rsa.RSAPrivateKey,
        *,
        din: str | None = None,
    ) -> None:
        self._session = session
        self._base = f"https://{normalize_host(host)}"
        self._password = password
        self._key = key
        self._token: str | None = None
        self.din = din
        self._lock = asyncio.Lock()
        self._telemetry_task: asyncio.Task | None = None

    async def _request(
        self, method: str, path: str, *, time_budget: float | None = None, **kwargs
    ) -> bytes:
        try:
            async with self._session.request(
                method,
                self._base + path,
                ssl=False,  # Tesla gateway uses a self-signed certificate.
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(
                    total=REQUEST_TIMEOUT if time_budget is None else time_budget
                ),
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError("Gateway login expired or was rejected")
                if response.status != 200:
                    raise PowerwallError(f"Gateway returned HTTP {response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise PowerwallError("Gateway response exceeded size limit")
                return bytes(raw)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PowerwallError("Cannot reach the local Powerwall gateway") from err

    async def _login(self) -> None:
        raw = await self._request(
            "POST",
            "/api/login/Basic",
            json={
                "username": "customer",
                "password": self._password,
                "email": "customer@customer.domain",
                "clientInfo": {"timezone": "UTC"},
            },
        )
        try:
            token = json.loads(raw)["token"]
            if not isinstance(token, str) or not token:
                raise ValueError("Empty token")
        except (ValueError, KeyError, TypeError) as err:
            raise AuthenticationError("Gateway returned no login token") from err
        self._token = token

    async def _send(self, payload: bytes, *, time_budget: float | None = None) -> bytes:
        if self._password is None:
            # Pairing supplies the DIN; v1r authenticates the RSA signature itself.
            signed = await asyncio.get_running_loop().run_in_executor(
                None, signed_request, payload, self.din, self._key
            )
            return await self._request(
                "POST",
                "/tedapi/v1r",
                time_budget=time_budget,
                data=signed,
                headers={"Content-Type": "application/octet-stream"},
            )
        for attempt in range(2):
            if self._token is None:
                await self._login()
            # Re-sign after login; signatures are valid for only 12 seconds.
            signed = await asyncio.get_running_loop().run_in_executor(
                None, signed_request, payload, self.din, self._key
            )
            try:
                return await self._request(
                    "POST",
                    "/tedapi/v1r",
                    time_budget=time_budget,
                    data=signed,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Authorization": f"Bearer {self._token}",
                    },
                )
            except AuthenticationError:
                self._token = None
                if attempt:
                    raise
        raise AssertionError("Unreachable")

    async def async_connect(self) -> str:
        """Read DIN and config to verify the key without actuating the battery."""
        async with self._lock:
            try:
                if self._password is not None:
                    await self._login()
                    raw = await self._request(
                        "GET", "/tedapi/din", headers={"Authorization": f"Bearer {self._token}"}
                    )
                    if raw.startswith(b"\x1f\x8b"):
                        raw = gzip.decompress(raw)
                    self.din = raw.decode().strip()
                din = self.din or ""
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", din):
                    raise ValueError("Invalid DIN")
                self.din = din
                response = response_envelope(await self._send(config_request(din)))
                file = nested(nested(nested(response, 15), 2), 1)
                config = json.loads(file.get(100, b""))
                if not isinstance(config, dict):
                    raise ValueError("Config must be an object")
            except (ValueError, UnicodeError, OSError, TypeError) as err:
                raise PowerwallError("Gateway returned invalid identity or config data") from err
            return din

    async def async_dispatch(
        self, watts: float, seconds: int, *, time_budget: float | None = None
    ) -> None:
        self.cancel_telemetry()
        time_budget = REQUEST_TIMEOUT if time_budget is None else time_budget
        try:
            # The controller bounds renewals by the previous lease and the session end.
            # This deadline includes any lock wait, signing and bearer login.
            async with asyncio.timeout(time_budget), self._lock:
                if not self.din:
                    raise PowerwallError("Gateway has not been connected")
                payload = dispatch_request(self.din, watts, seconds)
                require_dispatch_ack(await self._send(payload, time_budget=time_budget))
        except TimeoutError as err:
            raise PowerwallError(
                f"Powerwall dispatch timed out after {time_budget:g} seconds"
            ) from err

    async def async_resume(self) -> None:
        self.cancel_telemetry()
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT), self._lock:
                if not self.din:
                    raise PowerwallError("Gateway has not been connected")
                require_dispatch_ack(await self._send(resume_request(self.din)))
        except TimeoutError as err:
            raise PowerwallError("Powerwall release timed out") from err

    def cancel_telemetry(self) -> None:
        """Preempt a low-priority read before sending a battery command."""
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()

    async def async_cancel_telemetry(self) -> None:
        task = self._telemetry_task
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, PowerwallError):
                pass

    async def async_read_telemetry(self) -> Telemetry:
        if self._lock.locked() or self._telemetry_task is not None:
            raise TelemetryBusy("Telemetry deferred while the gateway is busy")
        task = asyncio.create_task(self._read_telemetry(), name="Powerwall local telemetry")
        self._telemetry_task = task
        try:
            return await task
        except asyncio.CancelledError:
            # Only an explicit command preemption becomes a skipped read.
            # Unload/shutdown cancellation must still propagate to the caller.
            if asyncio.current_task().cancelling():
                raise
            raise TelemetryBusy("Telemetry preempted by a battery command") from None
        finally:
            if self._telemetry_task is task:
                self._telemetry_task = None

    async def _read_telemetry(self) -> Telemetry:
        if not self.din:
            raise PowerwallError("Gateway has not been connected")
        payload = telemetry_request(self.din)
        for attempt in range(2):
            try:
                async with asyncio.timeout(TELEMETRY_TIMEOUT), self._lock:
                    response = await self._send(payload)
                    return parse_telemetry(telemetry_response(response))
            except TimeoutError as err:
                if attempt:
                    raise PowerwallError(
                        "Powerwall telemetry read timed out on both attempts"
                    ) from err
                _LOGGER.debug("Powerwall telemetry read timed out, retrying once")
