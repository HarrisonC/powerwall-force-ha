"""Validate measured values, directions and missing data without live hardware."""

import json
from pathlib import Path

import pytest

from custom_components.powerwall_force.protocol import (
    PowerwallError,
    blob,
    telemetry_request,
    telemetry_response,
)
from custom_components.powerwall_force.telemetry import parse_telemetry


def test_status_request_matches_upstream_query_and_generated_schema(upstream_queries):
    capture = json.loads(
        (Path(__file__).parent / "fixtures/upstream_status_query.json").read_text()
    )
    envelope = upstream_queries.MessageEnvelope()
    envelope.deliveryChannel = 1
    envelope.sender.local = 1
    envelope.recipient.din = "DIN"
    envelope.payload.send.num = 2
    envelope.payload.send.payload.value = 1
    envelope.payload.send.payload.text = capture["text"]
    envelope.payload.send.code = bytes.fromhex(capture["code"])
    envelope.payload.send.b.value = capture["b_value"]
    assert telemetry_request("DIN") == envelope.SerializeToString()


def test_decode_generated_response(upstream_queries, telemetry_payload):
    envelope = upstream_queries.MessageEnvelope()
    envelope.payload.recv.value = 1
    envelope.payload.recv.text = json.dumps(telemetry_payload)
    assert telemetry_response(blob(10, envelope.SerializeToString())) == telemetry_payload


@pytest.mark.parametrize("raw", [b"", blob(10, b""), blob(10, blob(16, b""))])
def test_missing_query_response_is_an_error(raw):
    with pytest.raises(PowerwallError):
        telemetry_response(raw)


@pytest.mark.parametrize("value", [b"not json", b"[]", b"null", b"\xff"])
def test_invalid_json_is_an_error(value):
    with pytest.raises(PowerwallError, match="invalid telemetry"):
        telemetry_response(blob(10, blob(16, blob(2, blob(2, value)))))


def test_values_units_and_direction(telemetry_payload):
    data = parse_telemetry(telemetry_payload)
    assert data.battery_level == 50
    assert data.battery_energy == 6.75
    assert data.battery_capacity == 13.5
    assert data.battery_power == -1000
    assert data.solar_power == 2500
    assert data.home_power == 1600
    assert data.grid_power == 100
    assert data.grid_import == 100
    assert data.grid_export == 0
    assert data.grid_connected is True

    telemetry_payload["control"]["meterAggregates"][0]["realPowerW"] = -900
    data = parse_telemetry(telemetry_payload)
    assert data.grid_import == 0
    assert data.grid_export == 900


def test_zeroes_are_measurements_and_missing_is_not_zero(telemetry_payload):
    control = telemetry_payload["control"]
    control["systemStatus"]["nominalEnergyRemainingWh"] = 0
    control["meterAggregates"] = [{"location": "battery", "realPowerW": 0}]
    data = parse_telemetry(telemetry_payload)
    assert data.battery_level == 0
    assert data.battery_energy == 0
    assert data.battery_power == 0
    assert data.solar_power is None
    assert data.home_power is None
    assert data.grid_import is None
    assert data.grid_export is None


@pytest.mark.parametrize("value", [True, "1500", float("nan"), float("inf"), {}, 10**500])
def test_invalid_measurements_are_unavailable(telemetry_payload, value):
    telemetry_payload["control"]["meterAggregates"][0]["realPowerW"] = value
    data = parse_telemetry(telemetry_payload)
    assert data.grid_power is None
    assert data.grid_import is None
    assert data.battery_power == -1000


@pytest.mark.parametrize("remaining,capacity", [(-1, 13500), (10, 0), (20, 10)])
def test_invalid_energy_does_not_generate_a_percentage(telemetry_payload, remaining, capacity):
    telemetry_payload["control"]["systemStatus"] = {
        "nominalEnergyRemainingWh": remaining,
        "nominalFullPackEnergyWh": capacity,
    }
    assert parse_telemetry(telemetry_payload).battery_level is None


@pytest.mark.parametrize(
    "state,expected",
    [
        ({"contactorClosed": False, "gridOK": True, "microGridOK": True}, False),
        ({"contactorClosed": False, "gridOK": False, "microGridOK": True}, False),
        ({"contactorClosed": True, "gridOK": False, "microGridOK": True}, None),
        ({"contactorClosed": True, "gridOK": True, "microGridOK": False}, None),
        ({"customerIslandMode": "BACKUP", "gridOK": True}, None),
        ({"contactorClosed": 1, "gridOK": True, "microGridOK": True}, None),
        (None, None),
    ],
)
def test_grid_state_uses_physical_readback(telemetry_payload, state, expected):
    telemetry_payload["control"]["islanding"] = state
    assert parse_telemetry(telemetry_payload).grid_connected is expected


@pytest.mark.parametrize("data", [{}, {"control": None}, {"control": {}}, {"control": []}])
def test_empty_status_is_an_error(data):
    with pytest.raises(PowerwallError, match="telemetry"):
        parse_telemetry(data)


def test_duplicate_meter_is_not_an_unambiguous_reading(telemetry_payload):
    telemetry_payload["control"]["meterAggregates"].append({"location": "SITE", "realPowerW": 800})
    assert parse_telemetry(telemetry_payload).grid_power is None
