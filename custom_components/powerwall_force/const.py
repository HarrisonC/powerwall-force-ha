"""Constants for the local dispatch integration."""

DOMAIN = "powerwall_force"
NAME = "Powerwall Force"
CONF_PRIVATE_KEY_PATH = "private_key_path"
CONF_GATEWAY_DIN = "gateway_din"
CONF_MAX_CHARGE_POWER = "max_charge_power_w"
CONF_MAX_DISCHARGE_POWER = "max_discharge_power_w"
CONF_MANUAL_DURATION = "manual_duration_minutes"
CONF_MANUAL_CHARGE_POWER = "manual_charge_power_w"
CONF_MANUAL_DISCHARGE_POWER = "manual_discharge_power_w"
DEFAULT_MANUAL_DURATION = 30
LEASE_SECONDS = 30
RENEW_SECONDS = 15
RENEW_TIMEOUT = 12
MAX_DURATION_SECONDS = 86400
REQUEST_TIMEOUT = 8
CONF_POLL_INTERVAL = "poll_interval_seconds"
DEFAULT_POLL_INTERVAL = 2
MIN_POLL_INTERVAL = 1
MAX_POLL_INTERVAL = 300
TELEMETRY_TIMEOUT = 4
TELEMETRY_MAX_AGE = 30
