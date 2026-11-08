"""Small, strict protobuf wire subset for RSA TEDAPI battery dispatch.

Field numbers follow the upstream schemas linked in docs/protocol.md. This
module deliberately implements only the messages we use, avoiding a generated
copy of Tesla's entire API and a protobuf runtime dependency.
"""

from __future__ import annotations

import json
import math
import struct
import time
import uuid

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .status_query import STATUS_QUERY, STATUS_QUERY_CODE


class PowerwallError(Exception):
    """A local gateway request failed or was rejected."""


class AuthenticationError(PowerwallError):
    """Credentials or the RSA registration need attention."""


class TelemetryBusy(PowerwallError):
    """A read was deferred to give a battery command priority."""


class CommandRejected(PowerwallError):
    """Keep the gateway status and explanation available to the controller."""

    def __init__(self, status_code: int, detail: str = "") -> None:
        self.status_code = status_code
        self.detail = detail
        hint = " (command unsupported by this firmware)" if status_code == 12 else ""
        message = f"Gateway rejected command: status {status_code}{hint}"
        super().__init__(f"{message}: {detail}" if detail else message)


def varint(value: int) -> bytes:
    """Encode an unsigned protobuf varint."""
    if not 0 <= value < 2**64:
        raise ValueError("Varint outside uint64 range")
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def uint(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def blob(field: int, value: bytes) -> bytes:
    return varint(field << 3 | 2) + varint(len(value)) + value


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data):
            break
        byte = data[offset]
        offset += 1
        if shift == 63 and byte > 1:
            break
        value |= (byte & 127) << shift
        if byte < 128:
            return value, offset
    raise PowerwallError("Malformed protobuf varint")


def fields(data: bytes) -> dict[int, int | bytes]:
    """Read scalar fields, skipping no bytes silently on malformed input."""
    result: dict[int, int | bytes] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        field, wire = tag >> 3, tag & 7
        if not 0 < field < 2**29:
            raise PowerwallError("Invalid protobuf field number")
        if wire == 0:
            value, offset = _read_varint(data, offset)
        else:
            if wire == 2:
                length, offset = _read_varint(data, offset)
            elif wire in (1, 5):
                length = 8 if wire == 1 else 4
            else:
                raise PowerwallError("Unsupported protobuf wire type")
            end = offset + length
            if end > len(data):
                raise PowerwallError("Truncated protobuf message")
            value, offset = data[offset:end], end
        result[field] = value
    return result


def nested(data: dict[int, int | bytes], field: int) -> dict[int, int | bytes]:
    value = data.get(field, b"")
    if not isinstance(value, bytes):
        raise PowerwallError("Unexpected protobuf field type")
    return fields(value)


def envelope(din: str, payload_field: int, payload: bytes) -> bytes:
    return (
        uint(1, 2)  # HERMES_COMMAND
        + blob(2, uint(4, 1))  # CUSTOMER_MOBILE_APP
        + blob(3, blob(1, din.encode()))
        + blob(payload_field, payload)
    )


def config_request(din: str) -> bytes:
    request = uint(1, 1) + blob(2, b"config.json")
    return envelope(din, 15, blob(1, request))


def telemetry_request(din: str) -> bytes:
    """Wrap the verbatim Tesla-signed read query in its local query envelope."""
    query = (
        uint(1, 2)
        + blob(2, uint(1, 1) + blob(2, STATUS_QUERY.encode()))
        + blob(3, STATUS_QUERY_CODE)
        + blob(4, blob(1, b"{}"))
    )
    return (
        uint(1, 1)  # LOCAL query delivery, distinct from a dispatch envelope.
        + blob(2, uint(3, 1))
        + blob(3, blob(1, din.encode()))
        + blob(16, blob(1, query))
    )


def telemetry_response(raw: bytes) -> dict:
    """Read the query's JSON result after checking signed-message errors."""
    result = nested(nested(response_envelope(raw), 16), 2).get(2)
    if not isinstance(result, bytes) or not result:
        raise PowerwallError("Gateway returned no telemetry query result")
    try:
        data = json.loads(result)
    except (ValueError, UnicodeError) as err:
        raise PowerwallError("Gateway returned invalid telemetry JSON") from err
    if not isinstance(data, dict):
        raise PowerwallError("Gateway returned invalid telemetry data")
    return data


def dispatch_request(din: str, watts: float, seconds: int) -> bytes:
    if not math.isfinite(watts) or not 1 <= seconds <= 86400:
        raise ValueError("Dispatch requires finite watts and 1–86400 seconds")
    power = varint(1 << 3 | 1) + struct.pack("<d", watts) + uint(2, seconds)
    return envelope(din, 5, blob(37, blob(2, power)))


def resume_request(din: str) -> bytes:
    return envelope(din, 5, blob(37, blob(1, b"")))


def signed_request(inner: bytes, din: str, key: rsa.RSAPrivateKey) -> bytes:
    """Sign personalized TLV metadata with RSA PKCS1v15 / SHA512."""
    identity = din.encode()
    if not 1 <= len(identity) <= 255:
        raise ValueError("Invalid gateway DIN")
    expires = math.ceil(time.time()) + 12
    metadata = (
        b"\x00\x01\x07\x01\x01\x07\x02"
        + bytes([len(identity)])
        + identity
        + b"\x04\x04"
        + struct.pack(">I", expires)
        + b"\xff"
        + inner
    )
    signature = key.sign(metadata, padding.PKCS1v15(), hashes.SHA512())
    public = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )
    rsa_data = varint(1 << 3 | 5) + struct.pack("<I", expires) + blob(2, signature)
    signature_data = blob(1, blob(1, public)) + blob(7, rsa_data)
    return (
        blob(6, uint(1, 7))
        + blob(10, inner)
        + blob(13, signature_data)
        + blob(51, str(uuid.uuid4()).encode())
    )


def response_envelope(raw: bytes) -> dict[int, int | bytes]:
    """Reject authorization faults and common API errors before reading payload."""
    if b"authorization not verified" in raw.lower():
        raise AuthenticationError("RSA key is registered but not verified")
    outer = fields(raw)
    fault = nested(outer, 12).get(1, 0)
    if fault in (3, 4, 5, 7):
        raise AuthenticationError(f"RSA authorization rejected (fault {fault})")
    if fault:
        raise PowerwallError(f"Gateway message fault {fault}")
    if not outer.get(10):
        raise PowerwallError("Gateway response has no signed-message payload")
    inner = nested(outer, 10)
    common = nested(inner, 4)
    if 1 in common:
        status = nested(nested(common, 1), 1)
        code = status.get(1, 0)
        if not isinstance(code, int):
            raise PowerwallError("Invalid gateway command status")
        detail = status.get(2, b"")
        if not isinstance(detail, bytes):
            raise PowerwallError("Invalid gateway command status message")
        detail = " ".join(detail.decode("utf-8", errors="replace").split())[:250]
        raise CommandRejected(code, detail)
    return inner


def require_dispatch_ack(raw: bytes) -> None:
    teg = nested(response_envelope(raw), 5)
    if 38 not in teg:
        raise PowerwallError("Gateway did not acknowledge battery dispatch")
    nested(teg, 38)  # Acknowledgment must be a valid message, not a scalar field.
