"""TP-Link ER605 Home Assistant integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import (
    CONF_IPSTATS_POLL_INTERVAL,
    CONF_MEDIUM_POLL_INTERVAL,
    CONF_POLL_INTERVAL,
    DEFAULT_IPSTATS_POLL_INTERVAL,
    DEFAULT_MEDIUM_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .coordinator import ER605Coordinator
from .data import ER605ConfigEntry, ER605RuntimeData
from .http_client import ER605HttpClient, HttpError, HttpLoginError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]

SERVICE_REFRESH_FAST    = "refresh_fast"
SERVICE_REFRESH_MEDIUM  = "refresh_medium"
SERVICE_REFRESH_IPSTATS = "refresh_ipstats"
SERVICE_REFRESH_ALL     = "refresh_all"


async def async_setup_entry(hass: HomeAssistant, entry: ER605ConfigEntry) -> bool:
    """Set up ER605 from a config entry — HTTP or SNMP."""
    from .const import PROTOCOL_SNMP

    protocol = entry.data.get("protocol", "http")

    if protocol == PROTOCOL_SNMP:
        return await _async_setup_snmp(hass, entry)
    return await _async_setup_http(hass, entry)


async def _async_setup_http(hass: HomeAssistant, entry) -> bool:
    """Existing HTTP setup — content moved verbatim from async_setup_entry."""
    host             = entry.data[CONF_HOST]
    username         = entry.data[CONF_USERNAME]
    password         = entry.data[CONF_PASSWORD]
    interval         = entry.options.get(CONF_POLL_INTERVAL,        DEFAULT_POLL_INTERVAL)
    medium_interval  = entry.options.get(CONF_MEDIUM_POLL_INTERVAL, DEFAULT_MEDIUM_POLL_INTERVAL)
    ipstats_interval = entry.options.get(CONF_IPSTATS_POLL_INTERVAL, DEFAULT_IPSTATS_POLL_INTERVAL)

    client = ER605HttpClient(host, username, password)
    coordinator = ER605Coordinator(
        hass, client,
        poll_interval=interval,
        medium_poll_interval=medium_interval,
        ipstats_poll_interval=ipstats_interval,
    )
    try:
        device_info = await coordinator.async_setup()
    except ConfigEntryAuthFailed:
        await client.async_close()
        raise
    except Exception as err:
        await client.async_close()
        raise ConfigEntryNotReady(f"Cannot connect to ER605 at {host}: {err}") from err

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = ER605RuntimeData(coordinator=coordinator, device_info=device_info)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def _async_setup_snmp(hass: HomeAssistant, entry) -> bool:
    """SNMP setup — mirrors HTTP setup structure."""
    from .const import (
        CONF_COMMUNITY, CONF_SNMP_PORT,
        DEFAULT_SNMP_MEDIUM_POLL_INTERVAL, DEFAULT_SNMP_POLL_INTERVAL,
        DEFAULT_SNMP_STATIC_POLL_INTERVAL,
    )
    from .snmp_client import ER605SnmpClient, SnmpConnectionError
    from .snmp_coordinator import ER605SnmpCoordinator
    from .snmp_data import SnmpRuntimeData

    host      = entry.data[CONF_HOST]
    community = entry.data[CONF_COMMUNITY]
    port      = entry.data.get(CONF_SNMP_PORT, 161)
    interval        = entry.options.get(CONF_POLL_INTERVAL,        DEFAULT_SNMP_POLL_INTERVAL)
    medium_interval = entry.options.get(CONF_MEDIUM_POLL_INTERVAL, DEFAULT_SNMP_MEDIUM_POLL_INTERVAL)
    static_interval = entry.options.get(CONF_IPSTATS_POLL_INTERVAL, DEFAULT_SNMP_STATIC_POLL_INTERVAL)

    client = ER605SnmpClient(host=host, port=port, community=community)
    coordinator = ER605SnmpCoordinator(
        hass, client,
        poll_interval=interval,
        medium_poll_interval=medium_interval,
        static_poll_interval=static_interval,
    )
    try:
        device_info = await coordinator.async_setup()
    except SnmpConnectionError as err:
        raise ConfigEntryNotReady(f"Cannot reach ER605 via SNMP at {host}: {err}") from err
    except Exception as err:
        raise ConfigEntryNotReady(f"SNMP setup failed for {host}: {err}") from err

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = SnmpRuntimeData(coordinator=coordinator, device_info=device_info)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_services(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ER605ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        from .const import PROTOCOL_HTTP
        protocol = entry.data.get("protocol", "http")
        if protocol == PROTOCOL_HTTP:
            await entry.runtime_data.coordinator._client.async_close()
        # SNMP coordinator has no separate client close needed (UDP, stateless)
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        for svc in (SERVICE_REFRESH_FAST, SERVICE_REFRESH_MEDIUM,
                    SERVICE_REFRESH_IPSTATS, SERVICE_REFRESH_ALL):
            hass.services.async_remove(DOMAIN, svc)
    return ok


async def _async_update_options(
    hass: HomeAssistant, entry: ER605ConfigEntry
) -> None:
    """React to options changes (poll interval) by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


# ── Service registration ──────────────────────────────────────────────────────

def _get_coordinator(hass: HomeAssistant) -> Any:
    """Return the coordinator from the first loaded config entry."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ValueError("No ER605 config entry loaded")
    return entries[0].runtime_data.coordinator


def _register_services(hass: HomeAssistant) -> None:
    """Register per-tier manual refresh services (idempotent)."""
    if hass.services.has_service(DOMAIN, SERVICE_REFRESH_FAST):
        return  # already registered

    async def _handle_refresh_fast(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_refresh_fast()

    async def _handle_refresh_medium(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_refresh_medium()

    async def _handle_refresh_ipstats(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_refresh_ipstats()

    async def _handle_refresh_all(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_refresh_all()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH_FAST, _handle_refresh_fast, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_MEDIUM, _handle_refresh_medium, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_IPSTATS, _handle_refresh_ipstats, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_ALL, _handle_refresh_all, schema=vol.Schema({}))
