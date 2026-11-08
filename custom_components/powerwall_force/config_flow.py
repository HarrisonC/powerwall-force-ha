"""Guided Tesla authorization and physical pairing, plus existing-key import."""

from __future__ import annotations

import asyncio
import logging
import time
from copy import copy

import aiohttp
import voluptuous as vol
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntry, ConfigEntryState, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow as oauth
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import _power
from .application_credentials import TeslaAuthorization
from .client import LocalPowerwall, load_private_key, normalize_host
from .const import (
    CONF_GATEWAY_DIN,
    CONF_MAX_CHARGE_POWER,
    CONF_MAX_DISCHARGE_POWER,
    CONF_POLL_INTERVAL,
    CONF_PRIVATE_KEY_PATH,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    NAME,
)
from .pairing import FLEET_BASES, EnergySite, FleetPairing, prepare_key, public_key
from .polling import poll_interval_seconds, validate_poll_interval
from .protocol import AuthenticationError, PowerwallError

_LOGGER = logging.getLogger(__name__)
PAIRING_WINDOW = 600
PAIRING_POLL = 4


def _schema(defaults: dict) -> vol.Schema:
    schema = {
        vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): str,
        vol.Required(
            CONF_PRIVATE_KEY_PATH,
            default=defaults.get(
                CONF_PRIVATE_KEY_PATH, "/config/powerwall_force/tedapi_rsa_private.pem"
            ),
        ): str,
        vol.Required(
            CONF_MAX_CHARGE_POWER,
            **(
                {"default": defaults[CONF_MAX_CHARGE_POWER]}
                if CONF_MAX_CHARGE_POWER in defaults
                else {}
            ),
        ): NumberSelector(
            NumberSelectorConfig(min=1, mode=NumberSelectorMode.BOX, unit_of_measurement="W")
        ),
        vol.Required(
            CONF_MAX_DISCHARGE_POWER,
            **(
                {"default": defaults[CONF_MAX_DISCHARGE_POWER]}
                if CONF_MAX_DISCHARGE_POWER in defaults
                else {}
            ),
        ): NumberSelector(
            NumberSelectorConfig(min=1, mode=NumberSelectorMode.BOX, unit_of_measurement="W")
        ),
    }
    if CONF_GATEWAY_DIN not in defaults:
        schema[vol.Required(CONF_PASSWORD)] = TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        )
    return vol.Schema(schema)


