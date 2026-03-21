# custom_components/er605/snmp_coordinator.py
"""ER605 SNMP coordinator — 3-tier polling via SNMPv2c."""
from __future__ import annotations

import logging
import re
import time
from datetime import timedelta

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
    from .const import (
        DEFAULT_SNMP_MEDIUM_POLL_INTERVAL,
        DEFAULT_SNMP_POLL_INTERVAL,
        DEFAULT_SNMP_STATIC_POLL_INTERVAL,
        IF_TYPE_ETHERNET,
        OID_HR_STORAGE_RAM,
        OID_HR_STORAGE_SIZE,
        OID_HR_STORAGE_TYPE,
        OID_HR_STORAGE_USED,
        OID_IF_ADMIN_STATUS,
        OID_IF_DESCR_BASE,
        OID_IF_HC_IN_BASE,
        OID_IF_HC_OUT_BASE,
        OID_IF_HIGH_SPEED,
        OID_IF_OPER_STATUS,
        OID_IF_PHYS_ADDR,
        OID_IF_TYPE_BASE,
        OID_IP_ADDR_IFINDEX,
        OID_SYS_CONTACT,
        OID_SYS_DESCR,
        OID_SYS_LOCATION,
        OID_SYS_NAME,
        OID_SYS_UPTIME,
    )
    from .snmp_client import ER605SnmpClient, SnmpConnectionError
    from .snmp_data import SnmpDeviceInfo, SnmpPortData, SnmpRouterData, SnmpWanData
except ImportError:
    from const import (  # type: ignore[no-redef]
        DEFAULT_SNMP_MEDIUM_POLL_INTERVAL,
        DEFAULT_SNMP_POLL_INTERVAL,
        DEFAULT_SNMP_STATIC_POLL_INTERVAL,
        IF_TYPE_ETHERNET,
        OID_HR_STORAGE_RAM,
        OID_HR_STORAGE_SIZE,
        OID_HR_STORAGE_TYPE,
        OID_HR_STORAGE_USED,
        OID_IF_ADMIN_STATUS,
        OID_IF_DESCR_BASE,
        OID_IF_HC_IN_BASE,
        OID_IF_HC_OUT_BASE,
        OID_IF_HIGH_SPEED,
        OID_IF_OPER_STATUS,
        OID_IF_PHYS_ADDR,
        OID_IF_TYPE_BASE,
        OID_IP_ADDR_IFINDEX,
        OID_SYS_CONTACT,
        OID_SYS_DESCR,
        OID_SYS_LOCATION,
        OID_SYS_NAME,
        OID_SYS_UPTIME,
    )
    from snmp_client import ER605SnmpClient, SnmpConnectionError  # type: ignore[no-redef]
    from snmp_data import SnmpDeviceInfo, SnmpPortData, SnmpRouterData, SnmpWanData  # type: ignore[no-redef]

    # Minimal stubs so the module loads without homeassistant installed (tests).
    class HomeAssistant:  # type: ignore[no-redef]
        pass

    class DataUpdateCoordinator:  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            pass
        def __class_getitem__(cls, item):
            return cls

    class UpdateFailed(Exception):  # type: ignore[no-redef]
        pass

_LOGGER = logging.getLogger(__name__)

_MAX_64BIT = 18_446_744_073_709_551_615

# ── Interface classification ──────────────────────────────────────────────────

_IFACE_IGNORE  = re.compile(r"^(lo|br-|veth|docker|gre|sit|ip6tnl|gretap|inf)", re.I)
_IFACE_TUNNEL  = re.compile(r"^(tun|pptp|l2tp|ipsec|wg|wireguard|tun_server)", re.I)

# ── WAN interface label mapping ───────────────────────────────────────────────

_IFACE_LABEL_MAP: dict[str, tuple[str, str]] = {
    "eth0":  ("WAN1",    "eth0"),
    "eth1":  ("WAN2",    "eth1"),
    "eth2":  ("WAN3",    "eth2"),
    "usb0":  ("WAN USB", "usb0"),
    "wwan0": ("WAN USB", "usb0"),   # normalize wwan0 → usb0 slug
}


def _label_for(if_descr: str) -> tuple[str, str]:
    """Return (display_label, iface_slug) for an interface description.

    e.g. "default/eth0" → ("WAN1", "eth0")
         "usb0"         → ("WAN USB", "usb0")
         "default/eth9" → ("WAN eth9", "eth9")   # unknown fallback
    """
    short = if_descr.split("/")[-1]
    return _IFACE_LABEL_MAP.get(short, (f"WAN {short}", short))


