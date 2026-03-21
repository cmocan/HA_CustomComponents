# custom_components/er605/snmp_entity.py
"""Base entity class for the TP-Link ER605 SNMP integration."""
from __future__ import annotations

try:
    from homeassistant.helpers.device_registry import DeviceInfo
    from homeassistant.helpers.update_coordinator import CoordinatorEntity

    from .const import CONF_SNMP_PORT, DOMAIN
    from .snmp_coordinator import ER605SnmpCoordinator
except ImportError:
    from const import CONF_SNMP_PORT, DOMAIN  # type: ignore[no-redef]
    from snmp_coordinator import ER605SnmpCoordinator  # type: ignore[no-redef]

    DeviceInfo = dict  # type: ignore[misc,assignment]

    class CoordinatorEntity:  # type: ignore[no-redef]
        def __init__(self, coordinator, *a, **kw):
            self.coordinator = coordinator

        def __class_getitem__(cls, item):
            return cls


class ER605SnmpEntity(CoordinatorEntity[ER605SnmpCoordinator]):
    """Base class: shared DeviceInfo and coordinator binding for SNMP entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ER605SnmpCoordinator, entry_id: str) -> None:
        super().__init__(coordinator)
        dev  = coordinator.device_info
        entry = coordinator.hass.config_entries.async_get_entry(entry_id)
        host  = entry.data.get("host", "") if entry else ""
        port  = entry.data.get(CONF_SNMP_PORT, 161) if entry else 161

        self._attr_device_info = DeviceInfo(
            identifiers       = {(DOMAIN, entry_id)},
            name              = dev.model if dev else "TP-Link ER605",
            manufacturer      = "TP-Link",
            model             = dev.model if dev else "ER605",
            sw_version        = dev.fw_version if dev else None,
            configuration_url = f"http://{host}",
        )
