"""Wire vectors, real RSA verification, and rejection of gateway faults."""

import struct
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from custom_components.powerwall_force.protocol import (
    AuthenticationError,
    CommandRejected,
    PowerwallError,
    blob,
    config_request,
    dispatch_request,
    fields,
    nested,
    require_dispatch_ack,
    response_envelope,
    resume_request,
    signed_request,
    uint,
    varint,
)


def test_dispatch_wire_vectors():
    request = dispatch_request("DIN", -2500.0, 30)
    power = nested(nested(nested(fields(request), 5), 37), 2)
    assert struct.unpack("<d", power[1])[0] == -2500
    assert power[2] == 30
    assert nested(nested(fields(resume_request("DIN")), 5), 37) == {1: b""}
    assert nested(nested(fields(config_request("DIN")), 15), 1) == {1: 1, 2: b"config.json"}


def test_rsa_signature_and_metadata(rsa_key):
    inner = dispatch_request("DIN-123", 1234, 30)
    with patch("custom_components.powerwall_force.protocol.time.time", return_value=1000.2):
        wire = fields(signed_request(inner, "DIN-123", rsa_key))
    assert nested(wire, 6) == {1: 7}
    assert wire[10] == inner
    signature_data = nested(wire, 13)
    assert nested(signature_data, 1)[1] == rsa_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.PKCS1
    )
    signature = nested(signature_data, 7)
    expires = struct.unpack("<I", signature[1])[0]
    assert expires == 1013
    metadata = (
        bytes.fromhex("0001070101070207")
        + b"DIN-123"
        + bytes.fromhex("0404")
        + struct.pack(">I", expires)
        + b"\xff"
        + inner
    )
    rsa_key.public_key().verify(signature[2], metadata, padding.PKCS1v15(), hashes.SHA512())


@pytest.mark.parametrize("raw", [b"\x00", b"\x80", b"\x0a\x05x", b"\x0b", b"\x09x", b"\xff" * 12])
def test_reject_malformed_protobuf(raw):
    with pytest.raises(PowerwallError):
        fields(raw)


@pytest.mark.parametrize("value", [0, 127, 128, 65536, 2**64 - 1])
def test_varints(value):
    assert fields(uint(1, value)) == {1: value}


def test_reject_invalid_varint():
    with pytest.raises(ValueError):
        varint(-1)


def test_dispatch_ack_and_faults():
    require_dispatch_ack(blob(10, blob(5, blob(38, b""))))
    with pytest.raises(PowerwallError, match="acknowledge"):
        require_dispatch_ack(blob(10, blob(5, b"")))
    with pytest.raises(AuthenticationError):
        response_envelope(blob(12, uint(1, 3)))
    with pytest.raises(AuthenticationError, match="not verified"):
        response_envelope(b"v1r: client authorization not verified")
    with pytest.raises(PowerwallError, match="fault 2"):
        response_envelope(blob(12, uint(1, 2)))
    error = blob(10, blob(4, blob(1, blob(1, uint(1, 12)))))
    with pytest.raises(PowerwallError, match="unsupported"):
        require_dispatch_ack(error)
    with pytest.raises(PowerwallError, match="no signed-message payload"):
        response_envelope(b"")


def test_dispatch_rejection_preserves_gateway_explanation():
    status = uint(1, 11) + blob(2, b"Power must be at most 10000 W\n")
    raw = blob(10, blob(4, blob(1, blob(1, status))))
    with pytest.raises(CommandRejected) as rejection:
        require_dispatch_ack(raw)
    assert rejection.value.status_code == 11
    assert rejection.value.detail == "Power must be at most 10000 W"
