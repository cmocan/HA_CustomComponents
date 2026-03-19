"""Config flow for the TP-Link ER605 integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .http_client import ER605HttpClient, HttpError, HttpLoginError

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST):     cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)


class ER605ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host     = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            unique_id, err = await self._test_connection(host, username, password)

            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host}
                )
                return self.async_create_entry(
                    title=f"TP-Link ER605 ({host})",
                    data={
                        CONF_HOST:     host,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host     = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            unique_id, err = await self._test_connection(host, username, password)

            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured(
                    updates={CONF_HOST: host}
                )
                return self.async_update_reload_and_abort(
                    self._get_reconfigure_entry(),
                    data_updates={
                        CONF_HOST:     host,
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        reauth_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }
        )

        if user_input is not None:
            host     = entry.data[CONF_HOST]
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            _, err = await self._test_connection(host, username, password)

            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: password,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=reauth_schema,
            errors=errors,
            description_placeholders={"host": entry.data.get(CONF_HOST, "")},
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ER605OptionsFlow:
        return ER605OptionsFlow()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _test_connection(
        self, host: str, username: str, password: str
    ) -> tuple[str, str | None]:
        """Try to login and extract the unique_id (WAN1 MAC).

        Returns (unique_id, None) on success or ("", error_key) on failure.
        """
        client = ER605HttpClient(host, username, password)
        try:
            await client.login()
            ifaces    = await client.get_interfaces()
            unique_id = _extract_unique_id(ifaces)
            if not unique_id:
                return "", "cannot_connect"
            return unique_id, None
        except HttpLoginError:
            return "", "invalid_auth"
        except (HttpError, TimeoutError) as err:
            _LOGGER.error("Cannot connect to ER605 at %s: %s (%s)", host, err, type(err).__name__)
            return "", "cannot_connect"
        except Exception as err:
            _LOGGER.exception("Unexpected error connecting to ER605 at %s: %s", host, err)
            return "", "cannot_connect"
        finally:
            await client.async_close()


class ER605OptionsFlow(config_entries.OptionsFlow):
    """Options flow — only the poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current_interval): vol.All(
                        vol.Coerce(int),
                        vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                    )
                }
            ),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_unique_id(ifaces: list[dict]) -> str:
    """Return the WAN1 MAC address as a lowercase, dash-free string."""
    for iface in ifaces:
        if iface.get("t_name", "").startswith("WAN") and iface.get("macaddr"):
            return iface["macaddr"].replace("-", "").lower()
    # Fallback: first interface with a MAC
    for iface in ifaces:
        if iface.get("macaddr"):
            return iface["macaddr"].replace("-", "").lower()
    return ""