class PowerwallForceConfigFlow(oauth.AbstractOAuth2FlowHandler, domain=DOMAIN):
    VERSION = 1
    DOMAIN = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PowerwallOptionsFlow:
        return PowerwallOptionsFlow()

    def __init__(self) -> None:
        super().__init__()
        self._local_data: dict = {}
        self._fleet: FleetPairing | None = None
        self._sites: list[EnergySite] = []
        self._site: EnergySite | None = None
        self._key = None
        self._key_path: str | None = None
        self._base = FLEET_BASES["Europe"]
        self._registered_at = 0.0
        self._verification_task: asyncio.Task | None = None
        self._pairing_error = "pairing_failed"

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    async def async_generate_authorize_url(self) -> str:
        if isinstance(self.flow_impl, TeslaAuthorization):
            # Application implementations are shared by concurrent wizards.
            # Give this flow its own audience without changing another flow.
            self.flow_impl = copy(self.flow_impl)
            self.flow_impl.audience = self._base
        return await super().async_generate_authorize_url()

    @callback
    def async_remove(self) -> None:
        # The flow manager cancels the progress task before removing this flow.
        self._fleet = None
        self.external_data = None
        self._key = None

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        return self.async_show_menu(step_id="user", menu_options=["pair_gateway", "existing_key"])

    async def async_step_pair_gateway(self, user_input: dict | None = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                host = normalize_host(user_input[CONF_HOST])
            except ValueError:
                errors[CONF_HOST] = "invalid_host"
            for field in (CONF_MAX_CHARGE_POWER, CONF_MAX_DISCHARGE_POWER):
                try:
                    _power(user_input[field])
                except vol.Invalid:
                    errors[field] = "invalid_power"
            if not errors:
                self._local_data = {
                    CONF_HOST: host,
                    CONF_MAX_CHARGE_POWER: _power(user_input[CONF_MAX_CHARGE_POWER]),
                    CONF_MAX_DISCHARGE_POWER: _power(user_input[CONF_MAX_DISCHARGE_POWER]),
                }
                self._base = FLEET_BASES[user_input["region"]]
                return await self.async_step_authorization()
        return self.async_show_form(
            step_id="pair_gateway",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required("region", default="Europe"): vol.In(list(FLEET_BASES)),
                    vol.Required(CONF_MAX_CHARGE_POWER): NumberSelector(
                        NumberSelectorConfig(
                            min=1, mode=NumberSelectorMode.BOX, unit_of_measurement="W"
                        )
                    ),
                    vol.Required(CONF_MAX_DISCHARGE_POWER): NumberSelector(
                        NumberSelectorConfig(
                            min=1, mode=NumberSelectorMode.BOX, unit_of_measurement="W"
                        )
                    ),
                }
            ),
        )

    async def async_step_authorization(self, user_input: dict | None = None) -> FlowResult:
        methods = ["tesla_login"]
        if self._fleet_entries():
            methods.insert(0, "tesla_fleet")
        return self.async_show_menu(step_id="authorization", menu_options=methods)

    def _fleet_entries(self) -> dict:
        return {
            entry.entry_id: entry
            for entry in self.hass.config_entries.async_entries("tesla_fleet")
            if entry.state is ConfigEntryState.LOADED
        }

    async def async_step_tesla_fleet(self, user_input: dict | None = None) -> FlowResult:
        entries = self._fleet_entries()
        errors = {}
        if user_input is not None:
            entry = entries.get(user_input["entry_id"])
            if entry is None:
                errors["base"] = "invalid_auth"
            else:
                try:
                    implementation = await oauth.async_get_config_entry_implementation(
                        self.hass, entry
                    )
                    session = oauth.OAuth2Session(self.hass, entry, implementation)
                    await session.async_ensure_token_valid()
                    return await self._use_token(session.token["access_token"])
                except (aiohttp.ClientError, PowerwallError, ValueError, KeyError):
                    errors["base"] = "cloud_unavailable"
        return self.async_show_form(
            step_id="tesla_fleet",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("entry_id"): vol.In(
                        {key: entry.title for key, entry in entries.items()}
                    ),
                }
            ),
        )

    async def async_step_tesla_login(self, user_input: dict | None = None) -> FlowResult:
        if await oauth.async_get_implementations(self.hass, DOMAIN):
            return await self.async_step_pick_implementation()
        return await self.async_step_application_credentials()

    async def async_step_application_credentials(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            await async_import_client_credential(
                self.hass,
                DOMAIN,
                ClientCredential(user_input["client_id"].strip(), user_input["client_secret"]),
            )
            return await self.async_step_pick_implementation()
        return self.async_show_form(
            step_id="application_credentials",
            data_schema=vol.Schema(
                {
                    vol.Required("client_id"): str,
                    vol.Required("client_secret"): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    ),
                }
            ),
        )

    async def async_oauth_create_entry(self, data: dict) -> FlowResult:
        # Do not let the OAuth base class persist account tokens into our entry.
        self.external_data = None
        return await self._use_token(data["token"]["access_token"])

    async def _use_token(self, token: str) -> FlowResult:
        self._fleet = FleetPairing(async_get_clientsession(self.hass), self._base, token)
        try:
            self._sites = await self._fleet.async_sites()
        except AuthenticationError:
            self._fleet = None
            return self.async_abort(reason="cloud_auth_failed")
        except PowerwallError:
            self._fleet = None
            return self.async_abort(reason="cloud_unavailable")
        if not self._sites:
            return self.async_abort(reason="no_energy_sites")
        return await self.async_step_select_site()

    async def async_step_select_site(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            self._site = next(
                (site for site in self._sites if site.site_id == user_input["site_id"]), None
            )
            if self._site is None:
                return self.async_abort(reason="no_energy_sites")
            await self.async_set_unique_id(self._site.din)
            self._abort_if_unique_id_configured()
            return await self.async_step_register_key()
        return self.async_show_form(
            step_id="select_site",
            data_schema=vol.Schema(
                {
                    vol.Required("site_id", default=self._sites[0].site_id): vol.In(
                        {site.site_id: f"{site.name} ({site.din})" for site in self._sites}
                    ),
                }
            ),
        )

    async def async_step_register_key(self, user_input: dict | None = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                self._key_path, self._key = await self.hass.async_add_executor_job(
                    prepare_key, self.hass.config.path(".storage", DOMAIN), self._site.din
                )
                await self._fleet.async_register(self._site, public_key(self._key))
            except AuthenticationError:
                return self.async_abort(reason="cloud_auth_failed")
            except (PowerwallError, OSError, ValueError):
                errors["base"] = "registration_failed"
            else:
                self._registered_at = time.monotonic()
                return await self.async_step_isolator()
        return self.async_show_form(
            step_id="register_key",
            errors=errors,
            description_placeholders={
                "name": self._site.name,
                "din": self._site.din,
            },
        )

    async def async_step_isolator(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_verify_pairing()
        return self.async_show_form(
            step_id="isolator",
            description_placeholders={
                "name": self._site.name,
                "din": self._site.din,
            },
        )

    async def _wait_for_gateway(self) -> str | None:
        remaining = PAIRING_WINDOW - (time.monotonic() - self._registered_at)
        if remaining <= 0:
            return "pairing_timeout"
        client = LocalPowerwall(
            async_get_clientsession(self.hass),
            self._local_data[CONF_HOST],
            None,
            self._key,
            din=self._site.din,
        )
        try:
            async with asyncio.timeout(remaining):
                while True:
                    state = await self._fleet.async_key_state(self._site, public_key(self._key))
                    if state == 2:
                        return "pairing_timeout"
                    try:
                        await client.async_connect()
                        return None  # A signed local read proves this gateway accepts our key.
                    except PowerwallError:
                        pass
                    await asyncio.sleep(PAIRING_POLL)
        except AuthenticationError:
            return "cloud_auth_failed"
        except TimeoutError:
            return "pairing_timeout"
        except PowerwallError:
            return "cloud_unavailable"

    async def async_step_verify_pairing(self, user_input: dict | None = None) -> FlowResult:
        if self._verification_task is None:
            self._verification_task = self.hass.async_create_task(
                self._wait_for_gateway(), "Verify local Powerwall pairing"
            )
        if not self._verification_task.done():
            return self.async_show_progress(
                step_id="verify_pairing",
                progress_action="verify_pairing",
                progress_task=self._verification_task,
            )
        error = self._verification_task.result()
        self._verification_task = None
        if error:
            self._pairing_error = error
            return self.async_show_progress_done(next_step_id="pairing_retry")
        return self.async_show_progress_done(next_step_id="finish_pairing")

    async def async_step_pairing_retry(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            if self._pairing_error in ("cloud_auth_failed", "cloud_unavailable"):
                self._fleet = None
                return await self.async_step_authorization()
            return await self.async_step_register_key()
        return self.async_show_form(step_id="pairing_retry", errors={"base": self._pairing_error})

    async def async_step_finish_pairing(self, user_input: dict | None = None) -> FlowResult:
        data = {
            **self._local_data,
            CONF_PRIVATE_KEY_PATH: self._key_path,
            CONF_GATEWAY_DIN: self._site.din,
        }
        self._fleet = None
        return self.async_create_entry(title=NAME, data=data)

    async def _validate(self, data: dict) -> tuple[str | None, dict]:
        for field in (CONF_MAX_CHARGE_POWER, CONF_MAX_DISCHARGE_POWER):
            try:
                data[field] = _power(data[field])
            except vol.Invalid:
                return None, {field: "invalid_power"}
        try:
            data[CONF_HOST] = normalize_host(data[CONF_HOST])
        except ValueError:
            return None, {CONF_HOST: "invalid_host"}
        try:
            key = await self.hass.async_add_executor_job(
                load_private_key, data[CONF_PRIVATE_KEY_PATH]
            )
        except (OSError, ValueError, TypeError):
            return None, {CONF_PRIVATE_KEY_PATH: "invalid_key"}
        client = LocalPowerwall(
            async_get_clientsession(self.hass),
            data[CONF_HOST],
            data.get(CONF_PASSWORD),
            key,
            din=data.get(CONF_GATEWAY_DIN),
        )
        try:
            return await client.async_connect(), {}
        except AuthenticationError:
            return None, {"base": "invalid_auth"}
        except PowerwallError:
            return None, {"base": "cannot_connect"}

    async def async_step_existing_key(self, user_input: dict | None = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            din, errors = await self._validate(user_input)
            if not errors:
                await self.async_set_unique_id(din)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=NAME, data=user_input)
        return self.async_show_form(
            step_id="existing_key", data_schema=_schema(user_input or {}), errors=errors
        )

    async def async_step_reconfigure(self, user_input: dict | None = None) -> FlowResult:
        entry = self._get_reconfigure_entry()
        return await self._update(entry, "reconfigure", user_input)

    async def async_step_reauth(self, entry_data: dict) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict | None = None) -> FlowResult:
        return await self._update(self._get_reauth_entry(), "reauth_confirm", user_input)

    async def _update(self, entry, step_id: str, user_input: dict | None) -> FlowResult:
        errors = {}
        if user_input is not None:
            user_input = {**entry.data, **user_input}
            din, errors = await self._validate(user_input)
            if not errors:
                if din != entry.unique_id:
                    return self.async_abort(reason="wrong_gateway")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                    reason="reauth_successful"
                    if step_id == "reauth_confirm"
                    else "reconfigure_successful",
                )
        return self.async_show_form(
            step_id=step_id, data_schema=_schema(user_input or dict(entry.data)), errors=errors
        )


class PowerwallOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors = {}
        if user_input is not None:
            try:
                interval = validate_poll_interval(user_input.get(CONF_POLL_INTERVAL))
            except vol.Invalid:
                errors[CONF_POLL_INTERVAL] = "invalid_poll_interval"
            else:
                # Manual button preferences share options; retain their current values.
                return self.async_create_entry(
                    title="",
                    data={**self.config_entry.options, CONF_POLL_INTERVAL: interval},
                )
        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL,
                        default=poll_interval_seconds(self.config_entry.options),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_POLL_INTERVAL,
                            max=MAX_POLL_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    )
                }
            ),
        )
