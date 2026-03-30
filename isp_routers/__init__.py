"""ISP Routers Home Assistant integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr

from .const import CONF_POLL_INTERVAL, CONF_ROUTER_TYPE, DEFAULT_POLL_INTERVAL, DOMAIN
from .coordinator import IspRoutersCoordinator
from .data import IspRoutersRuntimeData
from .router_registry import AuthError, FetchError, ROUTER_REGISTRY

# Import router modules to trigger self-registration into ROUTER_REGISTRY
from .routers import arris_tg3442de as _arris  # noqa: F401
from .routers import zte_f660 as _zte          # noqa: F401

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.SWITCH,
]

# Maps HA service field names → Arris router payload field names (fallback)
_WIFI_FIELD_MAP: dict[str, str] = {
    "enable_wifi":        "EnableWiFiFunction",
    "enable_24g":         "Enable",
    "ssid_24g":           "SSID",
    "passphrase_24g":     "Passphrase",
    "ssid_broadcast_24g": "SSIDAdvertisementEnabled",
    "mode_24g":           "ModeEnabled",
    "enable_5g":          "Enable5G",
    "ssid_5g":            "SSID5G",
    "passphrase_5g":      "Passphrase5G",
    "ssid_broadcast_5g":  "SSIDAdvertisementEnabled5G",
    "mode_5g":            "ModeEnabled5G",
    "enable_guest":       "EnableGuest",
    "ssid_guest":         "SSIDGuest",
    "passphrase_guest":   "PassphraseGuest",
    "guest_isolation":    "IsolationEnabledGuest",
    "band_steering":      "BandSteerEnable",
    "split_ssid":         "SplitSSIDEnable",
}

# Maps router_type → HA service name for WiFi configuration
_WIFI_SERVICE_NAMES: dict[str, str] = {
    "arris_tg3442de": "configure_wifi_arris",
    "zte_f660":       "configure_wifi_zte",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an ISP router from a config entry."""
    router_type = entry.data[CONF_ROUTER_TYPE]
    if router_type not in ROUTER_REGISTRY:
        raise ConfigEntryNotReady(f"Unknown router type: {router_type!r}")

    strategy = ROUTER_REGISTRY[router_type]
    client_kwargs = {k: v for k, v in entry.data.items() if k != CONF_ROUTER_TYPE}
    client = strategy.client_class(**client_kwargs)

    coordinator = IspRoutersCoordinator(
        hass,
        strategy,
        client,
        poll_interval=entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        await coordinator.async_close()
        raise
    except Exception as err:
        await coordinator.async_close()
        raise ConfigEntryNotReady(
            f"Cannot connect to {strategy.display_name} at {entry.data.get('host')}: {err}"
        ) from err

    entry.runtime_data = IspRoutersRuntimeData(coordinator=coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Register router-specific configure_wifi service
    if hasattr(coordinator.client, "async_set_wifi_config"):
        service_name = _WIFI_SERVICE_NAMES.get(router_type)
        if service_name and not hass.services.has_service(DOMAIN, service_name):
            hass.services.async_register(
                DOMAIN,
                service_name,
                _make_configure_wifi_handler(hass, service_name),
            )

    return True


def _make_configure_wifi_handler(hass: HomeAssistant, service_name: str):
    """Return a service handler closure for a configure_wifi_* service."""

    async def _handle_configure_wifi(call: ServiceCall) -> None:
        # Resolve device_id → config entry
        device_ids: set[str] = call.data.get("device_id", set())
        if isinstance(device_ids, str):
            device_ids = {device_ids}

        if not device_ids:
            raise HomeAssistantError(f"{service_name}: no device targeted")

        dev_reg = dr.async_get(hass)
        coordinator: IspRoutersCoordinator | None = None

        for device_id in device_ids:
            device = dev_reg.async_get(device_id)
            if device is None:
                continue
            for entry_id in device.config_entries:
                entry = hass.config_entries.async_get_entry(entry_id)
                if (entry and entry.domain == DOMAIN
                        and hasattr(entry, "runtime_data")
                        and hasattr(entry.runtime_data.coordinator.client, "async_set_wifi_config")):
                    coordinator = entry.runtime_data.coordinator
                    break
            if coordinator:
                break

        if coordinator is None:
            raise HomeAssistantError(
                f"{service_name}: could not find a supported router in the service call target"
            )

        # Map HA field names → router field names, skip fields not provided.
        # Use per-client WIFI_FIELD_MAP if available, else fall back to the
        # global Arris-only _WIFI_FIELD_MAP for backward compatibility.
        field_map = getattr(coordinator.client, 'WIFI_FIELD_MAP', _WIFI_FIELD_MAP)
        overrides: dict[str, Any] = {}
        for ha_field, router_field in field_map.items():
            if ha_field in call.data:
                overrides[router_field] = call.data[ha_field]

        if not overrides:
            _LOGGER.warning("%s called with no fields — nothing to do", service_name)
            return

        client = coordinator.client
        async with coordinator.client_lock:
            try:
                await client.async_login()
                await client.async_set_wifi_config(overrides)
            except AuthError as err:
                raise HomeAssistantError(f"{service_name}: authentication failed: {err}") from err
            except FetchError as err:
                raise HomeAssistantError(f"{service_name}: router error: {err}") from err
            except Exception as err:
                _LOGGER.exception("%s: unexpected error", service_name)
                raise HomeAssistantError(f"{service_name}: unexpected error: {err}") from err
            finally:
                await client.async_logout()

        await coordinator.async_request_refresh()

    return _handle_configure_wifi


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — close HTTP session after platforms are unloaded."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.coordinator.async_close()

        # Unregister router-specific service if no more entries of this type remain
        router_type = entry.data.get(CONF_ROUTER_TYPE)
        service_name = _WIFI_SERVICE_NAMES.get(router_type)
        if service_name:
            remaining_same_type = [
                e for e in hass.config_entries.async_entries(DOMAIN)
                if e.entry_id != entry.entry_id
                and e.data.get(CONF_ROUTER_TYPE) == router_type
                and hasattr(e, "runtime_data")
                and hasattr(e.runtime_data.coordinator.client, "async_set_wifi_config")
            ]
            if not remaining_same_type and hass.services.has_service(DOMAIN, service_name):
                hass.services.async_remove(DOMAIN, service_name)

    return ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when poll_interval option changes."""
    await hass.config_entries.async_reload(entry.entry_id)
