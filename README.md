# Powerwall Force

A small Home Assistant custom integration for **Powerwall 3 local telemetry and
timed battery dispatch** using a paired RSA-4096 key and `/tedapi/v1r` on the home LAN.

Automations can start charging or discharging for 1 second to 24 hours, choose
a battery power setpoint in watts, use the configured maximum, and stop early.
A new command replaces the previous session. The Powerwall returns to its
configured operating mode when the session ends.

The runtime uses only Home Assistant's existing `aiohttp` and `cryptography`
packages. There is no add-on, proxy, MQTT broker, cloud polling, protobuf runtime,
or additional pip requirement. All live readings share one local query every
2 seconds by default. Three manual buttons, three manual settings and command status
remain event-driven.

**Status: experimental.** The user confirmed version 0.3.0's local charge,
discharge and manual controls work on Powerwall 3. Version 0.4.0 adds telemetry,
which still needs on-device confirmation. Automated tests use a simulated
gateway and real Home Assistant Core. Accepted power limits vary by firmware.
Successful setup verifies authorization with a read-only request; it does not
establish that the firmware supports battery dispatch.

## What the power setting means

`power_w` is a requested **battery** charge or discharge power, not a site import
or export target and not a measured hard power cap. Solar, house demand, battery
state, temperature, firmware, and the inverter's own limits affect the result.
Use the new **Battery power** sensor to observe actual battery power, and
**Grid power** to observe net site import/export.

Enter separate charge and discharge maxima during setup, using limits accepted
by your installation. Omitting `power_w` uses that configured maximum. Values
above it are rejected. There is no documented universal "maximum" command, so
this integration does not guess a rating or repeatedly probe larger setpoints.
For context, upstream testing reports rejection above 10,000 W discharge on
some PW3 firmware, even where the electrical rating is higher. Ratings and
accepted dispatch setpoints are distinct. Multi-unit aggregate behavior has not
been validated here; limits apply to the targeted gateway's dispatch command.

When a status **11** response identifies the exact rejected wattage and a lower
limit for that direction, the integration confirms resume and retries once at
that limit within the original session window. Later commands use the learned
charge or discharge ceiling until the integration reloads. `requested_power_w`
shows the requested setpoint and `dispatch_power_w` shows the setpoint sent to
the gateway after applying that ceiling. Other status 11 responses keep the
gateway's explanation in `last_error`. Set an accepted maximum under
**Reconfigure** if the gateway rejects your configured maximum without reporting
a usable limit.

## Direct dispatch behavior

