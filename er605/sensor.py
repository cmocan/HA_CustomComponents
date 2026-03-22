"""Sensor platform for the TP-Link ER605 integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
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

    from .const import IPSTATS_TOP_N, CONF_ENABLE_IPSTATS, DEFAULT_ENABLE_IPSTATS
    from .coordinator import ER605Coordinator
    from .data import ER605IfstatEntry, ER605IpstatEntry, ER605RouterData, ER605RuntimeData
    from .dns_resolver import _is_private
    from .entity import ER605Entity
    from .snmp_coordinator import ER605SnmpCoordinator, build_wan_stubs
    from .snmp_data import SnmpRouterData, SnmpWanData
    from .snmp_entity import ER605SnmpEntity
except ImportError:
    from const import IPSTATS_TOP_N, CONF_ENABLE_IPSTATS, DEFAULT_ENABLE_IPSTATS  # type: ignore[no-redef]
    from coordinator import ER605Coordinator  # type: ignore[no-redef]
    from data import ER605IfstatEntry, ER605IpstatEntry, ER605RouterData, ER605RuntimeData  # type: ignore[no-redef]
    from entity import ER605Entity  # type: ignore[no-redef]
    from snmp_coordinator import ER605SnmpCoordinator, build_wan_stubs  # type: ignore[no-redef]
    from snmp_data import SnmpRouterData, SnmpWanData  # type: ignore[no-redef]
    from snmp_entity import ER605SnmpEntity  # type: ignore[no-redef]
    from dns_resolver import _is_private  # type: ignore[no-redef]

    class SensorEntity:  # type: ignore[no-redef]
        pass

    from dataclasses import dataclass as _dataclass, field as _field

    @_dataclass(frozen=True, kw_only=True)
    class SensorEntityDescription:  # type: ignore[no-redef]
        key:  str = ""
        name: str = ""
        icon: str | None = None
        native_unit_of_measurement: object = None
        device_class: object = None
        state_class: object = None
        suggested_display_precision: int | None = None
        options: list | None = None

    class SensorDeviceClass:  # type: ignore[no-redef]
        ENUM = "enum"
        DURATION = "duration"
        DATA_RATE = "data_rate"
        DATA_SIZE = "data_size"

    class SensorStateClass:  # type: ignore[no-redef]
        MEASUREMENT = "measurement"
        TOTAL_INCREASING = "total_increasing"

    class ConfigEntry:  # type: ignore[no-redef]
        pass

    class HomeAssistant:  # type: ignore[no-redef]
        pass

    class AddEntitiesCallback:  # type: ignore[no-redef]
        pass

    PERCENTAGE = "%"

    class UnitOfDataRate:  # type: ignore[no-redef]
        KILOBYTES_PER_SECOND = "kB/s"
        MEGABITS_PER_SECOND  = "Mbit/s"

    class UnitOfInformation:  # type: ignore[no-redef]
        GIGABYTES = "GB"

    class UnitOfTime:  # type: ignore[no-redef]
        SECONDS = "s"

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

IPSTATS_SENSOR_KEYS: frozenset[str] = frozenset({"lan_clients_total", "lan_clients_active"})


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
        async_add_entities(_build_snmp_sensors(runtime.coordinator, entry.entry_id))
        return
    # existing HTTP path unchanged below
    runtime: ER605RuntimeData = entry.runtime_data
    coordinator: ER605Coordinator = runtime.coordinator
    enable_ipstats = entry.options.get(CONF_ENABLE_IPSTATS, DEFAULT_ENABLE_IPSTATS)
    dev_info = runtime.device_info

    entities: list[ER605Entity] = []

    # System-wide sensors
    for desc in SYSTEM_SENSORS:
        if not enable_ipstats and desc.key in IPSTATS_SENSOR_KEYS:
            continue
        entities.append(ER605SystemSensor(coordinator, entry.entry_id, desc))

    # Per-WAN sensors — driven by device_info.wan_ports (stable, always complete)
    active_indices = set(dev_info.active_wan_indices)
    wan_ports = [
        p for p in dev_info.wan_ports if p.index in active_indices
    ]

    for port in wan_ports:
        wan_name = f"WAN{port.index}"          # t_name used by interface API
        wan_key  = wan_name.lower()             # "wan1", "wan2"
        label    = port.name                    # "WAN1", "WAN/LAN2"

        entities.extend([
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_ip",
                    name          = f"{label} IP Address",
                    icon          = "mdi:ip-network",
                    interface_key = wan_name,
                ),
            ),
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_gateway",
                    name          = f"{label} Gateway",
                    icon          = "mdi:router-network",
                    interface_key = wan_name,
                ),
            ),
            ER605WANSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_dns",
                    name          = f"{label} DNS",
                    icon          = "mdi:dns",
                    interface_key = wan_name,
                ),
            ),
            ER605IPv6Sensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key           = f"{wan_key}_ipv6_address",
                    name          = f"{label} IPv6 Address",
                    icon          = "mdi:ip-network-outline",
                    interface_key = wan_name,
                ),
            ),
        ])
        entities.append(
            ER605WanConnectionTypeSensor(
                coordinator, entry.entry_id, wan_name, label
            )
        )
        entities.append(
            ER605WanRoleSensor(
                coordinator, entry.entry_id, wan_name, label
            )
        )

    # One WAN Mode sensor per config entry
    entities.append(ER605WanModeSensor(coordinator, entry.entry_id))

    # One Top External Destinations sensor per config entry
    if enable_ipstats:
        entities.append(ER605TopExternalDestinationsSensor(coordinator, entry.entry_id))

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
                ),
            )
        )

    # Per-zone interface traffic sensors
    for stat in coordinator.data.ifstat:
        z      = stat.zone                    # e.g. "WAN1"
        zk     = z.lower()                   # e.g. "wan1"
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
                    native_unit_of_measurement = UnitOfInformation.GIGABYTES,
                    device_class               = SensorDeviceClass.DATA_SIZE,
                    state_class                = SensorStateClass.TOTAL_INCREASING,
                    zone_key   = z,
                    zone_field = "rx_bytes",
                ),
            ),
            ER605IfstatSensor(
                coordinator, entry.entry_id,
                ER605SensorDescription(
                    key        = f"ifstat_{zk}_tx_bytes",
                    name       = f"{z} Total Uploaded",
                    icon       = "mdi:upload",
                    native_unit_of_measurement = UnitOfInformation.GIGABYTES,
                    device_class               = SensorDeviceClass.DATA_SIZE,
                    state_class                = SensorStateClass.TOTAL_INCREASING,
                    zone_key   = z,
                    zone_field = "tx_bytes",
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
        self._cached_attrs: dict[str, Any] | None = None
        self._cached_gen: int = -1

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
            pool_fn = lambda: self.coordinator.data.lan_clients  # noqa: E731
        elif key == "lan_clients_active":
            pool_fn = lambda: self.coordinator.data.active_lan_clients  # noqa: E731
        else:
            return None

        gen = self.coordinator.ipstats_generation
        if self._cached_gen == gen and self._cached_attrs is not None:
            return self._cached_attrs

        pool = pool_fn()
        top = sorted(pool, key=lambda e: e.rx_bps + e.tx_bps, reverse=True)[:IPSTATS_TOP_N]
        external_hosts = self.coordinator.data.external_hosts
        self._cached_attrs = {
            "clients": [
                {
                    "addr":     e.addr,
                    "hostname": external_hosts.get(e.addr) or e.hostname,
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
        self._cached_gen = gen
        return self._cached_attrs


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
    def native_value(self) -> float | int | None:
        stat: ER605IfstatEntry | None = self.coordinator.data.ifstat_zone(
            self.entity_description.zone_key
        )
        if stat is None:
            return None
        val = getattr(stat, self.entity_description.zone_field, None)
        if val is None:
            return None
        # Bytes fields are stored as raw bytes; convert to GB for display.
        if self.entity_description.zone_field.endswith("_bytes"):
            return round(val / 1_000_000_000, 3)
        return val


# ─────────────────────────────────────────────────────────────────────────────
# HTTP WAN health entities
# ─────────────────────────────────────────────────────────────────────────────


class ER605WanConnectionTypeSensor(ER605Entity, SensorEntity):
    """WAN connection protocol type (dhcp / static / pppoe / …) — HTTP only, Tier 2."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options      = ["dhcp", "static", "pppoe", "l2tp", "pptp", "mobile"]

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        wan_name: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._wan_name = wan_name
        self._attr_unique_id = f"{entry_id}_{wan_name.lower()}_connection_type"
        self._attr_name = f"{label} Connection Type"

    @property
    def native_value(self) -> str | None:
        data: ER605RouterData | None = self.coordinator.data
        if data is None:
            return None
        iface = data.interface(self._wan_name)
        return iface.proto.lower() if iface and iface.proto else None


