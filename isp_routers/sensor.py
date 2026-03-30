"""Sensor platform for the ISP Routers integration."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IspRoutersCoordinator
from .data import DslChannel, IspRoutersConfigEntry, RouterData
from .entity import IspRoutersEntity
from .router_registry import ChannelSensorTemplate

_STATIC_VALUE_MAP: dict[str, Callable[[RouterData], object]] = {
    "uptime":                  lambda d: d.uptime_seconds,
    "connected_devices_total": lambda d: len(d.connected_devices),
    "active_devices":          lambda d: sum(1 for dev in d.connected_devices if dev.is_active),
    "voip_lines":              lambda d: d.voip_lines,
    # WAN
    "wan_ip":                  lambda d: d.wan_status[0].ip if d.wan_status else None,
    "wan_gateway":             lambda d: d.wan_status[0].gateway if d.wan_status else None,
    "wan_dns1":                lambda d: d.wan_status[0].dns1 if d.wan_status else None,
    # LAN
    "lan_network":             lambda d: d.lan_network,
    "lan_port_1_speed":        lambda d: next((p.bitrate for p in d.lan_ports if p.port_id == 1), None),
    "lan_port_2_speed":        lambda d: next((p.bitrate for p in d.lan_ports if p.port_id == 2), None),
    "lan_port_3_speed":        lambda d: next((p.bitrate for p in d.lan_ports if p.port_id == 3), None),
    "lan_port_4_speed":        lambda d: next((p.bitrate for p in d.lan_ports if p.port_id == 4), None),
    # WAN secondary DNS
    "wan_dns2":                lambda d: d.wan_status[0].dns2 if d.wan_status else None,
    # ZTE: CPU / memory usage
    "cpu_usage":               lambda d: d.cpu_usage,
    "mem_usage":               lambda d: d.mem_usage,
    # ZTE: firewall level
    "firewall_level":          lambda d: d.firewall_level,
    # ZTE: LAN port Rx/Tx bytes
    "lan_port_1_rx_bytes":     lambda d: next((p.rx_bytes for p in d.lan_ports if p.port_id == 1), None),
    "lan_port_1_tx_bytes":     lambda d: next((p.tx_bytes for p in d.lan_ports if p.port_id == 1), None),
    "lan_port_2_rx_bytes":     lambda d: next((p.rx_bytes for p in d.lan_ports if p.port_id == 2), None),
    "lan_port_2_tx_bytes":     lambda d: next((p.tx_bytes for p in d.lan_ports if p.port_id == 2), None),
    "lan_port_3_rx_bytes":     lambda d: next((p.rx_bytes for p in d.lan_ports if p.port_id == 3), None),
    "lan_port_3_tx_bytes":     lambda d: next((p.tx_bytes for p in d.lan_ports if p.port_id == 3), None),
    "lan_port_4_rx_bytes":     lambda d: next((p.rx_bytes for p in d.lan_ports if p.port_id == 4), None),
    "lan_port_4_tx_bytes":     lambda d: next((p.tx_bytes for p in d.lan_ports if p.port_id == 4), None),
    # Arris: device identity
    "serial_number":           lambda d: d.serial_number,
    "hw_version":              lambda d: d.hw_version,
    "wan_mac":                 lambda d: d.wan_mac,
    "lan_mac":                 lambda d: d.lan_mac,
    # Arris: WiFi
    "wifi_24g_ssid":           lambda d: d.wifi_24g_ssid,
    "wifi_5g_ssid":            lambda d: d.wifi_5g_ssid,
    "wifi_24g_channel":        lambda d: d.wifi_24g_channel,
    "wifi_5g_channel":         lambda d: d.wifi_5g_channel,
    "wifi_24g_bandwidth":      lambda d: d.wifi_24g_bandwidth,
    "wifi_5g_bandwidth":       lambda d: d.wifi_5g_bandwidth,
    # Arris: WAN IPv6
    "wan_ipv6_link_local":     lambda d: d.wan_ipv6_link_local,
    # Arris: DOCSIS / modem status
    "docsis_status":           lambda d: d.docsis_status,
    "gateway_mode":            lambda d: d.gateway_mode,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IspRoutersConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IspRoutersCoordinator = entry.runtime_data.coordinator
    strategy = coordinator.strategy
    entities: list[SensorEntity] = []

    # Static sensors driven by strategy.sensor_descs
    for desc in strategy.sensor_descs:
        value_fn = _STATIC_VALUE_MAP.get(desc.key)
        if value_fn is not None:
            entities.append(
                IspRoutersStaticSensor(coordinator, entry.entry_id, desc, value_fn)
            )

    # Dynamic DOCSIS channel sensors (Arris only — empty list on ZTE)
    if coordinator.data and strategy.channel_sensor_templates:
        for template in strategy.channel_sensor_templates:
            for channel in coordinator.data.docsis_channels:
                if (template.direction_filter is None
                        or template.direction_filter == channel.direction):
                    entities.append(
                        IspRoutersChannelSensor(
                            coordinator, entry.entry_id, template, channel
                        )
                    )

    async_add_entities(entities)


class IspRoutersStaticSensor(IspRoutersEntity, SensorEntity):
    """A static sensor reading one field from RouterData."""

    def __init__(
        self,
        coordinator: IspRoutersCoordinator,
        entry_id: str,
        desc: SensorEntityDescription,
        value_fn: Callable[[RouterData], object],
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = desc
        self._value_fn = value_fn
        self._attr_unique_id = f"{entry_id}_{desc.key}"

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        return self._value_fn(self.coordinator.data)


class IspRoutersChannelSensor(IspRoutersEntity, SensorEntity):
    """A per-DOCSIS-channel sensor expanded from a ChannelSensorTemplate."""

    def __init__(
        self,
        coordinator: IspRoutersCoordinator,
        entry_id: str,
        template: ChannelSensorTemplate,
        channel: DslChannel,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._template = template
        self._channel_id = channel.channel_id
        self._direction = channel.direction
        self._attr_unique_id = (
            f"{entry_id}_docsis_{channel.direction}_{channel.channel_id}_{template.key_suffix}"
        )
        self._attr_name = template.name_template.format(channel_id=channel.channel_id)
        self._attr_native_unit_of_measurement = template.unit
        self._attr_device_class = template.device_class

    @property
    def native_value(self):
        if not self.coordinator.data:
            return None
        for ch in self.coordinator.data.docsis_channels:
            if ch.channel_id == self._channel_id and ch.direction == self._direction:
                return self._template.value_fn(ch)
        return None