This is an undocumented Tesla command. Upstream reports that it can bypass
backup reserve and grid export restrictions. Discharging may drain below your
chosen backup reserve and export even when normal export is disabled. Charging
can import from the grid. This integration does not enforce a reserve floor,
export agreement, tariff, or state-of-charge cutoff; automation duration and
requested wattage are its controls. Firmware and battery protections still
determine actual operation. Review whether this mode fits your installation and
Powerwall warranty before using it. See the
[upstream dispatch findings](https://github.com/jasonacox/pypowerwall#lan-control-v1r).

The integration never changes operating mode, reserve, export configuration, or
grid contactor state. A dispatch acknowledgement confirms command acceptance,
not actual power. No battery dispatch or grid-contactor commands have been sent
to real hardware while developing this project.

## Pair directly in Home Assistant

Choose **Pair a Powerwall** when adding the integration:

1. Enter the Powerwall's LAN IP address, Tesla account region, and accepted
   charge/discharge power limits.
2. Choose **Use my Tesla Fleet connection** if you already have that integration
   configured, or **Sign in with Tesla** with your own developer app credentials.
3. Select the correct energy site. The wizard displays the gateway identity so
   that accounts with multiple sites can choose explicitly.
4. Select Submit on **Register local control key**. Home Assistant generates and
   securely saves an RSA-4096 private key, then registers its public key with Tesla.
5. After registration, follow Tesla's instructions for your unit: turn the
   Powerwall DC isolator **OFF for 10 seconds**, then **ON**. Do this within the
   10-minute verification window. Select Submit once it is back on.
6. The wizard waits for the gateway to accept a **signed, read-only LAN request**.
   Cloud verification alone does not complete setup. No battery dispatch is sent.

The generated private key stays in `/config/.storage/powerwall_force/`, in a
file created with owner-only permissions. Only its path and gateway DIN are
saved in the integration entry. Failed or cancelled attempts retain the key
so that retrying registration reuses the same key. Protect that directory in
backups. Do not delete it while using the integration.

Guided pairing does not require a gateway QR password. After pairing, normal
startup, telemetry, charge, discharge, and stop use the key and DIN directly, with no
Tesla account login or cloud API calls. Cloud tokens are held only for the
setup flow and are not saved in this integration's config entry.

### Tesla sign-in prerequisite

PowerSync's sign-in uses PowerSync's hosted authorization service. This project
has no equivalent hosted service and does not use PowerSync's application ID or
account proxy. An independent Tesla OAuth login needs a registered developer
application, as described in
[Tesla's authorization documentation](https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens).

The easiest route when already configured is to reuse **Tesla Fleet** in Home
Assistant. The wizard can refresh that connection's token through Home
Assistant and use its energy permissions during pairing. Tesla Fleet does not
need to remain installed for this integration's subsequent local requests.

For the direct login option, complete Tesla's developer application approval
and registration requirements and enable `energy_device_data` and `energy_cmds`.
Register the exact Home Assistant OAuth callback URL. With the **My Home
Assistant** integration enabled, this is
`https://my.home-assistant.io/redirect/oauth`; otherwise use the callback URL for
your configured Home Assistant frontend. Enter the application's client ID and
client secret in the wizard. These are **developer application credentials**,
not your Tesla account password. Home Assistant's Application Credentials
component stores them centrally; the Tesla account password is entered only on
Tesla's website. The integration asks for energy scopes and does not request
vehicle access or a long-lived refresh token for its own login.

### Use an existing paired key

The **Use an existing paired key** option is retained. Place an existing,
unencrypted RSA-4096 PEM key on Home Assistant, for example at
`/config/powerwall_force/tedapi_rsa_private.pem`, and supply its path and the
customer password (the last five characters of the gateway QR password).
Restrict file permissions and keep a secure backup. File paths must be valid
inside the Home Assistant container, if applicable.

External registration with
[pyPowerwall](https://github.com/jasonacox/pypowerwall#v1r-lan-tedapi-setup---option-5)
remains available as an alternative. The public key must be registered and
verified for the same private key used locally.

## Install and configure

For HACS, open **Custom repositories** from the HACS menu. Add
`https://github.com/HarrisonC/powerwall-force-ha` as an **Integration**, download
the latest release of **Powerwall Force**, and restart Home Assistant.

For manual installation, follow these steps.

1. Copy `custom_components/powerwall_force` into
   `/config/custom_components/powerwall_force` on Home Assistant, then restart it.
   Alternatively, extract the release ZIP into `/config`; it contains that layout.
2. In **Settings → Devices & services → Add integration**, add **Powerwall Force**.
3. Choose **Pair a Powerwall** and complete the guided steps above, or choose
   **Use an existing paired key** to import one.
4. Setup verifies the key with a signed, read-only config request. It sends no
   battery dispatch command.

Home Assistant Core **2025.4 or later** is required. Reserve the gateway's IP
address in DHCP. This integration targets the LAN RSA endpoint on Powerwall 3;
it does not rely on routing through the gateway's Wi-Fi access point.

Use the integration's **Reconfigure** menu to change the address, password, key
path, or maxima. Reconfiguration reloads the entry and ends any active session.
Authentication failures offer a reauthentication form. A replacement gateway
must be added as a new integration because its identity differs.

The gateway has a self-signed HTTPS certificate, so certificate verification is
disabled only for requests to that configured gateway. Commands are RSA-signed
with a short signature lifetime. Legacy customer passwords are stored through normal Home
Assistant config entries; private keys stay in separate files. Developer app
credentials, when used, are stored by Home Assistant Application Credentials. Include
these in your normal Home Assistant backup protection.

## Upgrade an existing installation

Copy the updated `powerwall_force` folder into
`/config/custom_components/powerwall_force`, replacing the integration's code
files, and restart Home Assistant. The existing config entry and RSA key are
reused, including saved manual duration and power. The new telemetry entities
are added to the same device as the existing controls and status sensor. Pairing
does not need to be repeated.

## Manual controls on the device page

Open **Settings → Devices & services → Powerwall Force**, then open its device.
The **Controls** section contains **Force charge**, **Force discharge**, **Stop**
and the three manual inputs. It is ordered before **Sensors**, with diagnostic
capacity further down in **Diagnostic**. These entities can also be added to a
Home Assistant dashboard.

Set the three manual inputs before pressing a start button:

- **Manual duration**, in minutes, from 1 to 1,440; initially 30 minutes.
- **Manual charge power**, in watts; initially the configured charge maximum.
- **Manual discharge power**, in watts; initially the configured discharge maximum.

Changing these inputs saves preferences for the next button press. Pressing a
start button uses those values and replaces the current session; **Stop** resumes
the Powerwall's configured operating mode. Buttons use the same input validation,
short gateway leases, and failure handling as the automation actions.

Manual settings are stored in Home Assistant config-entry options and survive
reloads and restarts. When a setup maximum is reduced, the corresponding manual
setting is limited to the new maximum. For example, a saved 9,000 W manual value
with a newly configured 8,000 W maximum is displayed and used as 8,000 W.

Automation actions retain their own settings: omitting `power_w` uses the setup
maximum, rather than a saved manual button power. Automation duration remains
explicit and supports seconds as well as minutes and hours.

## Local telemetry

Version **0.4.0** adds the following through the **existing RSA connection**:

| Entity | Units / meaning |
| --- | --- |
| Battery level | Raw percentage: remaining energy divided by reported full-pack energy |
| Battery energy remaining | kWh of reported stored energy |
| Battery reported capacity | kWh of reported full-pack energy, in Diagnostic |
| Battery power | W; positive discharges, negative charges |
| Solar power | W of generation reported by the gateway |
| Home consumption | W of load reported by the gateway |
| Grid power | W; positive imports, negative exports |
| Grid import power | Positive import W, or zero when exporting |
| Grid export power | Positive export W, or zero when importing |
| Grid connected | On/off from physical contactor and grid/microgrid health readback |

One signed, read-only status query updates all these entities every **2 seconds**
by default; the interval is configurable.
It uses the same address, private key and DIN as dispatch, with no additional
password or cloud authorization. Existing-key imports retain their existing
local bearer authentication. No new native JSON endpoint is used.

Charge, discharge, renewal and stop commands preempt telemetry reads. Each read
has a four-second deadline and retries once after a timeout. A failed poll makes telemetry
unavailable while manual controls and actions remain usable. A deliberately
deferred reading becomes unavailable on the next update once it is older than
three configured intervals or 30 seconds, whichever is longer. Missing fields
remain unavailable. Disabling all
telemetry entities stops scheduled reads.

**Battery level** uses the raw energy ratio, before any hidden reserve adjustment;
it can differ from the Tesla app's usable percentage. Reported capacity is a
measurement, not a battery-health percentage. Remaining energy and capacity are
storage measurements, not cumulative counters for the Energy dashboard. This
version adds no native meter login, cumulative energy counters, component/fan/
temperature polling or inferred battery-reserve cutoff.

The **Dispatch status** sensor continues to report accepted command state and
requested power. Its value is separate from measured Battery power; telemetry
does not change the duration, wattage or configured maximum of a session.

### Change the polling interval

In **Settings → Devices & services → Powerwall Force**, select **Configure**.
Set **Polling interval (seconds)** to a whole number from **1 to 300** and submit.
The default is **2 seconds**, including existing entries upgraded to v0.5.0 that
have no saved interval. The option is stored per gateway and survives restarts.

Changes apply to the running telemetry coordinator without reloading the
integration or interrupting an active charge/discharge. Saved manual button
duration and powers are retained. The interval only controls telemetry reads;
dispatch leases continue to renew on their existing schedule.

## Automation actions

Charge at 2,500 W for 30 minutes:

```yaml
action: powerwall_force.charge
data:
  duration: "00:30:00"
  power_w: 2500
```

Discharge at the configured maximum for 15 minutes:

```yaml
action: powerwall_force.discharge
data:
  duration:
    minutes: 15
```

Stop and return to normal operation:

```yaml
action: powerwall_force.stop
```

Schedule charging through Home Assistant's existing automation triggers:

```yaml
alias: Powerwall overnight charging
triggers:
  - trigger: time
    at: "02:00:00"
actions:
  - action: powerwall_force.charge
    data:
      duration: "02:00:00"
      power_w: 2500
mode: single
```

Actions return after the gateway acknowledges the first command, rather than
blocking for the whole session. Durations also accept Home Assistant time-period
formats such as `{seconds: 45}`. Fractional remaining seconds are rounded up to
the gateway's one-second resolution.

With multiple configured gateways, supply `config_entry_id` on every action.
The action editor provides an integration selector. The dispatch status sensor
also exposes the ID as an attribute. Omitting it is allowed only when exactly
one Powerwall Force entry exists.

The sensor state is `idle`, `charge`, `discharge`, or `error`. Attributes include
the requested and dispatched watts (negative for charging), `ends_at` (UTC),
installation limits, `last_error`, `restore_status`, and `restore_error`.
An interrupted session retains `error` and its cause even when the gateway
acknowledges resume. `restore_status` is `pending` while resume is unconfirmed
and `confirmed` after acknowledgement. Battery power shows the measured result.

## Session expiry and restarts

The integration sends a gateway-side lease of at most **30 seconds**. It starts
renewal **15 seconds** after the preceding request started and waits up to
**12 seconds**, bounded by the previous lease and the requested session end.
The final renewal is shortened to the remaining duration. At the end, stop,
unload, or normal Home Assistant shutdown, it requests resumption of the
configured operating mode.

If Home Assistant crashes, loses network access, or renewal fails, the last
accepted lease expires on the gateway after at most 30 seconds from acceptance.
A timeout ends the session and triggers a resume request. Failed resume attempts
are retried up to twice, 10 seconds apart. New dispatches are blocked while
resume is pending. If retries are exhausted, **Stop** tries again. Before sending
power, the integration records that a resume may be needed. After a restart it
uses that record to resume normal operation without replaying the timed session.
This behavior depends on the gateway implementing its dispatch expiry as
documented upstream; hardware verification is still required.

Other applications can replace a gateway dispatch. These power readings do not
identify dispatch ownership, and the next renewal may replace that application's
command. Run one dispatch controller per gateway.

## Development

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check custom_components tests
python3 tools/package.py
```

The tests cover real RSA signatures, byte-for-byte compatibility with the
upstream generated protobuf schema, local HTTP authentication, read-only setup,
dispatch acknowledgements and errors, timing, replacement, and failure handling.
Pairing tests cover Tesla OAuth callbacks, reuse of Tesla Fleet authorization,
private-key permissions and reuse, explicit site selection, cancellation, and
local verification even when the cloud reports that the key is verified.
Manual-control tests operate Home Assistant's native button and number actions,
verify that all controls share the status sensor's device, check saved settings
across reloads, and reproduce rejection of an 11,000 W default discharge request.
Telemetry tests check the exact upstream signed query and independent generated
schema, RSA-only LAN requests, units and directions, missing/invalid/zero values,
grid contactor state, native entity grouping, read failure and recovery, polling
cleanup, and command preemption of slow reads.
Polling-option tests cover the 2-second upgrade default, validation, persistence,
live interval changes during charging, preserved manual settings, and sample age
at slower polling intervals.
Development dependencies are not shipped in the integration ZIP.

Protocol details and research sources are in [docs/protocol.md](docs/protocol.md).

