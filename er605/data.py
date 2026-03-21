"""Data contracts for the TP-Link ER605 integration.

All dataclasses here represent one complete poll result.  Entities read
fields directly — no string-key lookups with fallbacks in entity code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

try:
    from homeassistant.config_entries import ConfigEntry
except ImportError:
    class ConfigEntry:  # type: ignore[no-redef]
        """Stub for test environments without homeassistant installed."""
        def __class_getitem__(cls, item):
            return cls

if TYPE_CHECKING:
    from .coordinator import ER605Coordinator


# ── Per-interface data ────────────────────────────────────────────────────────

@dataclass
class ER605InterfaceData:
    """State of one physical interface (from admin/interface?form=status2)."""

    name: str               # t_name:  "WAN1", "WAN2", "LAN1"
    label: str              # t_label: "WAN1", "WAN/LAN2", "LAN"
    is_wan: bool            # t_name.startswith("WAN")
    is_up: bool             # t_isup
    proto: str              # "dhcp", "static", "pppoe"
    mac: str                # normalised: lowercase, no dashes
    ip: str | None          # ipaddr — None when down or unassigned
    gateway: str | None     # gateway
    dns1: str | None        # dns1
    netmask: str | None     # netmask
    online: bool            # True if WAN online detection confirms gateway reachable
    role:   str | None      # "primary" / "backup" / "balanced" / None (LAN interfaces)

    @property
    def entity_key(self) -> str:
        """Stable, lowercased key used in entity unique IDs. e.g. 'wan1'."""
        return self.name.lower()


@dataclass
class ER605Ipv6InterfaceData:
    """IPv6 status for one WAN (from admin/ipv6?form=wanv6_status_info)."""

    name: str               # ifname: "WAN1", "WAN2"
    label: str              # t_label
    enabled: bool           # enable == "on"
    is_up: bool             # isup
    ip6addr: str | None     # None when "::"
    ip6gw: str | None       # None when "::"


@dataclass
class ER605IpstatEntry:
    """Per-IP traffic statistics (from admin/ipstats?form=list)."""

    addr: str               # IP address (local or remote)
    rx_bytes: int           # bytes received by this IP
    tx_bytes: int           # bytes transmitted by this IP
    rx_bps: int             # current receive rate  (KB/s)
    tx_bps: int             # current transmit rate (KB/s)
    rx_pkts: int            # packets received
    tx_pkts: int            # packets transmitted
    rx_pps: int             # current receive packet rate (pkts/s)
    tx_pps: int             # current transmit packet rate (pkts/s)

    @property
    def is_lan(self) -> bool:
        """True for RFC-1918 addresses (local LAN clients)."""
        return (
            self.addr.startswith("10.")
            or self.addr.startswith("192.168.")
            or self.addr.startswith("172.")
        )


@dataclass
class ER605IfstatEntry:
    """Per-zone traffic statistics (from admin/ifstat?form=list)."""

    zone: str               # "WAN1", "WAN2", "LAN1", etc.
    rx_bytes: int           # cumulative bytes received (since boot)
    tx_bytes: int           # cumulative bytes transmitted (since boot)
    rx_bps: int             # current receive rate  (KB/s)
    tx_bps: int             # current transmit rate (KB/s)
    rx_pkts: int            # cumulative packets received
    tx_pkts: int            # cumulative packets transmitted
    rx_pps: int             # current receive packet rate  (pkts/s)
    tx_pps: int             # current transmit packet rate (pkts/s)


@dataclass
class ER605PhysicalPortData:
    """State of one physical switch port (from admin/switch?form=state)."""

    port: str               # "1" .. "5"
    connected: bool         # state == "connected"
    speed: str | None       # "1000M", "100M", "10M", or None when disconnected
    duplex: str | None      # "Full", "Half", or None
    flowcontrol: str | None # "on", "off", or None


@dataclass
class ER605WanPortInfo:
    """One entry from admin/interface_wan?form=wanmode wan_names list."""

    index: str              # port index: "1", "2", ...
    name: str               # display name: "WAN1", "WAN/LAN2", "USB Modem"
    port_type: str          # "0"=WAN fixed, "1"=WAN/LAN, "2"=LAN, "4"=USB modem
    speed_bps: int | None   # from rate dict; None if not present


# ── System-level data ─────────────────────────────────────────────────────────

@dataclass
class ER605SystemData:
    """CPU and memory snapshot (from admin/sys_status?form=all_usage)."""

    cpu_per_core: dict[str, int]   # {"core1": 2, "core2": 3, ...}
    mem_percent: int               # 0–100

    @property
    def cpu_avg(self) -> int | None:
        vals = list(self.cpu_per_core.values())
        return round(sum(vals) / len(vals)) if vals else None

    @property
    def cpu_core_count(self) -> int:
        return len(self.cpu_per_core)


# ── Top-level poll snapshot ───────────────────────────────────────────────────

@dataclass
class ER605RouterData:
    """Complete snapshot returned by one coordinator poll cycle."""

    uptime_seconds: int
    system: ER605SystemData
    interfaces: list[ER605InterfaceData]
    ipv6_interfaces: list[ER605Ipv6InterfaceData]
    physical_ports: list[ER605PhysicalPortData]
    ifstat: list[ER605IfstatEntry]               # per-zone traffic stats
    ipstats: list[ER605IpstatEntry]             # per-IP traffic stats (slow-polled)
    poll_timestamp: float                        # time.monotonic() at poll start
    wan_policy: str | None = None               # "load_balance" / "failover" / "single" / None

    @property
    def wan_interfaces(self) -> list[ER605InterfaceData]:
        return [i for i in self.interfaces if i.is_wan]

    def interface(self, name: str) -> ER605InterfaceData | None:
        return next((i for i in self.interfaces if i.name == name), None)

    def ipv6(self, name: str) -> ER605Ipv6InterfaceData | None:
        return next((i for i in self.ipv6_interfaces if i.name == name), None)

    def ifstat_zone(self, zone: str) -> ER605IfstatEntry | None:
        return next((e for e in self.ifstat if e.zone == zone), None)

    @property
    def lan_clients(self) -> list[ER605IpstatEntry]:
        """LAN clients with any recorded traffic."""
        return [e for e in self.ipstats if e.is_lan]

    @property
    def active_lan_clients(self) -> list[ER605IpstatEntry]:
        """LAN clients with non-zero current traffic rate."""
        return [e for e in self.ipstats if e.is_lan and (e.rx_bps > 0 or e.tx_bps > 0)]


# ── Static device info (fetched once at setup) ────────────────────────────────

@dataclass
class ER605DeviceInfo:
    """Static device metadata fetched once during _async_setup()."""

    model: str              # "ER605"
    hw_version: str         # "v2" parsed from "ER605 v2.20"
    fw_version: str         # "2.3.2 Build 20251029 Rel.12727"
    unique_id: str          # WAN1 MAC, lowercase no-dash: "0cef1523f57d"
    wan_ports: list[ER605WanPortInfo]    # all ports from wanmode endpoint
    active_wan_indices: list[str]        # wan_numbers from wanmode


# ── HA runtime data stored in config_entry.runtime_data ──────────────────────

@dataclass
class ER605RuntimeData:
    """Stored in ConfigEntry.runtime_data."""

    coordinator: ER605Coordinator
    device_info: ER605DeviceInfo


# ── Type alias ────────────────────────────────────────────────────────────────

try:
    ER605ConfigEntry = ConfigEntry[ER605RuntimeData]
except TypeError:
    # Older HA versions where ConfigEntry is not generic at runtime
    ER605ConfigEntry = ConfigEntry  # type: ignore[assignment, misc]
