"""Config flow for the TP-Link ER605 integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import FlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_COMMUNITY,
    CONF_IPSTATS_POLL_INTERVAL,
    CONF_MEDIUM_POLL_INTERVAL,
    CONF_POLL_INTERVAL,
    CONF_PROTOCOL,
    CONF_SNMP_PORT,
    DEFAULT_IPSTATS_POLL_INTERVAL,
    DEFAULT_MEDIUM_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SNMP_MEDIUM_POLL_INTERVAL,
    DEFAULT_SNMP_POLL_INTERVAL,
    DEFAULT_SNMP_STATIC_POLL_INTERVAL,
    DOMAIN,
    MAX_IPSTATS_POLL_INTERVAL,
    MAX_MEDIUM_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MAX_SNMP_STATIC_POLL_INTERVAL,
    MIN_IPSTATS_POLL_INTERVAL,
    MIN_MEDIUM_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MIN_SNMP_STATIC_POLL_INTERVAL,
    PROTOCOL_HTTP,
    PROTOCOL_SNMP,
)
from .http_client import ER605HttpClient, HttpError, HttpLoginError

_LOGGER = logging.getLogger(__name__)

# ── Schemas ───────────────────────────────────────────────────────────────────

STEP_PROTOCOL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PROTOCOL, default=PROTOCOL_HTTP): vol.In(
            [PROTOCOL_HTTP, PROTOCOL_SNMP]
        ),
    }
)

STEP_HTTP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST):     cv.string,
        vol.Required(CONF_USERNAME): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)

STEP_SNMP_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST):      cv.string,
        vol.Required(CONF_COMMUNITY): cv.string,
        vol.Optional(CONF_SNMP_PORT, default=161): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
    }
)

# Keep backward compat alias
STEP_USER_SCHEMA = STEP_HTTP_SCHEMA


class ER605ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup config flow."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._protocol: str = PROTOCOL_HTTP

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 1: choose HTTP or SNMP."""
        if user_input is not None:
            self._protocol = user_input[CONF_PROTOCOL]
            if self._protocol == PROTOCOL_SNMP:
                return await self.async_step_snmp()
            return await self.async_step_http()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_PROTOCOL_SCHEMA,
        )

    async def async_step_http(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2a: HTTP credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host     = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD]

            unique_id, err = await self._test_http_connection(host, username, password)
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=f"TP-Link ER605 ({host})",
                    data={
                        CONF_PROTOCOL:  PROTOCOL_HTTP,
                        CONF_HOST:      host,
                        CONF_USERNAME:  username,
                        CONF_PASSWORD:  password,
                    },
                )

        return self.async_show_form(
            step_id="http",
            data_schema=STEP_HTTP_SCHEMA,
            errors=errors,
        )

    async def async_step_snmp(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Step 2b: SNMP credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host      = user_input[CONF_HOST].strip()
            community = user_input[CONF_COMMUNITY]
            port      = int(user_input.get(CONF_SNMP_PORT, 161))

            unique_id, err = await self._test_snmp_connection(host, community, port)
            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_create_entry(
                    title=f"TP-Link ER605 SNMP ({host})",
                    data={
                        CONF_PROTOCOL:  PROTOCOL_SNMP,
                        CONF_HOST:      host,
                        CONF_COMMUNITY: community,
                        CONF_SNMP_PORT: port,
                    },
                )

        return self.async_show_form(
            step_id="snmp",
            data_schema=STEP_SNMP_SCHEMA,
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        protocol = entry.data.get(CONF_PROTOCOL, PROTOCOL_HTTP)

        schema = STEP_SNMP_SCHEMA if protocol == PROTOCOL_SNMP else STEP_HTTP_SCHEMA

        if user_input is not None:
            if protocol == PROTOCOL_SNMP:
                host      = user_input[CONF_HOST].strip()
                community = user_input[CONF_COMMUNITY]
                port      = int(user_input.get(CONF_SNMP_PORT, 161))
                unique_id, err = await self._test_snmp_connection(host, community, port)
                updates = {CONF_PROTOCOL: PROTOCOL_SNMP, CONF_HOST: host, CONF_COMMUNITY: community, CONF_SNMP_PORT: port}
            else:
                host     = user_input[CONF_HOST].strip()
                username = user_input[CONF_USERNAME].strip()
                password = user_input[CONF_PASSWORD]
                unique_id, err = await self._test_http_connection(host, username, password)
                updates = {CONF_PROTOCOL: PROTOCOL_HTTP, CONF_HOST: host, CONF_USERNAME: username, CONF_PASSWORD: password}

            if err:
                errors["base"] = err
            else:
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host})
                return self.async_update_reload_and_abort(
                    entry, data_updates=updates
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Re-auth only applies to HTTP (SNMP has no session to expire)."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        # SNMP entries have no session-based auth — reauth does not apply
        if entry.data.get(CONF_PROTOCOL) == PROTOCOL_SNMP:
            return self.async_abort(reason="snmp_no_reauth")

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
            _, err = await self._test_http_connection(host, username, password)
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_USERNAME: username, CONF_PASSWORD: password},
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

    async def _test_http_connection(
        self, host: str, username: str, password: str
    ) -> tuple[str, str | None]:
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
            _LOGGER.error("Cannot connect to ER605 at %s: %s", host, err)
            return "", "cannot_connect"
        except Exception as err:
            _LOGGER.exception("Unexpected error connecting to ER605 at %s: %s", host, err)
            return "", "cannot_connect"
        finally:
            await client.async_close()

    async def _test_snmp_connection(
        self, host: str, community: str, port: int
    ) -> tuple[str, str | None]:
        from .snmp_client import ER605SnmpClient, SnmpConnectionError

        client = ER605SnmpClient(host=host, port=port, community=community, timeout=5)
        try:
            await client.get("1.3.6.1.2.1.1.5.0")  # liveness probe
            # Try to get WAN1 MAC for unique_id
            try:
                mac_raw   = await client.get("1.3.6.1.2.1.2.2.1.6.1026")
                # pysnmp OctetString: strip "0x" prefix and spaces, then hex-decode
                mac_bytes = bytes.fromhex(str(mac_raw).replace("0x", "").replace(" ", ""))
                unique_id = mac_bytes.hex()
            except Exception as mac_err:
                _LOGGER.debug("WAN1 MAC not available, using host fallback: %s", mac_err)
                unique_id = f"snmp_{host}"
            return unique_id, None
        except SnmpConnectionError:
            return "", "cannot_connect"
        except Exception as err:
            _LOGGER.exception("Unexpected SNMP error connecting to %s: %s", host, err)
            return "", "cannot_connect"


