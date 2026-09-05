"""Config flow for the Aclara ACE integration."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AclaraAceClient, AclaraAceError, AuthError
from .const import (
    CONF_BILLING_PERIOD_DAYS,
    CONF_BILLING_START,
    CONF_CLIENT_ID,
    CONF_PRICE_PER_UNIT,
    CONF_TIERS,
    DEFAULT_BILLING_PERIOD_DAYS,
    DEFAULT_PRICE_PER_UNIT,
    DOMAIN,
)
from .tariff import TariffError, Tier, format_tiers, parse_tiers

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
        vol.Required(CONF_CLIENT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


async def _async_validate(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, str]:
    """Log in and return account details for the unique id / title."""
    client = AclaraAceClient(
        async_create_clientsession(hass),
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        client_id=int(data[CONF_CLIENT_ID]),
    )
    await client.async_login()
    account = await client.async_get_account()
    return {
        "account_id": account.account_id,
        "name": account.name,
        "meters": ", ".join(m.meter_id for m in account.meters),
    }


class AclaraAceConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for portal credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                info = await _async_validate(self.hass, user_input)
            except AuthError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, AclaraAceError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating ACE credentials")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(
                    f"{user_input[CONF_CLIENT_ID]}_{info['account_id']}".lower()
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"ACE {info['account_id']}",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_CLIENT_ID: int(user_input[CONF_CLIENT_ID]),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(STEP_USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication after a failed login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            try:
                await _async_validate(self.hass, data)
            except AuthError:
                errors["base"] = "invalid_auth"
            except (aiohttp.ClientError, TimeoutError, AclaraAceError):
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error validating ACE credentials")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            description_placeholders={CONF_USERNAME: entry.data[CONF_USERNAME]},
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> AclaraAceOptionsFlow:  # type: ignore[no-untyped-def]
        """Return the options flow."""
        return AclaraAceOptionsFlow()


class AclaraAceOptionsFlow(OptionsFlowWithReload):
    """Options: tiered tariff and billing cycle for the cost statistic."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show/handle the options form."""
        errors: dict[str, str] = {}
        options = self.config_entry.options
        if user_input is not None:
            text = user_input.get(CONF_TIERS, "")
            try:
                tiers = parse_tiers(text)
            except TariffError:
                errors[CONF_TIERS] = "invalid_tiers"
            else:
                if len(tiers) > 1 and not user_input.get(CONF_BILLING_START):
                    errors[CONF_BILLING_START] = "billing_start_required"
            if not errors:
                data = {
                    CONF_TIERS: format_tiers(tiers),
                    CONF_BILLING_START: user_input.get(CONF_BILLING_START) or "",
                    CONF_BILLING_PERIOD_DAYS: int(user_input[CONF_BILLING_PERIOD_DAYS]),
                }
                return self.async_create_entry(data=data)

        current_tiers = options.get(CONF_TIERS)
        if current_tiers is None and (flat := options.get(CONF_PRICE_PER_UNIT, DEFAULT_PRICE_PER_UNIT)):
            current_tiers = format_tiers((Tier(None, float(flat)),))
        suggested = {
            CONF_TIERS: current_tiers or "",
            CONF_BILLING_START: options.get(CONF_BILLING_START) or None,
            CONF_BILLING_PERIOD_DAYS: options.get(CONF_BILLING_PERIOD_DAYS, DEFAULT_BILLING_PERIOD_DAYS),
        }
        schema = vol.Schema(
            {
                vol.Optional(CONF_TIERS): TextSelector(TextSelectorConfig(multiline=True)),
                vol.Optional(CONF_BILLING_START): DateSelector(),
                vol.Required(CONF_BILLING_PERIOD_DAYS): NumberSelector(
                    NumberSelectorConfig(min=1, max=366, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="days")
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or suggested),
            errors=errors,
        )