class ER605WanRoleSensor(ER605Entity, SensorEntity):
    """WAN failover role (primary / backup / balanced) — HTTP only, Tier 2."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options      = ["primary", "backup", "balanced"]

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        wan_name: str,
        label: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._wan_name = wan_name
        self._attr_unique_id = f"{entry_id}_{wan_name.lower()}_role"
        self._attr_name = f"{label} Role"

    @property
    def native_value(self) -> str | None:
        data: ER605RouterData | None = self.coordinator.data
        if data is None:
            return None
        iface = data.interface(self._wan_name)
        return iface.role if iface else None


class ER605WanModeSensor(ER605Entity, SensorEntity):
    """System WAN policy mode (load_balance / failover / single) — HTTP only, Tier 2.

    One entity per config entry (not per WAN).
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options      = ["load_balance", "failover", "single"]
    _attr_icon         = "mdi:wan"

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_wan_mode"
        self._attr_name = "WAN Mode"

    @property
    def native_value(self) -> str | None:
        data: ER605RouterData | None = self.coordinator.data
        return data.wan_policy if data else None


class ER605TopExternalDestinationsSensor(ER605Entity, SensorEntity):
    """Top external destinations by traffic (Tier 3, one per config entry)."""

    _attr_icon        = "mdi:earth"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: ER605Coordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_top_external_destinations"
        self._attr_name      = "Top External Destinations"

    @property
    def native_value(self) -> int | None:
        """Count of unique external hosts ever resolved."""
        data: ER605RouterData | None = self.coordinator.data
        if data is None:
            return None
        return len(data.external_hosts)

    @property
    def extra_state_attributes(self) -> dict:
        data: ER605RouterData | None = self.coordinator.data
        if data is None:
            return {}
        external = data.external_hosts
        candidates = [
            e for e in data.ipstats
            if not _is_private(e.addr)
        ]
        candidates.sort(key=lambda e: e.rx_bytes + e.tx_bytes, reverse=True)
        top = candidates[:20]
        return {
            "top_destinations": [
                {
                    "host":     external.get(e.addr) or e.hostname or e.addr,
                    "ip":       e.addr,
                    "total_gb": round((e.rx_bytes + e.tx_bytes) / 1_000_000_000, 3),
                }
                for e in top
            ]
        }