class ER605OptionsFlow(config_entries.OptionsFlow):
    """Options flow — poll intervals for HTTP and SNMP."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        protocol = self.config_entry.data.get(CONF_PROTOCOL, PROTOCOL_HTTP)
        is_snmp  = (protocol == PROTOCOL_SNMP)

        # Defaults differ by protocol
        default_fast   = DEFAULT_SNMP_POLL_INTERVAL        if is_snmp else DEFAULT_POLL_INTERVAL
        default_medium = DEFAULT_SNMP_MEDIUM_POLL_INTERVAL if is_snmp else DEFAULT_MEDIUM_POLL_INTERVAL
        default_slow   = DEFAULT_SNMP_STATIC_POLL_INTERVAL if is_snmp else DEFAULT_IPSTATS_POLL_INTERVAL
        max_slow       = MAX_SNMP_STATIC_POLL_INTERVAL     if is_snmp else MAX_IPSTATS_POLL_INTERVAL
        min_slow       = MIN_SNMP_STATIC_POLL_INTERVAL     if is_snmp else MIN_IPSTATS_POLL_INTERVAL

        if user_input is not None:
            for key in (CONF_POLL_INTERVAL, CONF_MEDIUM_POLL_INTERVAL, CONF_IPSTATS_POLL_INTERVAL):
                val = user_input.get(key, 0)
                if 0 < val < 5:
                    errors[key] = "invalid_interval"
            if not errors:
                return self.async_create_entry(data=user_input)

        current_fast   = self.config_entry.options.get(CONF_POLL_INTERVAL,        default_fast)
        current_medium = self.config_entry.options.get(CONF_MEDIUM_POLL_INTERVAL, default_medium)
        current_slow   = self.config_entry.options.get(CONF_IPSTATS_POLL_INTERVAL, default_slow)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POLL_INTERVAL, default=current_fast): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                    ),
                    vol.Required(CONF_MEDIUM_POLL_INTERVAL, default=current_medium): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_MEDIUM_POLL_INTERVAL, max=MAX_MEDIUM_POLL_INTERVAL)
                    ),
                    vol.Required(CONF_IPSTATS_POLL_INTERVAL, default=current_slow): vol.All(
                        vol.Coerce(int), vol.Range(min=min_slow, max=max_slow)
                    ),
                }
            ),
            errors=errors,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_unique_id(ifaces: list[dict]) -> str:
    for iface in ifaces:
        if iface.get("t_name", "").startswith("WAN") and iface.get("macaddr"):
            return iface["macaddr"].replace("-", "").lower()
    for iface in ifaces:
        if iface.get("macaddr"):
            return iface["macaddr"].replace("-", "").lower()
    return ""
