"""Binary sensor platform for the ISP Routers integration."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IspRoutersCoordinator
from .data import DslChannel, IspRoutersConfigEntry, RouterData
from .entity import IspRoutersEntity
from .router_registry import ChannelBinarySensorTemplate

_STATIC_VALUE_MAP: dict[str, Callable[[RouterData], bool | None]] = {
    "wan_connected":    lambda d: d.wan_status[0].is_up if d.wan_status else None,
    "firewall_enabled": lambda d: d.firewall_enabled,
    "lan_port_1_active": lambda d: next((p.is_active for p in d.lan_ports if p.port_id == 1), None),
    "lan_port_2_active": lambda d: next((p.is_active for p in d.lan_ports if p.port_id == 2), None),
    "lan_port_3_active": lambda d: next((p.is_active for p in d.lan_ports if p.port_id == 3), None),
    "lan_port_4_active": lambda d: next((p.is_active for p in d.lan_ports if p.port_id == 4), None),
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IspRoutersConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IspRoutersCoordinator = entry.runtime_data.coordinator
    strategy = coordinator.strategy
    entities: list[BinarySensorEntity] = []

    # Static binary sensors
    for desc in strategy.binary_sensor_descs:
        value_fn = _STATIC_VALUE_MAP.get(desc.key)
        if value_fn is not None:
            entities.append(
                IspRoutersStaticBinarySensor(coordinator, entry.entry_id, desc, value_fn)
            )

    # Dynamic DOCSIS channel binary sensors (Arris only — empty list on ZTE)
    if coordinator.data and strategy.channel_binary_sensor_templates:
        for template in strategy.channel_binary_sensor_templates:
            for channel in coordinator.data.docsis_channels:
                if (template.direction_filter is None
                        or template.direction_filter == channel.direction):
                    entities.append(
                        IspRoutersChannelBinarySensor(
                            coordinator, entry.entry_id, template, channel
                        )
                    )

    async_add_entities(entities)


class IspRoutersStaticBinarySensor(IspRoutersEntity, BinarySensorEntity):
    """A static binary sensor reading one field from RouterData."""

    def __init__(
        self,
        coordinator: IspRoutersCoordinator,
        entry_id: str,
        desc: BinarySensorEntityDescription,
        value_fn: Callable[[RouterData], bool | None],
    ) -> None:
        super().__init__(coordinator, entry_id)
        self.entity_description = desc
        self._value_fn = value_fn
        self._attr_unique_id = f"{entry_id}_{desc.key}"

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        return self._value_fn(self.coordinator.data)


class IspRoutersChannelBinarySensor(IspRoutersEntity, BinarySensorEntity):
    """A per-DOCSIS-channel binary sensor expanded from a ChannelBinarySensorTemplate."""

    def __init__(
        self,
        coordinator: IspRoutersCoordinator,
        entry_id: str,
        template: ChannelBinarySensorTemplate,
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
        self._attr_device_class = template.device_class

    @property
    def is_on(self) -> bool | None:
        if not self.coordinator.data:
            return None
        for ch in self.coordinator.data.docsis_channels:
            if ch.channel_id == self._channel_id and ch.direction == self._direction:
                return self._template.value_fn(ch)
        return None
