"""Cross-check our minimal codec against independently generated upstream types."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import grpc_tools
import pytest

from custom_components.powerwall_force.protocol import (
    config_request,
    dispatch_request,
    require_dispatch_ack,
    resume_request,
    signed_request,
)


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    output = tmp_path_factory.mktemp("protobuf")
    source = Path(__file__).parent / "fixtures"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{source}",
            f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
            f"--python_out={output}",
            str(source / "upstream_combined.proto"),
        ],
        check=True,
        capture_output=True,
    )
    spec = importlib.util.spec_from_file_location(
        "upstream_combined_pb2", output / "upstream_combined_pb2.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("watts,seconds", [(-2500, 30), (10000, 1), (1.5, 86400)])
def test_dispatch_matches_generated_schema(upstream, watts, seconds):
    expected = upstream.MessageEnvelope()
    expected.deliveryChannel = upstream.DELIVERY_CHANNEL_HERMES_COMMAND
    expected.sender.authorizedClient = 1
    expected.recipient.din = "DIN"
    command = expected.teg.dispatch_battery_power_request.real_power
    command.power_watts = watts
    command.duration_seconds = seconds
    assert dispatch_request("DIN", watts, seconds) == expected.SerializeToString()


def test_resume_and_config_match_generated_schema(upstream):
    expected = upstream.MessageEnvelope()
    expected.deliveryChannel = upstream.DELIVERY_CHANNEL_HERMES_COMMAND
    expected.sender.authorizedClient = 1
    expected.recipient.din = "DIN"
    expected.teg.dispatch_battery_power_request.resume_device_mode.SetInParent()
    assert resume_request("DIN") == expected.SerializeToString()
    expected.ClearField("teg")
    expected.filestore.readFileRequest.domain = upstream.FILE_STORE_API_DOMAIN_CONFIG_JSON
    expected.filestore.readFileRequest.name = "config.json"
    assert config_request("DIN") == expected.SerializeToString()


def test_generated_schema_reads_signature_and_ack(upstream, rsa_key):
    signed = upstream.RoutableMessage()
    signed.ParseFromString(signed_request(dispatch_request("DIN", 1000, 30), "DIN", rsa_key))
    assert signed.to_destination.domain == upstream.DOMAIN_ENERGY_DEVICE
    assert signed.signature_data.rsa_data.expires_at > 0
    assert len(signed.signature_data.rsa_data.signature) == 512
    assert len(signed.signature_data.signer_identity.public_key) > 500
    reply = upstream.RoutableMessage()
    envelope = upstream.MessageEnvelope()
    envelope.teg.dispatch_battery_power_response.SetInParent()
    reply.protobuf_message_as_bytes = envelope.SerializeToString()
    require_dispatch_ack(reply.SerializeToString())