def _classify_interface(if_descr: str) -> str:
    """Classify an interface name. Returns 'ignore', 'tunnel', or 'physical'."""
    short = if_descr.split("/")[-1]
    if _IFACE_IGNORE.match(short):
        return "ignore"
    if _IFACE_TUNNEL.match(short):
        return "tunnel"
    return "physical"


# ── Counter helpers ───────────────────────────────────────────────────────────

def _safe_delta(current: int, previous: int, use_64bit: bool) -> int:
    """Compute counter delta, handling wrap and reboot.

    Rules (in order):
    1. current >= previous → normal increment
    2. previous + current < MAX → reboot (counter reset to current)
    3. otherwise → counter wrap: (MAX - previous) + current + 1

    Note: The 32-bit path (use_64bit=False) is included for completeness but
    never exercised in production — ER605 HC counters are 64-bit. The reboot
    heuristic is unreliable for 32-bit at high traffic volumes.
    """
    max_val = _MAX_64BIT if use_64bit else 4_294_967_295
    if current >= previous:
        return current - previous
    if (previous + current) < max_val:
        return current  # reboot reset
    return (max_val - previous) + current + 1


def _parse_firmware(sys_descr: str) -> str:
    """Extract a short firmware string from sysDescr."""
    m = re.search(r"(ER\d+\S*)\s+.*?Build\s+(\d+)", sys_descr)
    if m:
        return f"{m.group(1)} Build {m.group(2)}"
    m = re.search(r"Build\s+(\d+)", sys_descr)
    if m:
        return f"Build {m.group(1)}"
    return sys_descr[:60]


# ── Coordinator ───────────────────────────────────────────────────────────────

