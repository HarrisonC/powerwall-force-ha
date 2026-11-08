"""Key persistence and one-time Fleet registration, without Tesla requests."""

import base64
import json
import stat

from aiohttp import ClientSession, web

from custom_components.powerwall_force.pairing import (
    FLEET_BASES,
    FleetPairing,
    matching_key_state,
    prepare_key,
    public_key,
)


def test_generated_key_is_private_and_reused(tmp_path):
    path, key = prepare_key(str(tmp_path / "keys"), "DIN")
    from pathlib import Path

    assert stat.S_IMODE(Path(path).stat().st_mode) == 0o600
    again, same_key = prepare_key(str(tmp_path / "keys"), "DIN")
    assert again == path
    assert same_key.private_numbers() == key.private_numbers()
    assert key.key_size == 4096


def test_key_state_matches_only_our_public_key(rsa_key):
    public = public_key(rsa_key)
    data = {
        "response": {
            "message": {
                "Payload": {
                    "Authorization": {
                        "Message": {
                            "ListAuthorizedClientsResponse": {
                                "clients": [
                                    {
                                        "public_key": base64.b64encode(b"another app").decode(),
                                        "state": 3,
                                    },
                                    {"PublicKey": base64.b64encode(public).decode(), "State": 1},
                                ]
                            }
                        }
                    }
                }
            }
        }
    }
    assert matching_key_state(data, public) == 1
    assert matching_key_state(data, b"unregistered") is None
    assert matching_key_state({"clients": [{"public_key": "!bad", "state": 3}]}, public) is None


async def test_cloud_registration_targets_selected_site(aiohttp_server, rsa_key):
    commands = []

    async def products(request):
        assert request.headers["Authorization"] == "Bearer transient-token"
        return web.json_response(
            {
                "response": [
                    {"energy_site_id": 10, "gateway_id": "DIN-A", "site_name": "Home"},
                    {"energy_site_id": 20, "gateway_id": "DIN-B", "site_name": "Garage"},
                ]
            }
        )

    async def command(request):
        assert request.match_info["site_id"] == "20"
        commands.append(await request.json())
        return web.json_response(
            {
                "response": {
                    "clients": [
                        {
                            "public_key": base64.b64encode(public_key(rsa_key)).decode(),
                            "state": 3,
                        }
                    ]
                }
            }
        )

    app = web.Application()
    app.router.add_get("/api/1/products", products)
    app.router.add_post("/api/1/energy_sites/{site_id}/command", command)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = FleetPairing(session, FLEET_BASES["Europe"], "transient-token")
        client._base = str(server.make_url("")).rstrip("/")
        sites = await client.async_sites()
        await client.async_register(sites[1], public_key(rsa_key))
        assert await client.async_key_state(sites[1], public_key(rsa_key)) == 3
    assert commands[0]["category"] == "authorization"
    message = commands[0]["command_properties"]["message"]["authorization"]
    assert base64.b64decode(message["add_authorized_client_request"]["public_key"]) == public_key(
        rsa_key
    )
    assert "PRIVATE" not in json.dumps(commands)
    assert commands[1]["command_name"] == "list_authorized_clients_request"


async def test_paired_gateway_needs_no_customer_login(aiohttp_server, rsa_key):
    from custom_components.powerwall_force.client import LocalPowerwall
    from custom_components.powerwall_force.protocol import blob

    seen = []

    async def signed(request):
        assert "Authorization" not in request.headers
        seen.append(request.path)
        config = blob(1, blob(100, b"{}"))
        return web.Response(body=blob(10, blob(15, blob(2, config))))

    app = web.Application()
    app.router.add_post("/tedapi/v1r", signed)
    server = await aiohttp_server(app)
    async with ClientSession() as session:
        client = LocalPowerwall(session, "gateway", None, rsa_key, din="DIN")
        client._base = str(server.make_url("")).rstrip("/")
        assert await client.async_connect() == "DIN"
    assert seen == ["/tedapi/v1r"]
