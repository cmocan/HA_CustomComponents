"""Diagnostics support for the TP-Link ER605 integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .data import ER605ConfigEntry

TO_REDACT = {CONF_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ER605ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry (password redacted)."""
    runtime     = entry.runtime_data
    coordinator = runtime.coordinator
    device_info = runtime.device_info
    data        = coordinator.data

    wan_summary = [
        {
            "name":    i.name,
            "label":   i.label,
            "is_up":   i.is_up,
            "proto":   i.proto,
            "ip":      i.ip,
            "gateway": i.gateway,
        }
        for i in data.wan_interfaces
    ] if data else []

    port_summary = [
        {
            "port":      p.port,
            "connected": p.connected,
            "speed":     p.speed,
        }
        for p in data.physical_ports
    ] if data else []

    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "device": {
            "model":      device_info.model,
            "hw_version": device_info.hw_version,
            "fw_version": device_info.fw_version,
            "unique_id":  device_info.unique_id,
            "active_wans": device_info.active_wan_indices,
        },
        "last_poll": {
            "uptime_seconds":   data.uptime_seconds if data else None,
            "cpu_avg":          data.system.cpu_avg if data else None,
            "mem_percent":      data.system.mem_percent if data else None,
            "wan_interfaces":   wan_summary,
            "physical_ports":   port_summary,
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
        },
    }
