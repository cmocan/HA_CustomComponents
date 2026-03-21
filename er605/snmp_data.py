# custom_components/er605/snmp_data.py
"""Data contracts for the TP-Link ER605 SNMP integration."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

try:
    from homeassistant.config_entries import ConfigEntry
except ImportError:
    class ConfigEntry:  # type: ignore[no-redef]
        """Stub for test environments without homeassistant installed."""
        def __class_getitem__(cls, item):
            return cls

if TYPE_CHECKING:
    from .snmp_coordinator import ER605SnmpCoordinator


@dataclass
class SnmpDeviceInfo:
    """Static device metadata fetched once during async_setup()."""

    model: str              # "ER605" — parsed from sysDescr
    fw_version: str         # "Build 20231201" — parsed from sysDescr
    sys_name: str           # sysName — configured hostname
    sys_contact: str        # sysContact
    sys_location: str       # sysLocation
    wan1_mac: str           # ifPhysAddress WAN1, lowercase no-dashes (unique_id)


@dataclass
class SnmpWanData:
    """Per-WAN interface state for one poll cycle."""

    slot: int               # 1-based position in discovered WAN list
    if_index: int           # SNMP ifTable row index (e.g. 1026)
    if_descr: str           # ifDescr (e.g. "default/eth0")
    if_label:   str          # display name: "WAN1", "WAN2", "WAN3", "WAN USB"
    iface_slug: str          # stable slug: "eth0", "eth1", "eth2", "usb0"
    ip: str | None          # from ipAddrTable; None when not assigned
    oper_status: int        # ifOperStatus: 1=up, 2=down
    admin_status: int       # ifAdminStatus: 1=up, 2=down
    link_speed_mbps: int    # ifHighSpeed
    hc_in_octets: int       # ifHCInOctets raw counter
    hc_out_octets: int      # ifHCOutOctets raw counter
    rx_rate_mbps: float | None  # calculated; None on first poll
    tx_rate_mbps: float | None  # calculated; None on first poll

    @property
    def is_up(self) -> bool:
        return self.oper_status == 1


@dataclass
class SnmpPortData:
    """Physical port state (one row from ifTable)."""

    if_index: int
    if_descr: str           # e.g. "default/eth0"
    oper_status: int        # 1=up, 2=down
    admin_status: int       # 1=up, 2=down
    high_speed: int         # ifHighSpeed (Mbps)


@dataclass
class SnmpRouterData:
    """Complete snapshot returned by one coordinator poll cycle."""

    wan: list[SnmpWanData]          # 0–4 entries depending on discovery
    ports: list[SnmpPortData]       # all physical ethernet ports
    uptime_seconds: float | None    # sysUpTime / 100; None if unavailable
    memory_pct: float | None        # hrStorageUsed/hrStorageSize*100; None if unavailable
    sys_descr: str | None           # sysDescr (Tier 3, may be None before first T3 poll)
    sys_name: str | None            # sysName (Tier 3)
    sys_contact: str | None         # sysContact (Tier 3)
    sys_location: str | None        # sysLocation (Tier 3)
    poll_timestamp: float           # time.monotonic() at poll start


@dataclass
class SnmpRuntimeData:
    """Stored in ConfigEntry.runtime_data for SNMP entries."""

    coordinator: ER605SnmpCoordinator
    device_info: SnmpDeviceInfo


try:
    ER605SnmpConfigEntry = ConfigEntry[SnmpRuntimeData]
except TypeError:
    ER605SnmpConfigEntry = ConfigEntry  # type: ignore[misc,assignment]