# ─────────────────────────────────────────────────────────────────────────────
# SNMP sensor entities
# ─────────────────────────────────────────────────────────────────────────────


class ER605SnmpStaticSensor(ER605SnmpEntity, SensorEntity):
    """Sensor for Tier 3 static data (firmware, hostname)."""

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._key = key
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_name = name
        self._attr_icon = "mdi:router-network"

    @property
    def native_value(self) -> str | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        mapping = {
            "firmware": data.sys_descr,
            "hostname": data.sys_name,
        }
        return mapping.get(self._key)


class ER605SnmpUptimeSensor(ER605SnmpEntity, SensorEntity):
    """Uptime sensor (Tier 2)."""

    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class  = SensorDeviceClass.DURATION
    _attr_state_class   = SensorStateClass.TOTAL_INCREASING
    _attr_icon          = "mdi:clock-outline"

    def __init__(self, coordinator: ER605SnmpCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_snmp_uptime"
        self._attr_name = "Uptime"

    @property
    def native_value(self) -> float | None:
        data: SnmpRouterData | None = self.coordinator.data
        return data.uptime_seconds if data else None


class ER605SnmpWanIpSensor(ER605SnmpEntity, SensorEntity):
    """WAN IP address sensor (Tier 2)."""

    _attr_icon = "mdi:ip"

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        wan: SnmpWanData,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._iface_slug = wan.iface_slug
        self._attr_unique_id = f"{entry_id}_snmp_wan_{wan.iface_slug}_ip"
        self._attr_name = f"{wan.if_label} IP"

    @property
    def native_value(self) -> str | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        wan = next((w for w in data.wan if w.iface_slug == self._iface_slug), None)
        return wan.ip if wan else None


class ER605SnmpRateSensor(ER605SnmpEntity, SensorEntity):
    """WAN RX or TX rate sensor in Mbit/s (Tier 1)."""

    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_state_class   = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        wan: SnmpWanData,
        direction: str,  # "rx" or "tx"
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._iface_slug = wan.iface_slug
        self._direction  = direction
        self._attr_unique_id = f"{entry_id}_snmp_wan_{wan.iface_slug}_{direction}_rate"
        self._attr_name = f"{wan.if_label} {'Download' if direction == 'rx' else 'Upload'} Rate"
        self._attr_icon = "mdi:download-network" if direction == "rx" else "mdi:upload-network"

    @property
    def native_value(self) -> float | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        wan = next((w for w in data.wan if w.iface_slug == self._iface_slug), None)
        if wan is None:
            return None
        return wan.rx_rate_mbps if self._direction == "rx" else wan.tx_rate_mbps


class ER605SnmpBytesSensor(ER605SnmpEntity, SensorEntity):
    """WAN cumulative bytes sensor (TOTAL_INCREASING, Tier 1).

    Reports the raw ifHCInOctets / ifHCOutOctets 64-bit counter (bytes)
    converted to gigabytes (÷ 1 000 000 000).  The 64-bit counter tops out
    at ~18.4 × 10⁹ GB — no float64 overflow possible.
    """

    _attr_native_unit_of_measurement = UnitOfInformation.GIGABYTES
    _attr_device_class  = SensorDeviceClass.DATA_SIZE
    _attr_state_class   = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        coordinator: ER605SnmpCoordinator,
        entry_id: str,
        wan: SnmpWanData,
        direction: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._iface_slug = wan.iface_slug
        self._direction  = direction
        self._attr_unique_id = f"{entry_id}_snmp_wan_{wan.iface_slug}_{direction}_bytes"
        self._attr_name = f"{wan.if_label} {'Received' if direction == 'rx' else 'Sent'} Bytes"

    @property
    def native_value(self) -> float | None:
        data: SnmpRouterData | None = self.coordinator.data
        if data is None:
            return None
        wan = next((w for w in data.wan if w.iface_slug == self._iface_slug), None)
        if wan is None:
            return None
        octets = wan.hc_in_octets if self._direction == "rx" else wan.hc_out_octets
        return round(octets / 1_000_000_000, 3)


class ER605SnmpMemorySensor(ER605SnmpEntity, SensorEntity):
    """Memory utilization % (Tier 1)."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class   = SensorStateClass.MEASUREMENT
    _attr_icon          = "mdi:memory"

    def __init__(self, coordinator: ER605SnmpCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_snmp_memory_usage"
        self._attr_name = "Memory Usage"

    @property
    def native_value(self) -> float | None:
        data: SnmpRouterData | None = self.coordinator.data
        return data.memory_pct if data else None


def _build_snmp_sensors(
    coordinator: ER605SnmpCoordinator, entry_id: str
) -> list[SensorEntity]:
    """Build all SNMP sensor entities. WAN entities created per discovered WAN."""
    entities: list = []
    # Static
    entities.append(ER605SnmpStaticSensor(coordinator, entry_id, "firmware", "Firmware"))
    entities.append(ER605SnmpStaticSensor(coordinator, entry_id, "hostname", "Hostname"))
    # Uptime (Tier 2)
    entities.append(ER605SnmpUptimeSensor(coordinator, entry_id))
    # WAN sensors — build stubs from discovery lists (populated by async_setup,
    # available before the first poll cycle completes)
    for wan in build_wan_stubs(coordinator):
        entities.append(ER605SnmpWanIpSensor(coordinator, entry_id, wan))
        entities.append(ER605SnmpRateSensor(coordinator, entry_id, wan, "rx"))
        entities.append(ER605SnmpRateSensor(coordinator, entry_id, wan, "tx"))
        entities.append(ER605SnmpBytesSensor(coordinator, entry_id, wan, "rx"))
        entities.append(ER605SnmpBytesSensor(coordinator, entry_id, wan, "tx"))
    # Memory
    entities.append(ER605SnmpMemorySensor(coordinator, entry_id))
    return entities