class ER605SnmpCoordinator(DataUpdateCoordinator[SnmpRouterData]):
    """3-tier SNMP coordinator for the TP-Link ER605."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ER605SnmpClient,
        poll_interval: int = DEFAULT_SNMP_POLL_INTERVAL,
        medium_poll_interval: int = DEFAULT_SNMP_MEDIUM_POLL_INTERVAL,
        static_poll_interval: int = DEFAULT_SNMP_STATIC_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="ER605 SNMP",
            update_interval=timedelta(seconds=poll_interval) if poll_interval > 0 else None,
        )
        self._client = client
        self._host = client._host  # store once, used in fallback unique_id
        self._medium_poll_interval = medium_poll_interval
        self._static_poll_interval = static_poll_interval

        # WAN interface discovery results (set in async_setup)
        self._wan_indices: list[int] = []          # ifIndex per discovered WAN (0–4 entries)
        self._wan_descrs: list[str]  = []          # ifDescr per WAN
        self._wan_speeds: list[int]  = []          # ifHighSpeed per WAN
        self._wan_labels: list[str]  = []          # display label: "WAN1", "WAN USB", …
        self._wan_slugs:  list[str]  = []          # stable slug: "eth0", "usb0", …
        self._port_indices: list[int] = []         # all physical ethernet ports
        self._port_descrs: dict[int, str] = {}     # {ifIndex: ifDescr} for physical ports

        # hrStorage RAM row index (found at setup)
        self._ram_row: int | None = None

        # Rate calculation state
        self._prev_in:  dict[int, int] = {}        # {if_index: counter_value}
        self._prev_out: dict[int, int] = {}
        self._prev_poll_time: float = 0.0

        # Tier caches
        self._medium_last: float = 0.0
        self._static_last: float = 0.0
        self._force_medium: bool = False
        self._force_static: bool = False
        self._consecutive_failures: int = 0

        # Cached tier data
        self._wan_ips: dict[int, str | None] = {}  # {if_index: ip_str}
        self._uptime_seconds: float | None = None
        self._static_data: dict[str, str] = {}    # {"sys_descr": ..., "sys_name": ...}

        # Static device info (set in async_setup)
        self.device_info: SnmpDeviceInfo | None = None

    # ── One-time setup ────────────────────────────────────────────────────────

    async def async_setup(self) -> SnmpDeviceInfo:
        """Discover interfaces, fetch static device info. Call once at entry setup."""
        # 1. Walk ifDescr, ifType, ifHighSpeed
        descr_raw  = await self._client.walk(OID_IF_DESCR_BASE)
        type_raw   = await self._client.walk(OID_IF_TYPE_BASE)
        speed_raw  = await self._client.walk(OID_IF_HIGH_SPEED)

        # 2. Walk ipAddrTable to find which ifIndices have IPs
        ip_idx_raw = await self._client.walk(OID_IP_ADDR_IFINDEX)
        # ip_idx_raw: {"1.3.6.1.2.1.4.20.1.2.<a>.<b>.<c>.<d>": "<if_index>", ...}
        ip_to_ifindex: dict[str, int] = {}
        for oid, idx_str in ip_idx_raw.items():
            ip = ".".join(oid.split(".")[-4:])
            try:
                ip_to_ifindex[ip] = int(idx_str)
            except (ValueError, TypeError):
                pass
        # Reverse: ifindex → ip
        ifindex_to_ip: dict[int, str] = {v: k for k, v in ip_to_ifindex.items()}

        # 3. Build interface list
        wan_candidates: list[tuple[int, str, int]] = []  # (ifIndex, ifDescr, ifHighSpeed)
        ports: list[int] = []

        for descr_oid, descr_val in descr_raw.items():
            idx = int(descr_oid.split(".")[-1])
            if_type = int(type_raw.get(f"{OID_IF_TYPE_BASE}.{idx}", "0") or "0")
            speed = int(speed_raw.get(f"{OID_IF_HIGH_SPEED}.{idx}", "0") or "0")
            role = _classify_interface(str(descr_val))
            short = str(descr_val).split("/")[-1]
            is_usb_modem = short.startswith(("usb", "wwan"))

            if role == "ignore" or role == "tunnel":
                continue
            if is_usb_modem:
                # USB modems have a different ifType — detect by name
                if idx in ifindex_to_ip:
                    wan_candidates.append((idx, str(descr_val), 0))
            elif if_type == IF_TYPE_ETHERNET and speed > 0:
                # All physical Ethernet interfaces with link speed are WAN ports.
                # The ER605 does not expose DHCP-assigned WAN IPs in the SNMP
                # ipAddrTable, so IP presence cannot be used to distinguish WAN
                # from LAN.  LAN switch ports are behind br-lan (speed=0) and
                # never appear here.
                wan_candidates.append((idx, str(descr_val), speed))

        # 4. Sort WANs by ifIndex; capture all discovered WANs (0–4)
        wan_candidates.sort(key=lambda x: x[0])
        self._wan_indices = [w[0] for w in wan_candidates]
        self._wan_descrs  = [w[1] for w in wan_candidates]
        self._wan_speeds  = [w[2] for w in wan_candidates]
        labels_slugs      = [_label_for(d) for d in self._wan_descrs]
        self._wan_labels  = [ls[0] for ls in labels_slugs]
        self._wan_slugs   = [ls[1] for ls in labels_slugs]

        self._port_indices = sorted(ports)
        # Cache port descriptions for use in _fetch_tier1_ports
        self._port_descrs = {
            idx: str(descr_raw.get(f"{OID_IF_DESCR_BASE}.{idx}", str(idx)))
            for idx in self._port_indices
        }
        # Pre-populate WAN IPs cache
        for idx in self._wan_indices:
            self._wan_ips[idx] = ifindex_to_ip.get(idx)

        # 5. Find hrStorage RAM row
        storage_types = await self._client.walk(OID_HR_STORAGE_TYPE)
        for oid, type_val in storage_types.items():
            if str(type_val) == OID_HR_STORAGE_RAM or OID_HR_STORAGE_RAM in str(type_val):
                self._ram_row = int(oid.split(".")[-1])
                break
        if self._ram_row is None:
            self._ram_row = 1  # fallback confirmed by discover_all.py

        # 6. Fetch static device info
        sys_descr   = str(await self._client.get(OID_SYS_DESCR))
        sys_name    = str(await self._client.get(OID_SYS_NAME))
        sys_contact = str(await self._client.get(OID_SYS_CONTACT))
        sys_loc     = str(await self._client.get(OID_SYS_LOCATION))

        # 7. Get WAN1 MAC for unique_id
        wan1_mac = ""
        if self._wan_indices and self._wan_indices[0]:
            try:
                mac_raw = await self._client.get(
                    f"{OID_IF_PHYS_ADDR}.{self._wan_indices[0]}"
                )
                # pysnmp returns OctetString — convert hex bytes to mac string
                mac_bytes = bytes.fromhex(str(mac_raw).replace("0x", "").replace(" ", ""))
                wan1_mac = mac_bytes.hex()
            except Exception:
                wan1_mac = f"snmp_{self._host}"

        self._static_data = {
            "sys_descr":    sys_descr,
            "sys_name":     sys_name,
            "sys_contact":  sys_contact,
            "sys_location": sys_loc,
        }

        self.device_info = SnmpDeviceInfo(
            model       = sys_name or "ER605",
            fw_version  = _parse_firmware(sys_descr),
            sys_name    = sys_name,
            sys_contact = sys_contact,
            sys_location= sys_loc,
            wan1_mac    = wan1_mac,
        )
        return self.device_info

    # ── Main poll loop ────────────────────────────────────────────────────────

    async def _async_update_data(self) -> SnmpRouterData:
        """Fetch all tiers as appropriate. Called by DataUpdateCoordinator."""
        now = time.monotonic()
        try:
            result = await self._fetch_all(now)
            self._consecutive_failures = 0
            return result
        except SnmpConnectionError as err:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                raise UpdateFailed(f"SNMP unreachable after 3 attempts: {err}") from err
            _LOGGER.warning("SNMP poll failed (%d/3): %s", self._consecutive_failures, err)
            # Return last data or empty placeholder
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"SNMP unavailable: {err}") from err

    async def _fetch_all(self, now: float) -> SnmpRouterData:
        """Fetch all three tiers as scheduled."""
        # ── Tier 3: Static (sysDescr, sysName, sysContact, sysLocation) ──────
        if self._force_static or (
            self._static_poll_interval > 0
            and (now - self._static_last) >= self._static_poll_interval
        ):
            self._force_static = False
            oids = [OID_SYS_DESCR, OID_SYS_NAME, OID_SYS_CONTACT, OID_SYS_LOCATION]
            vals = await self._client.get_many(oids)
            self._static_data = {
                "sys_descr":    str(vals.get(OID_SYS_DESCR, "")),
                "sys_name":     str(vals.get(OID_SYS_NAME, "")),
                "sys_contact":  str(vals.get(OID_SYS_CONTACT, "")),
                "sys_location": str(vals.get(OID_SYS_LOCATION, "")),
            }
            self._static_last = now

        # ── Tier 2: Medium (WAN IPs, uptime) ─────────────────────────────────
        if self._force_medium or (
            self._medium_poll_interval > 0
            and (now - self._medium_last) >= self._medium_poll_interval
        ):
            self._force_medium = False
            ip_idx_raw = await self._client.walk(OID_IP_ADDR_IFINDEX)
            ifindex_to_ip: dict[int, str] = {}
            for oid, idx_str in ip_idx_raw.items():
                ip = ".".join(oid.split(".")[-4:])
                try:
                    ifindex_to_ip[int(idx_str)] = ip
                except (ValueError, TypeError):
                    pass
            # Update WAN IPs (never remove, just update)
            for idx in self._wan_indices:
                self._wan_ips[idx] = ifindex_to_ip.get(idx)
            # Uptime
            try:
                uptime_raw = await self._client.get(OID_SYS_UPTIME)
                self._uptime_seconds = float(str(uptime_raw)) / 100.0
            except Exception:
                self._uptime_seconds = None
            self._medium_last = now

        # ── Tier 1: Fast (counters, status, memory) ───────────────────────────
        wan_data    = await self._fetch_tier1_wan(now)
        port_data   = await self._fetch_tier1_ports()
        memory_pct  = await self._fetch_memory()

        return SnmpRouterData(
            wan              = wan_data,
            ports            = port_data,
            uptime_seconds   = self._uptime_seconds,
            memory_pct       = memory_pct,
            sys_descr        = self._static_data.get("sys_descr"),
            sys_name         = self._static_data.get("sys_name"),
            sys_contact      = self._static_data.get("sys_contact"),
            sys_location     = self._static_data.get("sys_location"),
            poll_timestamp   = now,
        )

    async def _fetch_tier1_wan(self, now: float) -> list[SnmpWanData]:
        """Fetch HC counters + oper/admin status for all discovered WAN interfaces."""
        result: list[SnmpWanData] = []
        elapsed = (now - self._prev_poll_time) if self._prev_poll_time else 0.0
        self._prev_poll_time = now

        for slot, (idx, descr, speed, label, slug) in enumerate(
            zip(
                self._wan_indices,
                self._wan_descrs,
                self._wan_speeds,
                self._wan_labels,
                self._wan_slugs,
            ),
            start=1,
        ):
            oids = [
                f"{OID_IF_HC_IN_BASE}.{idx}",
                f"{OID_IF_HC_OUT_BASE}.{idx}",
                f"{OID_IF_OPER_STATUS}.{idx}",
                f"{OID_IF_ADMIN_STATUS}.{idx}",
            ]
            vals = await self._client.get_many(oids)

            hc_in  = int(vals.get(f"{OID_IF_HC_IN_BASE}.{idx}",  "0") or "0")
            hc_out = int(vals.get(f"{OID_IF_HC_OUT_BASE}.{idx}", "0") or "0")
            oper   = int(vals.get(f"{OID_IF_OPER_STATUS}.{idx}",  "2") or "2")
            admin  = int(vals.get(f"{OID_IF_ADMIN_STATUS}.{idx}", "2") or "2")

            rx_rate = tx_rate = None
            if elapsed > 0 and idx in self._prev_in and idx in self._prev_out:
                rx_delta = _safe_delta(hc_in,  self._prev_in[idx],  True)
                tx_delta = _safe_delta(hc_out, self._prev_out[idx], True)
                rx_rate  = round(rx_delta * 8 / elapsed / 1_000_000, 3)
                tx_rate  = round(tx_delta * 8 / elapsed / 1_000_000, 3)

            self._prev_in[idx]  = hc_in
            self._prev_out[idx] = hc_out

            result.append(SnmpWanData(
                slot=slot, if_index=idx, if_descr=descr,
                if_label=label, iface_slug=slug,
                ip=self._wan_ips.get(idx),
                oper_status=oper, admin_status=admin, link_speed_mbps=speed,
                hc_in_octets=hc_in, hc_out_octets=hc_out,
                rx_rate_mbps=rx_rate, tx_rate_mbps=tx_rate,
            ))

        return result

    async def _fetch_tier1_ports(self) -> list[SnmpPortData]:
        """Fetch oper/admin status for all physical ports."""
        if not self._port_indices:
            return []
        oids = []
        for idx in self._port_indices:
            oids += [f"{OID_IF_OPER_STATUS}.{idx}", f"{OID_IF_ADMIN_STATUS}.{idx}"]
        vals = await self._client.get_many(oids)

        # Use port descriptions cached during async_setup
        descr_cache = self._port_descrs

        ports = []
        for idx in self._port_indices:
            oper  = int(vals.get(f"{OID_IF_OPER_STATUS}.{idx}",  "2") or "2")
            admin = int(vals.get(f"{OID_IF_ADMIN_STATUS}.{idx}", "2") or "2")
            ports.append(SnmpPortData(
                if_index=idx,
                if_descr=descr_cache.get(idx, str(idx)),
                oper_status=oper,
                admin_status=admin,
                high_speed=0,
            ))
        return ports

    async def _fetch_memory(self) -> float | None:
        """Return memory utilization % from hrStorage."""
        if self._ram_row is None:
            return None
        try:
            vals = await self._client.get_many([
                f"{OID_HR_STORAGE_SIZE}.{self._ram_row}",
                f"{OID_HR_STORAGE_USED}.{self._ram_row}",
            ])
            size = int(vals.get(f"{OID_HR_STORAGE_SIZE}.{self._ram_row}", "0") or "0")
            used = int(vals.get(f"{OID_HR_STORAGE_USED}.{self._ram_row}", "0") or "0")
            if size > 0:
                return round(used / size * 100, 1)
        except Exception:
            pass
        return None

    # ── Manual refresh ─────────────────────────────────────────────────────────

    async def async_refresh_fast(self) -> None:
        await self.async_request_refresh()

    async def async_refresh_medium(self) -> None:
        self._force_medium = True
        await self.async_request_refresh()

    async def async_refresh_ipstats(self) -> None:
        """Triggers Tier 3 (static data) for SNMP entries."""
        self._force_static = True
        await self.async_request_refresh()

    async def async_refresh_all(self) -> None:
        self._force_medium = True
        self._force_static = True
        await self.async_request_refresh()


def build_wan_stubs(coordinator: ER605SnmpCoordinator) -> list[SnmpWanData]:
    """Build stub SnmpWanData objects from coordinator discovery lists.

    Called at entity build time (before first poll) to construct entity stubs
    that provide stable iface_slug / if_label identity for entity registration.
    All live data is looked up via coordinator.data at property-access time.
    """
    return [
        SnmpWanData(
            slot=i + 1, if_index=idx, if_descr=descr,
            if_label=label, iface_slug=slug,
            ip=None, oper_status=2, admin_status=2, link_speed_mbps=speed,
            hc_in_octets=0, hc_out_octets=0,
            rx_rate_mbps=None, tx_rate_mbps=None,
        )
        for i, (idx, descr, label, slug, speed) in enumerate(zip(
            coordinator._wan_indices, coordinator._wan_descrs,
            coordinator._wan_labels, coordinator._wan_slugs, coordinator._wan_speeds,
        ))
    ]
