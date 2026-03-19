"""Base entity class for the TP-Link ER605 integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ER605Coordinator


class ER605Entity(CoordinatorEntity[ER605Coordinator]):
    """Base class: shared DeviceInfo and coordinator binding for all ER605 entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ER605Coordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        dev = coordinator.device_info

        entry = coordinator.hass.config_entries.async_get_entry(entry_id)
        config_url = f"https://{entry.data['host']}" if entry else None

        self._attr_device_info = DeviceInfo(
            identifiers       = {(DOMAIN, entry_id)},
            name              = dev.model if dev else "TP-Link ER605",
            manufacturer      = "TP-Link",
            model             = dev.model if dev else "ER605",
            hw_version        = dev.hw_version if dev else None,
            sw_version        = dev.fw_version if dev else None,
            configuration_url = config_url,
        )
