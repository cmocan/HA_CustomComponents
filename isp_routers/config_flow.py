"""Config flow for the ISP Routers integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback

from .const import (
    CONF_POLL_INTERVAL,
    CONF_ROUTER_TYPE,
    CONF_ZTE_MODEL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .router_registry import AuthError, ROUTER_REGISTRY

# Import router modules to ensure they are registered before config flow runs
from .routers import arris_tg3442de as _arris  # noqa: F401
from .routers import zte_f660 as _zte          # noqa: F401

_LOGGER = logging.getLogger(__name__)


def _router_type_schema() -> vol.Schema:
    return vol.Schema({
        vol.Required(CONF_ROUTER_TYPE): vol.In(list(ROUTER_REGISTRY.keys())),
    })


def _credentials_schema(router_type: str, defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    schema: dict = {
        vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
        vol.Required(CONF_USERNAME, default=d.get(CONF_USERNAME, "admin")): str,
        vol.Required(CONF_PASSWORD, default=""): str,
    }
    if router_type == "zte_f660":
        from .routers.zte_f660 import ZTE_MODEL_CHOICES
        schema[vol.Required(
            CONF_ZTE_MODEL, default=d.get(CONF_ZTE_MODEL, "f660")
        )] = vol.In(ZTE_MODEL_CHOICES)
    return vol.Schema(schema)


class IspRoutersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Two-step config flow: pick router type → enter credentials."""

    VERSION = 1
    _router_type: str = ""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: choose router type."""
        if user_input is not None:
            self._router_type = user_input[CONF_ROUTER_TYPE]
            return await self.async_step_credentials()
        return self.async_show_form(
            step_id="user",
            data_schema=_router_type_schema(),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 2: enter credentials and validate against the real router."""
        errors: dict[str, str] = {}
        if user_input is not None:
            strategy = ROUTER_REGISTRY[self._router_type]
            client = strategy.client_class(**user_input)
            unique_id: str | None = None
            try:
                await client.async_login()
                unique_id = await client.async_get_unique_id()
            except AuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow validation")
                errors["base"] = "cannot_connect"
            finally:
                await client.async_logout()
                await client.async_close()

            if not errors and unique_id is not None:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                entry_data = {CONF_ROUTER_TYPE: self._router_type, **user_input}
                return self.async_create_entry(
                    title=f"{strategy.display_name} ({user_input.get(CONF_HOST)})",
                    data=entry_data,
                )

        return self.async_show_form(
            step_id="credentials",
            data_schema=_credentials_schema(self._router_type),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Reauth initiated when ConfigEntryAuthFailed is raised by coordinator."""
        self._router_type = entry_data.get(CONF_ROUTER_TYPE, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show password-only form for reauth."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            strategy = ROUTER_REGISTRY[self._router_type]
            merged = {k: v for k, v in entry.data.items() if k != CONF_ROUTER_TYPE}
            merged[CONF_PASSWORD] = user_input[CONF_PASSWORD]
            client = strategy.client_class(**merged)
            try:
                await client.async_login()
            except AuthError:
                errors["base"] = "invalid_auth"
            except Exception:
                errors["base"] = "cannot_connect"
            finally:
                await client.async_logout()
                await client.async_close()

            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                    reason="reauth_successful",
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IspRoutersOptionsFlow:
        return IspRoutersOptionsFlow(config_entry)


class IspRoutersOptionsFlow(config_entries.OptionsFlow):
    """Options flow — allows adjusting poll_interval after setup."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=self._entry.options.get(
                        CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL
                    ),
                ): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL),
                ),
            }),
        )
