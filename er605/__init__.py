"""TP-Link ER605 Home Assistant integration."""

from __future__ import annotations

import logging

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
    """Set up ER605 from a config entry."""
    host     = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    medium_interval = entry.options.get(CONF_MEDIUM_POLL_INTERVAL, DEFAULT_MEDIUM_POLL_INTERVAL)
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

    # First data refresh
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = ER605RuntimeData(
        coordinator = coordinator,
        device_info = device_info,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register per-tier manual refresh services (once per domain)
    _register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ER605ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.coordinator._client.async_close()
    # Remove services when last entry is unloaded
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        for svc in (SERVICE_REFRESH_FAST, SERVICE_REFRESH_MEDIUM, SERVICE_REFRESH_IPSTATS, SERVICE_REFRESH_ALL):
            hass.services.async_remove(DOMAIN, svc)
    return ok


async def _async_update_options(
    hass: HomeAssistant, entry: ER605ConfigEntry
) -> None:
    """React to options changes (poll interval) by reloading the entry."""
    await hass.config_entries.async_reload(entry.entry_id)


# ── Service registration ──────────────────────────────────────────────────────

def _get_coordinator(hass: HomeAssistant) -> ER605Coordinator:
    """Return the coordinator from the first loaded config entry."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ValueError("No ER605 config entry loaded")
    entry: ER605ConfigEntry = entries[0]
    return entry.runtime_data.coordinator


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
