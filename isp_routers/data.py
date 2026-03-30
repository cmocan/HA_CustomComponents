"""Unified data contracts for the ISP Routers integration.

All platforms read from RouterData. Fields are None when a router does not
support them — the entity descriptor controls whether a sensor is registered.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .coordinator import IspRoutersCoordinator

try:
    from homeassistant.config_entries import ConfigEntry
except ImportError:
    class ConfigEntry:  # type: ignore[no-redef]
        def __class_getitem__(cls, item): return cls


@dataclass
class ConnectedDevice:
    """A single device seen by the router's DHCP/ARP table."""
    mac: str            # normalized: lowercase, colon-separated (aa:bb:cc:dd:ee:ff)
    ip: str | None      # internal LAN IP only — RFC 1918; never a WAN/public address
    hostname: str | None
    is_active: bool
    network_type: str | None   # "LAN" / "WLAN" / None
    port: str | None           # LAN port number or SSID name


@dataclass
class WanStatus:
    """State of one WAN connection."""
    name: str           # "WAN", "WAN1", etc.
    is_up: bool
    ip: str | None      # external/WAN IP address
    gateway: str | None
    dns1: str | None
    dns2: str | None    # secondary DNS — None if router only reports one


@dataclass
class LanPort:
    """State of one physical LAN port (Arris only)."""
    port_id: int        # 1-based
    is_active: bool
    bitrate: str | None  # e.g. "1 Gbps"; None when inactive
    rx_bytes: int | None = None  # cumulative bytes received since boot
    tx_bytes: int | None = None  # cumulative bytes transmitted since boot


@dataclass
class DslChannel:
    """One DOCSIS channel (Arris only). Empty list on all other routers."""
    channel_id: int
    direction: str           # "downstream" / "upstream"
    frequency_mhz: float | None
    power_dbmv: float | None  # signal power — same field used for both directions
    snr_db: float | None      # downstream only; always None for upstream channels
    locked: bool


@dataclass
class RouterData:
    """Complete poll result. Replaced wholesale each coordinator update cycle."""

    # ── Universal (all routers) ──────────────────────────────────────────
    model: str | None
    firmware: str | None
    uptime_seconds: int | None
    connected_devices: list[ConnectedDevice] = field(default_factory=list)
    wan_status: list[WanStatus] = field(default_factory=list)

    # ── Arris-specific ───────────────────────────────────────────────────
    firewall_enabled: bool | None = None
    lan_network: str | None = None          # e.g. "192.168.10.0/24"
    lan_ports: list[LanPort] = field(default_factory=list)
    docsis_channels: list[DslChannel] = field(default_factory=list)
    voip_lines: int | None = None
    serial_number: str | None = None
    hw_version: str | None = None
    wan_mac: str | None = None
    lan_mac: str | None = None
    wifi_24g_enabled: bool | None = None
    wifi_5g_enabled: bool | None = None
    wifi_24g_ssid: str | None = None
    wifi_5g_ssid: str | None = None
    wifi_24g_channel: str | None = None
    wifi_5g_channel: str | None = None
    wifi_24g_bandwidth: str | None = None
    wifi_5g_bandwidth: str | None = None
    wan_ipv6_link_local: str | None = None
    docsis_status: str | None = None        # e.g. "DOCSIS Online"
    gateway_mode: str | None = None         # e.g. "Ipv4"
    cm_operational: bool | None = None      # True when cable modem is operational

    # ── ZTE-specific ─────────────────────────────────────────────────────
    cpu_usage: int | None = None        # primary core usage, percent
    mem_usage: int | None = None        # memory usage, percent
    firewall_level: str | None = None   # "Low" / "Middle" / "High"
    wifi_enabled: bool | None = None    # True if any radio is on

    # ── Internal bookkeeping — never expose in entity state/attributes ───
    poll_monotonic: float = 0.0   # time.monotonic() at fetch completion


@dataclass
class IspRoutersRuntimeData:
    """Stored in entry.runtime_data after successful async_setup_entry."""
    coordinator: IspRoutersCoordinator


# Type alias — used for annotation only; defined at runtime as plain ConfigEntry
# to avoid subscript issues when running outside HA.
try:
    from homeassistant.config_entries import ConfigEntry as _CE
    IspRoutersConfigEntry = _CE  # HA provides the generic version
except ImportError:
    IspRoutersConfigEntry = ConfigEntry  # type: ignore[misc]
