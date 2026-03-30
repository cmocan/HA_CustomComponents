"""Switch platform for the ISP Routers integration (WiFi toggle)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IspRoutersCoordinator
from .data import IspRoutersConfigEntry
from .entity import IspRoutersEntity
from .router_registry import AuthError, FetchError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IspRoutersConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IspRoutersCoordinator = entry.runtime_data.coordinator
    entities: list[SwitchEntity] = []

    if hasattr(coordinator.client, "async_set_wifi_enabled"):
        entities.append(WiFiMasterSwitch(coordinator, entry.entry_id))

    async_add_entities(entities)


class WiFiMasterSwitch(IspRoutersEntity, SwitchEntity):
    """Switch that enables/disables both WiFi radios on the router.

    Current state is read from the last coordinator poll (wifi_24g_enabled AND
    wifi_5g_enabled both True → switch is on).  The action performs a full
    login → set_wifi_enabled → logout cycle to change the state, then requests
    a coordinator refresh.
    """

    _attr_name = "WiFi"
    _attr_icon = "mdi:wifi"

    def __init__(self, coordinator: IspRoutersCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_wifi_master_switch"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        # Both bands must be enabled for the switch to read as ON
        if data.wifi_24g_enabled is not None and data.wifi_5g_enabled is not None:
            return data.wifi_24g_enabled and data.wifi_5g_enabled
        # Fall back to single-band field if present
        if data.wifi_enabled is not None:
            return data.wifi_enabled
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._toggle(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._toggle(False)

    async def _toggle(self, enabled: bool) -> None:
        client = self.coordinator.client
        async with self.coordinator.client_lock:
            try:
                await client.async_login()
                await client.async_set_wifi_enabled(enabled)
            except (AuthError, FetchError) as err:
                _LOGGER.error("WiFi toggle failed: %s", err)
                return
            except Exception as err:
                _LOGGER.error("WiFi toggle unexpected error: %s", err)
                return
            finally:
                await client.async_logout()

        await self.coordinator.async_request_refresh()
