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
from .snmp_coordinator import ER605SnmpCoordinator, build_wan_stubs
from .snmp_data import SnmpRouterData, SnmpWanData
from .snmp_entity import ER605SnmpEntity

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
    # SNMP path
    from .const import PROTOCOL_SNMP
    if entry.data.get("protocol") == PROTOCOL_SNMP:
        from .snmp_data import SnmpRuntimeData
        runtime: SnmpRuntimeData = entry.runtime_data
        async_add_entities(_build_snmp_binary_sensors(runtime.coordinator, entry.entry_id))
        return
    # existing HTTP path unchanged below
    runtime: ER605RuntimeData = entry.runtime_data
    coordinator: ER605Coordinator = runtime.coordinator

    entities: list[ER605Entity] = []

    # One WAN connectivity + IPv6 sensor per active WAN port.
    # Use device_info.wan_ports (fetched once at setup, always complete)
    # instead of coordinator.data.wan_interfaces (may be empty if Tier 2
    # hasn't run yet).
    dev_info = runtime.device_info
    active_indices = set(dev_info.active_wan_indices)
    wan_ports = [
        p for p in dev_info.wan_ports if p.index in active_indices
    ]

    for port in wan_ports:
        wan_name = f"WAN{port.index}"          # t_name used by interface API
        wan_key  = wan_name.lower()             # "wan1", "wan2"
        label    = port.name                    # "WAN1", "WAN/LAN2"

        entities.append(
            ER605WANConnectivitySensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key              = f"{wan_key}_connected",
                    name             = f"{label} Connected",
                    device_class     = BinarySensorDeviceClass.CONNECTIVITY,
                    interface_key    = wan_name,
                ),
            )
        )

        entities.append(
            ER605IPv6EnabledSensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key           = f"{wan_key}_ipv6_enabled",
                    name          = f"{label} IPv6 Enabled",
                    interface_key = wan_name,
                ),
            )
        )

        entities.append(
            ER605WanOnlineSensor(
                coordinator,
                entry.entry_id,
                ER605BinaryEntityDescription(
                    key              = f"{wan_key}_online",
                    name             = f"{label} Online",
                    device_class     = BinarySensorDeviceClass.CONNECTIVITY,
                    interface_key    = wan_name,
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


class ER605WanOnlineSensor(ER605Entity, BinarySensorEntity):
    """WAN gateway reachability (online detection) — HTTP only, Tier 2.

    Distinct from ER605WANConnectivitySensor (link state). This reflects
    whether the ER605 can reach its WAN gateway, not just whether the link
    is physically connected.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

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
        ) if self.coordinator.data else None
        return iface.online if iface else None


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


# ─────────────────────────────────────────────────────────────────────────────
# SNMP binary sensor entities
# ─────────────────────────────────────────────────────────────────────────────

class ER605SnmpWanLinkSensor(ER605SnmpEntity, BinarySensorEntity):
    """WAN link status binary sensor (Tier 1)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        wan: SnmpWanData,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._iface_slug = wan.iface_slug
        self._attr_unique_id = f"{entry_id}_snmp_wan_{wan.iface_slug}_link"
        self._attr_name = f"{wan.if_label} Link"

    @property
    def is_on(self) -> bool | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        wan = next((w for w in data.wan if w.iface_slug == self._iface_slug), None)
        return wan.is_up if wan else None

    @property
    def extra_state_attributes(self) -> dict:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return {}
        wan = next((w for w in data.wan if w.iface_slug == self._iface_slug), None)
        return {"link_speed_mbps": wan.link_speed_mbps} if wan else {}


class ER605SnmpPortLinkSensor(ER605SnmpEntity, BinarySensorEntity):
    """Physical port link status binary sensor (Tier 1)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        if_index: int,
        if_descr: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._if_index = if_index
        self._attr_unique_id = f"{entry_id}_snmp_port_{if_index}_link"
        self._attr_name = f"Port {if_descr.split('/')[-1]} Link"
        self._attr_entity_registry_enabled_default = False  # disabled by default

    @property
    def is_on(self) -> bool | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        port = next((p for p in data.ports if p.if_index == self._if_index), None)
        return port.oper_status == 1 if port else None

    @property
    def extra_state_attributes(self) -> dict:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return {}
        port = next((p for p in data.ports if p.if_index == self._if_index), None)
        if port is None:
            return {}
        return {"admin_enabled": port.admin_status == 1, "iface_name": port.if_descr}


def _build_snmp_binary_sensors(
    coordinator: ER605SnmpCoordinator, entry_id: str
) -> list[BinarySensorEntity]:
    """Build all SNMP binary sensor entities."""
    entities: list = []
    # WAN link — build stubs from discovery lists (available before first poll)
    for wan in build_wan_stubs(coordinator):
        entities.append(ER605SnmpWanLinkSensor(coordinator, entry_id, wan))
    # Physical ports (discovered at startup — if none found, returns empty list)
    for idx in coordinator._port_indices:
        entities.append(
            ER605SnmpPortLinkSensor(
                coordinator, entry_id, idx, coordinator._port_descrs.get(idx, str(idx))
            )
        )
    return entities
