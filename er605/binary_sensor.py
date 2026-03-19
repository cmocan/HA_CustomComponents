"""Binary sensor platform for the TP-Link ER605 integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import ER605Coordinator
from .data import ER605RuntimeData
from .entity import ER605Entity

PARALLEL_UPDATES = 0


# ── Entity descriptions ───────────────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class ER605BinaryEntityDescription(BinarySensorEntityDescription):
    """Extended description carrying the interface/port key."""

    interface_key: str = ""   # WAN interface t_name, e.g. "WAN1"
    port_key:      str = ""   # physical port number, e.g. "1"


# ── Setup ─────────────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ER605RuntimeData = entry.runtime_data
    coordinator: ER605Coordinator = runtime.coordinator

    entities: list[ER605Entity] = []

    # One WAN connectivity sensor per discovered WAN interface
    for iface in coordinator.data.wan_interfaces:
        entities.append(
            ER605WANConnectivitySensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key              = f"{iface.entity_key}_connected",
                    name             = f"{iface.label} Connected",
                    device_class     = BinarySensorDeviceClass.CONNECTIVITY,
                    interface_key    = iface.name,
                ),
            )
        )

    # One IPv6 enabled sensor per WAN with IPv6 data
    for ipv6 in coordinator.data.ipv6_interfaces:
        entities.append(
            ER605IPv6EnabledSensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key           = f"{ipv6.name.lower()}_ipv6_enabled",
                    name          = f"{ipv6.label} IPv6 Enabled",
                    interface_key = ipv6.name,
                    entity_registry_enabled_default = False,
                ),
            )
        )

    # One physical port connected sensor per port (disabled by default)
    for port in coordinator.data.physical_ports:
        entities.append(
            ER605PortConnectedSensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key          = f"port_{port.port}_connected",
                    name         = f"Port {port.port} Connected",
                    device_class = BinarySensorDeviceClass.CONNECTIVITY,
                    port_key     = port.port,
                    entity_registry_enabled_default = False,
                ),
            )
        )

    async_add_entities(entities)


# ── Entity classes ────────────────────────────────────────────────────────────

class ER605WANConnectivitySensor(ER605Entity, BinarySensorEntity):
    """WAN interface up/down state."""

    entity_description: ER605BinaryEntityDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605BinaryEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        iface = self.coordinator.data.interface(
            self.entity_description.interface_key
        )
        return iface.is_up if iface else None


class ER605IPv6EnabledSensor(ER605Entity, BinarySensorEntity):
    """Whether IPv6 is enabled on a WAN interface."""

    entity_description: ER605BinaryEntityDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605BinaryEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        ipv6 = self.coordinator.data.ipv6(
            self.entity_description.interface_key
        )
        return ipv6.enabled if ipv6 else None


class ER605PortConnectedSensor(ER605Entity, BinarySensorEntity):
    """Physical switch port link state."""

    entity_description: ER605BinaryEntityDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605BinaryEntityDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def is_on(self) -> bool | None:
        for port in self.coordinator.data.physical_ports:
            if port.port == self.entity_description.port_key:
                return port.connected
        return None
