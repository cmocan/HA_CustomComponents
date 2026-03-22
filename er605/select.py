"""Select platform for the TP-Link ER605 integration — manual WAN override."""

from __future__ import annotations

import logging
import time
from typing import Any

try:
    from homeassistant.components.select import SelectEntity
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .const import DOMAIN, PROTOCOL_SNMP
    from .coordinator import ER605Coordinator
    from .data import ER605DeviceInfo, ER605RuntimeData
    from .entity import ER605Entity
except ImportError:
    from const import DOMAIN, PROTOCOL_SNMP  # type: ignore[no-redef]
    from coordinator import ER605Coordinator  # type: ignore[no-redef]
    from data import ER605DeviceInfo, ER605RuntimeData  # type: ignore[no-redef]
    from entity import ER605Entity  # type: ignore[no-redef]

    class SelectEntity:  # type: ignore[misc,assignment]
        """Stub SelectEntity for test environments without homeassistant installed."""

    class ER605Entity:  # type: ignore[misc,assignment]
        """Stub ER605Entity for test environments without homeassistant installed."""

        def __init__(self, coordinator, entry_id, *a, **kw):
            self.coordinator = coordinator

    class HomeAssistantError(Exception):  # type: ignore[no-redef]
        pass

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0

_UNSET = object()
_OPTION_AUTO = "Auto (Failover)"


# ── Option building (pure function, testable without HA) ─────────────────────

def _build_wan_options(
    device_info: ER605DeviceInfo,
) -> tuple[dict[str, str | None], dict[str | None, str]]:
    """Return (option_string → wan_name, wan_name → option_string) mappings.

    active_wan_indices contains integers as returned by the API (wan_numbers field);
    port.index is a string. str() coercion handles the type mismatch.
    """
    active = {str(w) for w in device_info.active_wan_indices}
    option_by_name: dict[str, str | None] = {_OPTION_AUTO: None}

    for port in device_info.wan_ports:
        if str(port.index) not in active:
            continue
        wan_name = f"WAN{port.index}"
        option   = f"Force {wan_name}"
        option_by_name[option] = wan_name

    name_by_option: dict[str | None, str] = {v: k for k, v in option_by_name.items()}
    return option_by_name, name_by_option


# ── Setup ─────────────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    if entry.data.get("protocol") == PROTOCOL_SNMP:
        return  # read-only SNMP — no write entity

    runtime: ER605RuntimeData = entry.runtime_data
    coordinator: ER605Coordinator = runtime.coordinator
    medium_interval = coordinator._medium_poll_interval

    async_add_entities([
        ER605WANOverrideSelect(
            coordinator,
            entry.entry_id,
            runtime.device_info,
            medium_interval,
        )
    ])


# ── Entity ────────────────────────────────────────────────────────────────────

class ER605WANOverrideSelect(ER605Entity, SelectEntity):
    """Select entity to manually force all traffic through a specific WAN."""

    _attr_icon = "mdi:swap-horizontal"

    def __init__(
        self,
        coordinator: ER605Coordinator,
        entry_id: str,
        device_info: ER605DeviceInfo,
        medium_poll_interval: int,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_wan_override"
        self._attr_name = "WAN Override"

        self._wan_name_by_option, self._option_by_wan_name = _build_wan_options(device_info)
        self._attr_options = list(self._wan_name_by_option.keys())

        # Optimistic state: stores internal WAN name after a successful write
        # until the medium-tier poll confirms the router state.
        self._optimistic_wan: Any = _UNSET
        self._optimistic_set_at: float = 0.0
        # Timeout: 2× medium interval; fallback to 120 s if interval is 0 (manual-only)
        self._optimistic_timeout: float = (
            medium_poll_interval * 2 if medium_poll_interval > 0 else 120.0
        )

    # ── CoordinatorEntity override ────────────────────────────────────────────

    def _handle_coordinator_update(self) -> None:
        """Clear optimistic state once coordinator confirms the router state."""
        if self._optimistic_wan is not _UNSET:
            data = self.coordinator.data
            if data is not None and data.wan_override == self._optimistic_wan:
                self._optimistic_wan = _UNSET
        super()._handle_coordinator_update()

    # ── State ─────────────────────────────────────────────────────────────────

    @property
    def current_option(self) -> str | None:
        # Expire stale optimistic state
        if self._optimistic_wan is not _UNSET:
            if time.monotonic() - self._optimistic_set_at > self._optimistic_timeout:
                _LOGGER.warning(
                    "WAN override optimistic state expired without coordinator "
                    "confirmation — reverting to polled state"
                )
                self._optimistic_wan = _UNSET
            else:
                return self._option_by_wan_name.get(self._optimistic_wan, _OPTION_AUTO)

        if self.coordinator.data is None:
            return _OPTION_AUTO
        return self._option_by_wan_name.get(
            self.coordinator.data.wan_override, _OPTION_AUTO
        )

    # ── Action ────────────────────────────────────────────────────────────────

    async def async_select_option(self, option: str) -> None:
        """Called by HA when the user picks a new option in the UI."""
        wan_name = self._wan_name_by_option.get(option)
        # wan_name is None for "Auto (Failover)", str for "Force WANx"
        await self.coordinator.async_set_wan_override(wan_name)
        # Only set optimistic state after a successful write (no exception above)
        self._optimistic_wan = wan_name
        self._optimistic_set_at = time.monotonic()
        self.async_write_ha_state()
