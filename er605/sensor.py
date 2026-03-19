"""Sensor platform for the TP-Link ER605 integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import IPSTATS_TOP_N
from .coordinator import ER605Coordinator
from .data import ER605IfstatEntry, ER605IpstatEntry, ER605RouterData, ER605RuntimeData
from .entity import ER605Entity

PARALLEL_UPDATES = 0


# ── Entity description extensions ─────────────────────────────────────────────

@dataclass(frozen=True, kw_only=True)
class ER605SensorDescription(SensorEntityDescription):
    """Extends SensorEntityDescription with a value accessor."""

    interface_key: str = ""   # WAN t_name when this is a per-WAN sensor
    port_key:      str = ""   # physical port number when port sensor
    zone_key:      str = ""   # ifstat zone ("WAN1", "LAN1", …)
    zone_field:    str = ""   # field name within ER605IfstatEntry


# ── System-wide sensor descriptors (created once) ─────────────────────────────

SYSTEM_SENSORS: tuple[ER605SensorDescription, ...] = (
    ER605SensorDescription(
        key         = "uptime",
        name        = "Uptime",
        native_unit_of_measurement = UnitOfTime.SECONDS,
        device_class               = SensorDeviceClass.DURATION,
        state_class                = SensorStateClass.TOTAL_INCREASING,
    ),
    ER605SensorDescription(
        key         = "cpu_usage",
        name        = "CPU Usage",
        native_unit_of_measurement = PERCENTAGE,
        state_class                = SensorStateClass.MEASUREMENT,
        suggested_display_precision = 0,
    ),
    ER605SensorDescription(
        key         = "memory_usage",
        name        = "Memory Usage",
        native_unit_of_measurement = PERCENTAGE,
        state_class                = SensorStateClass.MEASUREMENT,
        suggested_display_precision = 0,
    ),
    ER605SensorDescription(
        key         = "active_wan_count",
        name        = "Active WAN Count",
        state_class = SensorStateClass.MEASUREMENT,
        icon        = "mdi:wan",
    ),
    ER605SensorDescription(
        key         = "lan_clients_total",
        name        = "LAN Clients Total",
        state_class = SensorStateClass.MEASUREMENT,
        icon        = "mdi:devices",
    ),
    ER605SensorDescription(
        key         = "lan_clients_active",
        name        = "LAN Clients Active",
        state_class = SensorStateClass.MEASUREMENT,
        icon        = "mdi:lan-connect",
    ),
)


# ── Setup ─────────────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    runtime: ER605RuntimeData = entry.runtime_data
    coordinator: ER605Coordinator = runtime.coordinator
    dev_info = runtime.device_info

    entities: list[ER605Entity] = []

    # System-wide sensors
    for desc in SYSTEM_SENSORS:
        entities.append(ER605SystemSensor(coordinator, entry.entry_id, desc))

    # Per-WAN sensors
    for iface in coordinator.data.wan_interfaces:
        wan_key = iface.entity_key   # e.g. "wan1"
        label   = iface.label        # e.g. "WAN1"

        entities.extend([
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_ip",
                    name          = f"{label} IP Address",
                    icon          = "mdi:ip-network",
                    interface_key = iface.name,
                ),
            ),
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_gateway",
                    name          = f"{label} Gateway",
                    icon          = "mdi:router-network",
                    interface_key = iface.name,
                    entity_registry_enabled_default = False,
                ),
            ),
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_dns",
                    name          = f"{label} DNS",
                    icon          = "mdi:dns",
                    interface_key = iface.name,
                    entity_registry_enabled_default = False,
                ),
            ),
            ER605IPv6Sensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_ipv6_address",
                    name          = f"{label} IPv6 Address",
                    icon          = "mdi:ip-network-outline",
                    interface_key = iface.name,
                    entity_registry_enabled_default = False,
                ),
            ),
        ])

    # Per-physical-port speed sensor (disabled by default)
    for port in coordinator.data.physical_ports:
        entities.append(
            ER605PortSpeedSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key      = f"port_{port.port}_speed",
                    name     = f"Port {port.port} Speed",
                    icon     = "mdi:ethernet",
                    port_key = port.port,
                    entity_registry_enabled_default = False,
                ),
            )
        )

    # Per-zone interface traffic sensors
    for stat in coordinator.data.ifstat:
        z      = stat.zone                    # e.g. "WAN1"
        zk     = z.lower()                   # e.g. "wan1"
        is_wan = z.upper().startswith("WAN")  # enable totals for WAN, disable for LAN
        entities.extend([
            ER605IfstatSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key        = f"ifstat_{zk}_rx_bps",
                    name       = f"{z} Download Rate",
                    icon       = "mdi:download-network",
                    native_unit_of_measurement = UnitOfDataRate.KILOBYTES_PER_SECOND,
                    device_class               = SensorDeviceClass.DATA_RATE,
                    state_class                = SensorStateClass.MEASUREMENT,
                    zone_key   = z,
                    zone_field = "rx_bps",
                ),
            ),
            ER605IfstatSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key        = f"ifstat_{zk}_tx_bps",
                    name       = f"{z} Upload Rate",
                    icon       = "mdi:upload-network",
                    native_unit_of_measurement = UnitOfDataRate.KILOBYTES_PER_SECOND,
                    device_class               = SensorDeviceClass.DATA_RATE,
                    state_class                = SensorStateClass.MEASUREMENT,
                    zone_key   = z,
                    zone_field = "tx_bps",
                ),
            ),
            ER605IfstatSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key        = f"ifstat_{zk}_rx_bytes",
                    name       = f"{z} Total Downloaded",
                    icon       = "mdi:download",
                    native_unit_of_measurement = UnitOfInformation.BYTES,
                    device_class               = SensorDeviceClass.DATA_SIZE,
                    state_class                = SensorStateClass.TOTAL_INCREASING,
                    zone_key   = z,
                    zone_field = "rx_bytes",
                    entity_registry_enabled_default = is_wan,
                ),
            ),
            ER605IfstatSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key        = f"ifstat_{zk}_tx_bytes",
                    name       = f"{z} Total Uploaded",
                    icon       = "mdi:upload",
                    native_unit_of_measurement = UnitOfInformation.BYTES,
                    device_class               = SensorDeviceClass.DATA_SIZE,
                    state_class                = SensorStateClass.TOTAL_INCREASING,
                    zone_key   = z,
                    zone_field = "tx_bytes",
                    entity_registry_enabled_default = is_wan,
                ),
            ),
        ])

    async_add_entities(entities)


# ── Entity classes ────────────────────────────────────────────────────────────

class ER605SystemSensor(ER605Entity, SensorEntity):
    """System-wide sensor (uptime, CPU, memory, active WAN count)."""

    entity_description: ER605SensorDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605SensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> Any:
        data: ER605RouterData = self.coordinator.data
        key = self.entity_description.key
        if key == "uptime":
            return data.uptime_seconds
        if key == "cpu_usage":
            return data.system.cpu_avg
        if key == "memory_usage":
            return data.system.mem_percent
        if key == "active_wan_count":
            return len([i for i in data.wan_interfaces if i.is_up])
        if key == "lan_clients_total":
            return len(data.lan_clients)
        if key == "lan_clients_active":
            return len(data.active_lan_clients)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        key = self.entity_description.key
        if key == "lan_clients_total":
            pool = self.coordinator.data.lan_clients
        elif key == "lan_clients_active":
            pool = self.coordinator.data.active_lan_clients
        else:
            return None
        top = sorted(pool, key=lambda e: e.rx_bps + e.tx_bps, reverse=True)[:IPSTATS_TOP_N]
        return {
            "clients": [
                {
                    "addr":     e.addr,
                    "rx_bps":   e.rx_bps,
                    "tx_bps":   e.tx_bps,
                    "rx_bytes": e.rx_bytes,
                    "tx_bytes": e.tx_bytes,
                }
                for e in top
            ],
            "total_clients": len(pool),
            "top_n": IPSTATS_TOP_N,
        }


class ER605WANSensor(ER605Entity, SensorEntity):
    """Per-WAN sensor for IP, gateway, or DNS."""

    entity_description: ER605SensorDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605SensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> str | None:
        iface = self.coordinator.data.interface(
            self.entity_description.interface_key
        )
        if not iface:
            return None
        suffix = self.entity_description.key.split("_")[-1]
        if suffix == "ip":
            return iface.ip
        if suffix == "gateway":
            return iface.gateway
        if suffix == "dns":
            return iface.dns1
        return None


class ER605IPv6Sensor(ER605Entity, SensorEntity):
    """Per-WAN IPv6 address sensor."""

    entity_description: ER605SensorDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605SensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> str | None:
        ipv6 = self.coordinator.data.ipv6(self.entity_description.interface_key)
        return ipv6.ip6addr if ipv6 else None


class ER605PortSpeedSensor(ER605Entity, SensorEntity):
    """Physical port speed sensor."""

    entity_description: ER605SensorDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605SensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> str | None:
        for port in self.coordinator.data.physical_ports:
            if port.port == self.entity_description.port_key:
                return port.speed   # e.g. "1000M", or None when disconnected
        return None


class ER605IfstatSensor(ER605Entity, SensorEntity):
    """Per-zone interface traffic sensor (rx/tx rate or cumulative bytes)."""

    entity_description: ER605SensorDescription

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        description: ER605SensorDescription,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"

    @property
    def native_value(self) -> int | None:
        stat: ER605IfstatEntry | None = self.coordinator.data.ifstat_zone(
            self.entity_description.zone_key
        )
        if stat is None:
            return None
        return getattr(stat, self.entity_description.zone_field, None)
