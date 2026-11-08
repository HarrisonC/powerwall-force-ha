# Local Powerwall 3 telemetry research

Checked 2026-11-06 against the repositories linked by the user. This is a
source review, not a read of the user's gateway. Version 0.3.0's charge,
discharge and manual controls have been reported working by the user; telemetry
has not been added to that version.

Implementation follow-up: v0.4.0 adds the core status-query readings described
below, using the existing paired connection. Optional component diagnostics
and native cumulative counters remain research for subsequent versions.
Version 0.5.0 makes the core-read interval configurable from 1 to 300 seconds,
with a 2-second default.

## What the repositories establish

- [pyPowerwall](https://github.com/jasonacox/pypowerwall#v1r-feature-parity)
  implements RSA-signed LAN TEDAPI reads for Powerwall 3, including live status,
  configuration, firmware and component telemetry. Its implementation is the
  most detailed reference for the additional PW3 signals below.
- [PowerSync's local client](https://github.com/bolagnaise/PowerSync/blob/main/custom_components/power_sync/powerwall_local/client.py#L214)
  sends DeviceControllerQuery and PW3 component queries directly to
  `/tedapi/v1r`. Its normal local snapshot combines live status with a signed
  read of `config.json`. The same client also contains a separate Fleet API
  relay method: a reading exposed by PowerSync is not necessarily local merely
  because that integration supports local control.
- [Matthew1471's wrapper documentation](https://github.com/Matthew1471/Tesla-API/blob/main/Documentation/Wrappers/Python/README.adoc)
  and [local console example](https://github.com/Matthew1471/Tesla-API/blob/main/Python/examples/gateway_console.py)
  demonstrate the customer-login JSON API. They are useful background, but the
  example's longer endpoint list is not evidence that every endpoint works on
  current PW3 firmware.

These are structured API reads; scraping a web page is unnecessary.

## Core readings over the existing paired connection

The [local snapshot implementation](https://github.com/bolagnaise/PowerSync/blob/main/custom_components/power_sync/powerwall_local/client.py#L1006)
maps the following DeviceControllerQuery data. This query is sent over the same
RSA transport as our dispatch requests; it does not require continuous Tesla
cloud access.

| Data | Source and units | Interpretation |
| --- | --- | --- |
| Grid power | Site meter aggregate, W | Positive imports, negative exports. Separate import/export sensors can be calculated from it. |
| Battery power | Battery meter aggregate, W | Positive discharges, negative charges. This is measured power, separate from our requested dispatch setpoint. |
| Solar generation | Solar meter aggregate, W | Site aggregate; depends on installed metering. |
| Home consumption | Load meter aggregate, W | The gateway's reported aggregate load. |
| Remaining battery energy | System status, Wh | System aggregate, not a promise that all of it is available for discharge. |
| Reported full-pack energy | System status, Wh | Reported battery capacity; distinct from an installation's nameplate capacity. |
| Battery percentage | Calculated from remaining/full-pack energy | Raw percentage and an app-style usable percentage may differ because of reserved energy. |
| Grid connection / islanding | Contactor, utility-grid and microgrid status | Utility grid health alone does not prove the site is connected. |
| Active alerts and shutdown reasons | Control status | Lists and flags, with missing data kept distinct from an empty alert list. |
| Battery and inverter identities / disable reasons | Battery blocks and PV inverter lists | Useful for topology, counts and diagnostics. |

The signed configuration read can additionally provide operation mode, site
metadata, configured ratings, grid-charging permission and export policy. These
are settings, not live electrical measurements. PowerSync deliberately does
not publish an inferred app-facing backup-reserve percentage from the raw
configuration because the hidden reserve makes that conversion uncertain.

## Additional component and meter readings

[pyPowerwall's PW3 vitals implementation](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/__init__.py#L1302)
and [query variables and validation notes](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/queries/__init__.py#L48)
support the following. Availability depends on firmware, connected equipment
and which device the query actually reaches.

| Group | Locally exposed information | Limits |
| --- | --- | --- |
| Solar inputs | PW3 input A-F voltage, current and operating/connection state | Input power is calculated as voltage times current. Unused inputs and parallel strings need appropriate interpretation. |
| AC electrical data | Inverter voltage and frequency; meter/CT voltage, current, real and reactive power | Available phases and fields depend on the gateway and meter hardware. Some normalized library currents are calculated, rather than direct readings. |
| Temperatures | Inverter ambient; pack minimum/maximum and shunt temperature | Use proven live signals. Upstream found the PW3 heatsink field constant on tested firmware. |
| Cooling | Two inverter fan RPM readings and duty percentages | Upstream verified these on PW3; other fan names can be present but null. |
| Battery diagnostics | Per-pack nominal energy; lifetime discharged kWh/Ah, capacity in Ah, self-discharge, cycle and event counters | These are raw diagnostics, not a verified universal state-of-health percentage. A cycle counter was zero on upstream's tested units. |
| Safety / battery assembly | Battery connection counts and states, high-voltage disconnect and rapid-shutdown state, component alerts | Available diagnostic fields depend on the installed components. |
| Device information | Firmware, part/serial identifiers, update state/progress | Current firmware can be read through the system-info path; an available-update version is a different value. |
| Gateway uptime | Sync-board uptime and a calculated boot time | Requires a reporting sync board and a usable gateway timestamp. |

Two implementation details matter:

1. [pyPowerwall's query sets](https://github.com/jasonacox/pypowerwall/tree/main/pypowerwall/tedapi/queries)
   include Tesla-signed query text. The older set allows supported signal names
   to be added through variables; the newer set embeds names in the signed
   text. Arbitrarily rewriting a query is not equivalent to adding a field to
   an ordinary JSON request. Newer does not necessarily expose more readings.
2. [Follower handling](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/__init__.py#L1403)
   in pyPowerwall uses the gateway Wi-Fi path for separate follower queries.
   Site aggregate data does not establish that every follower inverter's
   individual telemetry is reachable over the leader's LAN endpoint. PW3 DC
   expansions can have a different reporting topology again.

## Cumulative energy and the separate local login

[pyPowerwall explicitly merges native meter counters](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/pypowerwall_tedapi.py#L512)
from `GET /api/meters/aggregates`. It uses the customer Bearer token obtained
through `POST /api/login/Basic`; this is additional local authentication, not
the RSA pairing or a Tesla cloud token. Its native-read path requires a customer
password, normally the last five characters of the correct gateway/PW3 label
password.

The endpoint can report cumulative imported/exported energy per meter section,
where supported. Actual counters can feed Home Assistant's Energy dashboard
and utility meters after validating units, direction and reset behavior. Daily
totals are then calculated from those counters. If counters are unavailable,
energy can instead be estimated by integrating power, with gaps and sampling
accuracy taken into account. Unsupported or absent counters must not become
synthetic zero totals.

The other basic JSON reads documented for PW3 are `/api/system_status/soe` and
`/api/system_status/grid_status`. Much of the older gateway JSON API can return
404 on PW3. The [PW3 access documentation](https://github.com/jasonacox/pypowerwall#basic-lan-access-no-rsa-key-required)
describes this smaller endpoint set.

## Suggested lightweight extension

This is an implementation recommendation inferred from the source review:

- Reuse our existing session, gateway address and paired private key for the
  signed DeviceControllerQuery. Share one response across all core entities.
- Start with a configurable 10-second interval for power, energy, percentage
  and grid-state readings. This is a proposed starting point, not a proven
  minimum or Tesla-supported polling rate.
- Fetch optional component diagnostics less often, for example every 60
  seconds; refresh static metadata and configuration more slowly. Avoid
  fetching the entire configuration for each individual sensor.
- Keep diagnostics optional and retain missing/null values as unavailable.
  Check component timestamps, since a successful HTTP response can contain
  stale signals.
- Keep reads bounded and ensure polling cannot delay the existing dispatch
  renewals. Telemetry errors should affect sensor availability independently
  of command acknowledgement/status.
- The existing wire helpers can be extended for the small query envelope and
  JSON response. A full pyPowerwall install, generated protobuf runtime, proxy,
  MQTT broker or new hosted service should not be necessary.
- Add customer-login credentials only if native cumulative counters are
  wanted and require that route on the user's firmware. Keep the paired
  core-read path usable without that extra login.

A useful first set would be battery percentage, battery remaining energy,
battery power, solar power, home power, grid import/export and grid-connected
status. Optional temperatures, strings, fans and diagnostic entities can follow.

## Interpretation limits

- A reported full-pack capacity can support a labeled capacity estimate, but
  does not by itself establish an authoritative battery health percentage.
- Raw SoC/reserve values need a documented basis; do not hard-code one site's
  hidden-reserve conversion as universal.
- [pyPowerwall's normalized maximum-power fields](https://github.com/jasonacox/pypowerwall/blob/main/pypowerwall/tedapi/pypowerwall_tedapi.py#L939)
  can be derived from nameplate data. Its live POD available-power signals are
  documented for PW2. These do not establish a reliable maximum accepted PW3
  dispatch wattage, so the integration's tested configured limit should remain.
- Historical cloud data, tariffs and forecasts shown by a larger integration
  are not established by these local telemetry reads.

The source-review pass only added this research note. See README.md for the
subsequent v0.4.0 implementation and its current sensor set.
