"""Real aiohttp transport against an in-process mock gateway."""

import asyncio
import gzip
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web
from cryptography.hazmat.primitives import serialization

from custom_components.powerwall_force import client as client_module
from custom_components.powerwall_force.client import (
    LocalPowerwall,
    load_private_key,
    normalize_host,
)
from custom_components.powerwall_force.protocol import (
    AuthenticationError,
    PowerwallError,
    TelemetryBusy,
    blob,
    fields,
    nested,
)


@pytest.mark.parametrize(
    "host", ["https://192.168.1.2", "u:p@gateway", "x/path", "a b", "", "x?y", "x:443"]
)
def test_reject_invalid_hosts(host):
    with pytest.raises(ValueError):
        normalize_host(host)


@pytest.mark.parametrize(
    "host,result",
    [
        ("192.168.1.2", "192.168.1.2"),
        ("POWERWALL.local", "powerwall.local"),
        ("::1", "[::1]"),
        ("[::1]", "[::1]"),
    ],
)
def test_hosts(host, result):
    assert normalize_host(host) == result


def test_load_key(tmp_path, rsa_key):
    path = tmp_path / "key.pem"
    path.write_bytes(
        rsa_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    assert load_private_key(str(path)).key_size == 4096


async def test_local_connect_dispatch_resume_and_reauth(aiohttp_server, rsa_key):
    seen = []
    logins = 0
    refuse_once = True

    async def login(request):
        nonlocal logins
        logins += 1
        assert (await request.json())["password"] == "abcde"
        return web.json_response({"token": f"token-{logins}"})

    async def din(request):
        assert request.headers["Authorization"] == "Bearer token-1"
        # Some firmware returns gzip without a Content-Encoding header.
        return web.Response(body=gzip.compress(b"1234567-00-A--SERIAL"))

    async def signed(request):
        nonlocal refuse_once
        raw = await request.read()
        outer = fields(raw)
        assert 13 in outer
        inner = nested(outer, 10)
        seen.append(raw)
        if 15 in inner:
            config = blob(1, blob(100, json.dumps({"control": {}}).encode()))
            return web.Response(body=blob(10, blob(15, blob(2, config))))
        if refuse_once:
            refuse_once = False
            return web.Response(status=401)
        assert request.headers["Authorization"] == "Bearer token-2"
        return web.Response(body=blob(10, blob(5, blob(38, b""))))

    app = web.Application()
    app.router.add_post("/api/login/Basic", login)
    app.router.add_get("/tedapi/din", din)
    app.router.add_post("/tedapi/v1r", signed)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway.local", "abcde", rsa_key)
        client._base = str(server.make_url("")).rstrip("/")
        assert await client.async_connect() == "1234567-00-A--SERIAL"
        await client.async_dispatch(-1000, 30)
        await client.async_resume()
    assert logins == 2
    assert len(seen) == 4
    assert fields(seen[1])[51] != fields(seen[2])[51]  # fresh signature envelope after re-login


async def test_bad_login_does_not_dispatch(aiohttp_server, rsa_key):
    async def reject(request):
        return web.Response(status=403)

    app = web.Application()
    app.router.add_post("/api/login/Basic", reject)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", "wrong", rsa_key)
        client._base = str(server.make_url("")).rstrip("/")
        with pytest.raises(AuthenticationError):
            await client.async_connect()


async def test_entire_renewal_has_one_deadline(rsa_key, monkeypatch):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT", 0.01)

    async def slow_send(payload, *, time_budget, report_phase):
        report_phase("waiting for the gateway response")
        await asyncio.sleep(1)

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", "abcde", rsa_key)
        client.din = "DIN"
        client._send = AsyncMock(side_effect=slow_send)
        with pytest.raises(PowerwallError, match="timed out.*waiting for the gateway response"):
            await client.async_dispatch(1000, 30)
        client._send.assert_awaited_once()


async def test_slower_renewal_uses_its_budget_for_the_http_request(
    aiohttp_server, rsa_key, monkeypatch
):
    monkeypatch.setattr(client_module, "REQUEST_TIMEOUT", 0.03)

    async def signed(request):
        await request.read()
        await asyncio.sleep(0.06)
        return web.Response(body=blob(10, blob(5, blob(38, b""))))

    app = web.Application()
    app.router.add_post("/tedapi/v1r", signed)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._base = str(server.make_url("")).rstrip("/")
        await client.async_dispatch(-1000, 30, time_budget=0.5)


async def test_telemetry_uses_existing_rsa_connection_only(
    aiohttp_server, rsa_key, upstream_queries, telemetry_payload
):
    seen = []

    async def signed(request):
        assert "Authorization" not in request.headers
        outer = fields(await request.read())
        assert 13 in outer
        inner = nested(outer, 10)
        seen.append(inner)
        if 15 in inner:
            config = blob(1, blob(100, b"{}"))
            return web.Response(body=blob(10, blob(15, blob(2, config))))
        envelope = upstream_queries.MessageEnvelope()
        envelope.ParseFromString(outer[10])
        assert envelope.recipient.din == "DIN"
        assert envelope.payload.send.payload.text.startswith(" query DeviceControllerQuery")
        reply = upstream_queries.MessageEnvelope()
        reply.payload.recv.value = 1
        reply.payload.recv.text = json.dumps(telemetry_payload)
        return web.Response(body=blob(10, reply.SerializeToString()))

    app = web.Application()
    app.router.add_post("/tedapi/v1r", signed)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._base = str(server.make_url("")).rstrip("/")
        assert await client.async_connect() == "DIN"
        snapshot = await client.async_read_telemetry()
        assert snapshot.battery_level == 50
        assert snapshot.grid_import == 100
    assert len(seen) == 2
    assert 15 in seen[0] and 16 in seen[1]


@pytest.mark.parametrize("action", ["dispatch", "resume"])
async def test_battery_commands_preempt_a_slow_read(rsa_key, action):
    reading, cancelled = asyncio.Event(), asyncio.Event()

    async def send(payload, *, time_budget=None, report_phase=None):
        if 16 in fields(payload):
            reading.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return blob(10, blob(5, blob(38, b"")))

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._send = AsyncMock(side_effect=send)
        read = asyncio.create_task(client.async_read_telemetry())
        await asyncio.wait_for(reading.wait(), 1)
        command = client.async_dispatch(1000, 30) if action == "dispatch" else client.async_resume()
        await asyncio.wait_for(command, 0.5)
        with pytest.raises(TelemetryBusy, match="preempted"):
            await read
        assert cancelled.is_set()
        assert not client._lock.locked()
        assert client._telemetry_task is None


async def test_telemetry_defers_to_a_command_already_in_flight(rsa_key):
    started, finish = asyncio.Event(), asyncio.Event()

    async def send(payload, *, time_budget=None, report_phase=None):
        started.set()
        await finish.wait()
        return blob(10, blob(5, blob(38, b"")))

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._send = AsyncMock(side_effect=send)
        command = asyncio.create_task(client.async_dispatch(1000, 30))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(TelemetryBusy, match="busy"):
            await client.async_read_telemetry()
        assert client._send.await_count == 1
        finish.set()
        await command


async def test_read_deadline_and_caller_cancellation_release_the_lock(rsa_key, monkeypatch):
    monkeypatch.setattr(client_module, "TELEMETRY_TIMEOUT", 0.01)

    async def send(payload):
        await asyncio.sleep(1)

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._send = AsyncMock(side_effect=send)
        with pytest.raises(PowerwallError, match="timed out on both attempts"):
            await client.async_read_telemetry()
        assert client._send.await_count == 2
        assert not client._lock.locked()
        read = asyncio.create_task(client.async_read_telemetry())
        await asyncio.sleep(0)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert client._telemetry_task is None
        assert not client._lock.locked()


async def test_one_telemetry_timeout_recovers_with_a_fresh_read(
    rsa_key, monkeypatch, telemetry_payload
):
    monkeypatch.setattr(client_module, "TELEMETRY_TIMEOUT", 0.01)
    raw = blob(10, blob(16, blob(2, blob(2, json.dumps(telemetry_payload).encode()))))
    attempts = 0

    async def send(payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await asyncio.Event().wait()
        return raw

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._send = AsyncMock(side_effect=send)
        data = await client.async_read_telemetry()
        assert data.grid_connected is True
        assert data.grid_power == 100
        assert attempts == 2


async def test_shutdown_awaits_read_cancellation(rsa_key):
    started = asyncio.Event()

    async def send(payload):
        started.set()
        await asyncio.Event().wait()

    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._send = AsyncMock(side_effect=send)
        read = asyncio.create_task(client.async_read_telemetry())
        await asyncio.wait_for(started.wait(), 1)
        await client.async_cancel_telemetry()
        with pytest.raises(TelemetryBusy):
            await read
        assert client._telemetry_task is None
        assert not client._lock.locked()
