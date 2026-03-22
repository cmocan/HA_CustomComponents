"""Device tracker platform for the ISP Routers integration."""
from __future__ import annotations

from homeassistant.components.device_tracker import ScannerEntity, SourceType
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import IspRoutersCoordinator
from .data import ConnectedDevice, IspRoutersConfigEntry
from .entity import IspRoutersEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IspRoutersConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: IspRoutersCoordinator = entry.runtime_data.coordinator
    if not coordinator.strategy.supports_device_tracker:
        return

    tracked_macs: set[str] = set()

    def _add_new_devices() -> None:
        if not coordinator.data:
            return
        new = [
            IspRoutersTrackedDevice(coordinator, entry.entry_id, dev)
            for dev in coordinator.data.connected_devices
            if dev.mac not in tracked_macs
        ]
        if new:
            tracked_macs.update(dev.mac for dev in coordinator.data.connected_devices)
            async_add_entities(new)

    # Register devices from the first poll and subscribe for future updates.
    _add_new_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_devices))


class IspRoutersTrackedDevice(IspRoutersEntity, ScannerEntity):
    """One tracked device per ConnectedDevice from the last poll."""

    def __init__(
        self,
        coordinator: IspRoutersCoordinator,
        entry_id: str,
        device: ConnectedDevice,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._mac = device.mac
        self._attr_unique_id = f"{entry_id}_{device.mac}"
        self._attr_name = device.hostname or device.mac

    @property
    def source_type(self) -> SourceType:
        return SourceType.ROUTER

    @property
    def is_connected(self) -> bool:
        device = self._find_device()
        return device.is_active if device else False

    @property
    def ip_address(self) -> str | None:
        device = self._find_device()
        return device.ip if device else None  # internal LAN IP only — RFC 1918

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def hostname(self) -> str | None:
        device = self._find_device()
        return device.hostname if device else None

    def _find_device(self) -> ConnectedDevice | None:
        if not self.coordinator.data:
            return None
        for dev in self.coordinator.data.connected_devices:
            if dev.mac == self._mac:
                return dev
        return None
