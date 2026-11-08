# Protocol and implementation notes

Research checked 2026-11-06. This implementation targets RSA local access on
Powerwall 3, the user's hardware. "Latest firmware" does not identify a single
version across staged Tesla rollouts. The user confirmed v0.3.0 works on their
unit. The v0.4.0 telemetry extension still needs on-device verification. Setup
intentionally performs no dispatch as a capability probe.

## Primary sources

- [pyPowerwall LAN control and dispatch behavior](https://github.com/jasonacox/pypowerwall#lan-control-v1r)
- [pyPowerwall RSA transport](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/tedapi_v1r.py)
- [pyPowerwall RSA registration utility](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/v1r_register.py)
- [pyPowerwall combined protobuf schema](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/protobuf/V2024_06/tedapi_combined.proto)
- [Matthew1471 Tesla-API research](https://github.com/Matthew1471/Tesla-API)
- [Home Assistant config flows](https://contributors.home-assistant.io/core/integrations/config-flow/)
- [Home Assistant action registration](https://contributors.home-assistant.io/core/integrations/actions/)
- [PowerSync pairing flow](https://github.com/bolagnaise/PowerSync/blob/main/custom_components/power_sync/powerwall_local/pairing.py)
- [Tesla authorization-code flow](https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens)

PowerSync was inspected to understand the onboarding sequence and the distinction
between cloud verification and local key acceptance. No PowerSync source code is
included here. Its hosted sign-in service is not used. This integration uses
Home Assistant's OAuth framework with user-supplied Tesla application credentials,
or reuses an existing Tesla Fleet authorization, for the one-time pairing API.

The guided wizard creates a private RSA key file before attempting cloud
registration. Retrying or cancelling preserves that key; re-registration does
not require a new key. The user explicitly selects the account's energy site
before any registration is sent. Verification matches the exact public key
and requires a successful signed local read after the physical confirmation
step, even when the cloud already reports verified. Cancelling the flow cancels
its progress task and releases the flow's cloud token reference.

Matthew1471's repository provides background on local Gateway APIs and signing.
No GPL-licensed Python code from that project is included. The implemented
dispatch path follows pyPowerwall's MIT-licensed transport and schema; its
license is retained in `THIRD_PARTY_NOTICES.md`. The upstream schema is kept
under `tests/fixtures` for independent wire compatibility checks, outside the
runtime package.

## Requests

1. Guided pairing obtains the full gateway DIN from the selected account energy
   site. This avoids a customer password or local bearer session entirely.
   Existing-key imports retain the legacy `POST /api/login/Basic` and
   `GET /tedapi/din` identity discovery route for backwards compatibility.
2. The DIN personalizes the RSA signature.
3. A signed, read-only filestore request for `config.json` verifies authorization.
   Configuration is inspected only to establish a valid response and discarded.
4. `POST /tedapi/v1r` carries signed dispatch or resume requests. A 401/403
   triggers one login and a fresh signature only for legacy imports; guided
   RSA-only entries surface the rejection directly. Connection failures do not
   trigger repeated dispatch retries.

Start and release have one eight-second deadline covering the complete request.
Renewal starts 15 seconds after the previous request started and has up to 12
seconds to complete. The deadline includes any signing or bearer login and is
capped at the previous lease's earliest possible expiry and the session end.
Timing from request start accounts for time spent waiting for acknowledgement.
A renewal failure ends the session and triggers a release attempt.

The `RoutableMessage` uses domain 7, inner message field 10, signature field 13,
and UUID field 51. The RSA metadata covers signature type, energy-device domain,
gateway DIN, a 12-second expiry, and the inner message bytes. RSA PKCS1v15 with
SHA512 signs this TLV payload. Public identity uses DER PKCS1, not SPKI.

The inner `MessageEnvelope` uses Hermes delivery channel 2, authorized client 1,
and the gateway DIN recipient. Battery dispatch is TEG field 37; acknowledgment
is TEG field 38. Real power is request field 2, with a double wattage at field 1
and a uint32 duration at field 2. Negative watts charge; positive watts discharge.
Resume is an empty message at request field 1. Exactly one request variant is sent.

Success requires an explicit dispatch response. HTTP 200 alone is insufficient:
the transport checks message faults and `common.error_response`, including
firmware's unsupported-command status 12. This is an acceptance acknowledgement,
not telemetry. Replies are not cryptographically verified, matching the upstream
transport; gateway HTTPS uses its self-signed certificate without validation.

## Scope and resource use

No operating settings are changed, so there is no stored config snapshot to
restore or conflict with Tesla app updates. The dispatch lease delegates expiry
to the gateway. Controller operations are serialized to prevent a stale stop or
renewal from overriding a replacement session. Elapsed durations use monotonic
time; wall time is used only for signed request expiry and the displayed end time.

There is one connection session shared through Home Assistant, one task per
active dispatch, one status entity, three buttons, three number controls, nine
telemetry sensors and one grid-connected binary sensor. Manual controls share
the same device and call the existing action
handlers, so their validation, locking, and gateway expiry behavior match
automation calls. Manual settings are stored in config-entry options, separately
from the installation maxima that default automation requests use.

Status 11 rejections include the attempted wattage and its source. The decoder
retains the gateway's status message for diagnostics; the integration does not
infer a universal discharge limit from this code or silently retry at a lower
wattage. The 11,000 W user report is consistent with upstream's observed 10,000 W
dispatch limit, and remains distinct from an inverter's advertised rating.

Only the protobuf wire
types needed for these messages are implemented. The decoder rejects malformed
lengths, overlong varints, and unsupported wire types. The runtime does not
import `pypowerwall` or generated protobuf code.

Automatic maximum-power discovery is deliberately absent: we found no reliable
universal maximum sentinel for this dispatch command. Installation-specific
limits are configured explicitly and checked before sending a request. Request
power is a setpoint, not a guaranteed measured upper bound.

## Shared local telemetry (v0.4.0)

The runtime retains pyPowerwall's verbatim Tesla-signed
`device_controller_basic` query from
[V2024_06.json](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/queries/V2024_06.json).
Its query text and ECDSA code are wrapped in the old QueryType wire format,
then signed with the existing RSA key and sent to the same `/tedapi/v1r` path.
The query's delivery channel is 1 and sender is local participant 1; these
inner fields differ from a dispatch envelope. Query send is field 16/1, with
query number 2, the signed text, code and empty variables object. Response JSON
is read from inner envelope field 16/2/2, after the usual message-fault and
common-error checks. No raw/native JSON API is polled and no new login is added.

One Home Assistant DataUpdateCoordinator refreshes every 2 seconds by default and shares
the response among all enabled telemetry entities. With no listeners it stops
scheduled polling. Core readings are site, battery, solar and load real power,
remaining/full-pack energy and physical grid connection readback. Grid import
and export are derived from signed site power. Percentage is the raw energy
ratio, and energy is converted from Wh to kWh. Missing, nonfinite, boolean or
ambiguous duplicate numeric readings are unavailable rather than zero. Real
zero readings remain valid. The percentage applies no inferred reserve scaling.

Reads retry once after a timeout, with a four-second deadline for each attempt.
A read defers if a command holds the client lock. A command cancels an in-flight
read or retry before taking that lock.
Caller cancellation still propagates, so unloading can terminate reads. A
preempted read retains the previous sample and checks its age limit
on updates. A failed read makes telemetry unavailable independently of command
status; it does not end a dispatch or block the Controls card from loading.
Shutdown stops scheduling and awaits read cancellation before controller cleanup.

The device's six button/number entities have no entity category, keeping them
in the native Controls card. Measurement sensor/binary-sensor entities appear
in Sensors, and reported capacity uses Diagnostic. Home Assistant's
[device-category implementation](https://github.com/home-assistant/frontend/blob/dev/src/data/device/device_entity_category.ts)
orders Controls before Sensors and Diagnostic; the integration uses this
supported grouping, with no frontend patch or custom panel.

Independent tests compile the retained upstream `tedapi.proto`, compare query
bytes against the captured query/signature, and generate response messages.
The runtime ships neither test schemas nor generated protobuf modules.

## Configurable polling (v0.5.0)

The Configure options flow stores `poll_interval_seconds` alongside the existing
manual button preferences, retaining those preferences on save. Whole seconds
from 1 to 300 are accepted. Entries with no saved interval use 2 seconds, so the
default also applies when upgrading from v0.4.0. Invalid stored interval values
fall back to that default.

An options update listener changes the running coordinator's interval and asks
Home Assistant to refresh/reschedule it. There is no config-entry reload and no
dispatch stop or replacement. Saving a manual button preference with the same
interval does not trigger an extra read. Changing the interval with no telemetry
listeners does not start polling. The retained-sample age limit is the longer
of 30 seconds and three configured intervals, checked on updates, so a slow
interval does not make otherwise healthy readings unavailable between polls.

