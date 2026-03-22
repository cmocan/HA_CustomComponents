"""ISP Routers Home Assistant integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from .const import CONF_POLL_INTERVAL, CONF_ROUTER_TYPE, DEFAULT_POLL_INTERVAL, DOMAIN
from .coordinator import IspRoutersCoordinator
from .data import IspRoutersRuntimeData
from .router_registry import ROUTER_REGISTRY

# Import router modules to trigger self-registration into ROUTER_REGISTRY
from .routers import arris_tg3442de as _arris  # noqa: F401
from .routers import zte_f660 as _zte          # noqa: F401

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.DEVICE_TRACKER,
]


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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry — close HTTP session after platforms are unloaded."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.coordinator.async_close()
    return ok


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload entry when poll_interval option changes."""
    await hass.config_entries.async_reload(entry.entry_id)
