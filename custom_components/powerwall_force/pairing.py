"""One-time Tesla public-key registration, isolated from local runtime control."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from .client import MAX_RESPONSE_BYTES, load_private_key
from .protocol import AuthenticationError, PowerwallError

FLEET_BASES = {
    "Europe": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "North America / Asia-Pacific": "https://fleet-api.prd.na.vn.cloud.tesla.com",
}


@dataclass(frozen=True)
class EnergySite:
    site_id: str
    din: str
    name: str


def prepare_key(directory: str, din: str) -> tuple[str, rsa.RSAPrivateKey]:
    """Create a private file before registering; retries reuse the same key."""
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / f"{hashlib.sha256(din.encode()).hexdigest()}.pem"
    if path.is_symlink():
        raise ValueError("Managed key path must not be a symbolic link")
    if path.exists():
        return str(path), load_private_key(str(path))
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return str(path), load_private_key(str(path))
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pem)
        handle.flush()
        os.fsync(handle.fileno())
    return str(path), key


def public_key(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )


def _canonical(name: str) -> str:
    return name.replace("_", "").lower()


def matching_key_state(data: object, public_der: bytes) -> int | None:
    """Match the exact public key, never another already-verified app key."""
    if isinstance(data, dict):
        fields = {_canonical(k): v for k, v in data.items()}
        candidate = fields.get("publickey")
        if isinstance(candidate, str):
            try:
                matches = base64.b64decode(candidate, validate=True) == public_der
            except ValueError:
                matches = False
            if matches:
                try:
                    return int(fields["state"])
                except (ValueError, TypeError, KeyError):
                    return None
        for value in data.values():
            state = matching_key_state(value, public_der)
            if state is not None:
                return state
    elif isinstance(data, list):
        for value in data:
            state = matching_key_state(value, public_der)
            if state is not None:
                return state
    return None


class FleetPairing:
    """Minimal Fleet client used only while a setup wizard is open."""

    def __init__(self, session: aiohttp.ClientSession, base: str, token: str) -> None:
        if base not in FLEET_BASES.values():
            raise ValueError("Unsupported Tesla Fleet API region")
        self._session, self._base, self._token = session, base, token

    async def _json(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with self._session.request(
                method,
                self._base + path,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=20),
                allow_redirects=False,
                **kwargs,
            ) as response:
                if response.status in (401, 403):
                    raise AuthenticationError(
                        "Tesla authorization expired or lacks energy permissions"
                    )
                if response.status != 200:
                    raise PowerwallError(f"Tesla registration API returned HTTP {response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise PowerwallError("Tesla API response exceeded size limit")
                result = json.loads(raw)
                if not isinstance(result, dict) or result.get("error"):
                    raise PowerwallError("Tesla API rejected the pairing request")
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise PowerwallError("Could not read Tesla pairing API response") from err

    async def async_sites(self) -> list[EnergySite]:
        result = await self._json("GET", "/api/1/products")
        sites = []
        for product in result.get("response", []):
            if not isinstance(product, dict) or not product.get("energy_site_id"):
                continue
            din = product.get("gateway_din") or product.get("gateway_id")
            site_id = str(product["energy_site_id"])
            if not site_id.isdigit() or not isinstance(din, str) or not din:
                continue
            sites.append(
                EnergySite(site_id, din, product.get("site_name") or f"Powerwall {site_id}")
            )
        return sites

    async def _command(self, site: EnergySite, name: str, body: dict) -> dict:
        return await self._json(
            "POST",
            f"/api/1/energy_sites/{site.site_id}/command",
            json={
                "category": "authorization",
                "command_name": name,
                "command_type": "grpc_command",
                "command_properties": {
                    "identifier_type": 1,
                    "message": {"authorization": {name: body}},
                },
            },
        )

    async def async_register(self, site: EnergySite, public_der: bytes) -> None:
        await self._command(
            site,
            "add_authorized_client_request",
            {
                "key_type": 1,
                "authorized_client_type": 1,
                "public_key": base64.b64encode(public_der).decode(),
                "description": "Powerwall Force local control",
            },
        )

    async def async_key_state(self, site: EnergySite, public_der: bytes) -> int | None:
        response = await self._command(site, "list_authorized_clients_request", {})
        return matching_key_state(response, public_der)
