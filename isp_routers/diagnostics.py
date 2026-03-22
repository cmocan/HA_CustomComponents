"""Diagnostics support for the ISP Routers integration."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .data import IspRoutersRuntimeData

TO_REDACT = {"password", "ip", "gateway", "dns1", "dns2", "mac", "wan_ip"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    runtime: IspRoutersRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data

    return async_redact_data(
        {
            "entry_data": dict(entry.data),
            "router_type": entry.data.get("router_type"),
            "router_data": {
                "model": data.model if data else None,
                "firmware": data.firmware if data else None,
                "uptime_seconds": data.uptime_seconds if data else None,
                "connected_device_count": len(data.connected_devices) if data else 0,
                "wan_status": [
                    {"name": w.name, "is_up": w.is_up, "ip": w.ip}
                    for w in (data.wan_status if data else [])
                ],
                "docsis_channel_count": len(data.docsis_channels) if data else 0,
            },
        },
        TO_REDACT,
    )
