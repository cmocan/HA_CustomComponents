"""Base entity class for the ISP Routers integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import IspRoutersCoordinator


class IspRoutersEntity(CoordinatorEntity[IspRoutersCoordinator]):
    """Shared DeviceInfo and coordinator binding for all ISP Routers entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: IspRoutersCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        data = coordinator.data
        entry = coordinator.hass.config_entries.async_get_entry(entry_id)
        host = entry.data.get("host") if entry else None
        config_url = f"http://{host}" if host else None

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=coordinator.strategy.display_name,
            manufacturer=self._manufacturer(coordinator.strategy.display_name),
            model=data.model if data else None,
            sw_version=data.firmware if data else None,
            configuration_url=config_url,
        )

    @staticmethod
    def _manufacturer(display_name: str) -> str:
        if "Arris" in display_name:
            return "Arris"
        if "ZTE" in display_name:
            return "ZTE"
        return "Unknown"
