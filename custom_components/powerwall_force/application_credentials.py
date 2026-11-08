"""Tesla-owned sign-in using the user's registered OAuth application."""

from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant

AUTHORIZE_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"


class TeslaAuthorization(AuthImplementation):
    audience = "https://fleet-api.prd.na.vn.cloud.tesla.com"

    @property
    def extra_authorize_data(self) -> dict:
        return {
            "scope": "openid energy_device_data energy_cmds",
            "prompt": "login",
            "require_requested_scopes": "true",
        }

    @property
    def extra_token_resolve_data(self) -> dict:
        return {"audience": self.audience}


async def async_get_auth_implementation(
    hass: HomeAssistant,
    auth_domain: str,
    credential: ClientCredential,
) -> TeslaAuthorization:
    return TeslaAuthorization(
        hass, auth_domain, credential, AuthorizationServer(AUTHORIZE_URL, TOKEN_URL)
    )
