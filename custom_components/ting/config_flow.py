"""Config flow for Ting."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .aggregator import AggregatorConfig
from .api import TingApi, extract_devices
from .auth import TingAuth
from .const import (
    BAND_HYSTERESIS,
    CONF_BAND_HIGH,
    CONF_BAND_LOW,
    CONF_FAST_INTERVAL,
    CONF_HOLD,
    CONF_INTERVAL,
    CONF_REFRESH_TOKEN,
    DEFAULT_BAND_HIGH,
    DEFAULT_BAND_LOW,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HOLD,
    DEFAULT_INTERVAL,
    DOMAIN,
)
from .exceptions import TingAuthError, TingConnectionError, TingResponseError

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


async def _validate_input(hass: HomeAssistant, user_input: dict[str, str]) -> dict[str, Any]:
    """Validate credentials and return entry data."""
    auth = TingAuth(async_get_clientsession(hass))
    await auth.async_login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
    api = TingApi(auth)
    user_data = await api.async_get_user()
    devices = extract_devices(user_data, auth.default_device)
    return {
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_REFRESH_TOKEN: auth.refresh_token,
        "user_id": auth.user_id,
        "default_device": auth.default_device,
        "device_count": len(devices),
    }


def _seconds(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            unit_of_measurement="s",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _volts(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=0.5,
            unit_of_measurement="V",
            mode=selector.NumberSelectorMode.BOX,
        )
    )


class TingOptionsFlow(config_entries.OptionsFlowWithReload):
    """Interval statistics options; saving reloads the entry."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show and validate the options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                AggregatorConfig(
                    interval=user_input[CONF_INTERVAL],
                    fast_interval=user_input[CONF_FAST_INTERVAL],
                    band_low=user_input[CONF_BAND_LOW],
                    band_high=user_input[CONF_BAND_HIGH],
                    hold=user_input[CONF_HOLD],
                    hysteresis=BAND_HYSTERESIS,
                )
            except ValueError:
                errors["base"] = "invalid_options"
            else:
                return self.async_create_entry(data=user_input)

        current = {**self.config_entry.options, **(user_input or {})}
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_INTERVAL, default=current.get(CONF_INTERVAL, DEFAULT_INTERVAL)
                ): _seconds(5, 3600),
                vol.Required(
                    CONF_FAST_INTERVAL,
                    default=current.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL),
                ): _seconds(1, 300),
                vol.Required(
                    CONF_BAND_LOW, default=current.get(CONF_BAND_LOW, DEFAULT_BAND_LOW)
                ): _volts(90, 130),
                vol.Required(
                    CONF_BAND_HIGH, default=current.get(CONF_BAND_HIGH, DEFAULT_BAND_HIGH)
                ): _volts(100, 150),
                vol.Required(CONF_HOLD, default=current.get(CONF_HOLD, DEFAULT_HOLD)): _seconds(
                    0, 3600
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)


class TingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Ting config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> TingOptionsFlow:
        """Return the options flow."""
        return TingOptionsFlow()

    async def async_step_user(self, user_input: dict[str, str] | None = None) -> config_entries.FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _validate_input(self.hass, user_input)
            except TingAuthError as err:
                _LOGGER.warning("Ting authentication failed during setup: %s", err)
                errors["base"] = "invalid_auth"
            except TingConnectionError:
                errors["base"] = "cannot_connect"
            except TingResponseError:
                _LOGGER.exception("Unexpected Ting response during setup")
                errors["base"] = "unknown"
            except Exception:
                _LOGGER.exception("Unexpected error during Ting setup")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(str(data["user_id"]))
                self._abort_if_unique_id_configured()
                title = user_input[CONF_USERNAME]
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> config_entries.FlowResult:
        """Handle an expired refresh token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self,
        user_input: dict[str, str] | None = None,
    ) -> config_entries.FlowResult:
        """Ask the user to sign in again."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                data = await _validate_input(self.hass, user_input)
            except TingAuthError as err:
                _LOGGER.warning("Ting authentication failed during reauth: %s", err)
                errors["base"] = "invalid_auth"
            except TingConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during Ting reauth")
                errors["base"] = "unknown"
            else:
                entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
                if entry is not None:
                    if entry.unique_id is not None and entry.unique_id != str(data["user_id"]):
                        errors["base"] = "invalid_auth"
                    else:
                        self.hass.config_entries.async_update_entry(entry, data=data)
                        await self.hass.config_entries.async_reload(entry.entry_id)
                        return self.async_abort(reason="reauth_successful")
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
