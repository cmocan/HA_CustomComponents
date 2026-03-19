"""DataUpdateCoordinator for the TP-Link ER605 integration."""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DEFAULT_IPSTATS_POLL_INTERVAL,
    DEFAULT_MEDIUM_POLL_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
)
from .data import (
    ER605DeviceInfo,
    ER605IfstatEntry,
    ER605IpstatEntry,
    ER605InterfaceData,
    ER605Ipv6InterfaceData,
    ER605PhysicalPortData,
    ER605RouterData,
    ER605SystemData,
    ER605WanPortInfo,
)
from .http_client import ER605HttpClient, HttpError, HttpLoginError, HttpSessionError

_LOGGER = logging.getLogger(__name__)


class ER605Coordinator(DataUpdateCoordinator[ER605RouterData]):
    """Polls the ER605 HTTP API and provides typed data to all entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ER605HttpClient,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
        medium_poll_interval: int = DEFAULT_MEDIUM_POLL_INTERVAL,
        ipstats_poll_interval: int = DEFAULT_IPSTATS_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval) if poll_interval > 0 else None,
        )
        self._client = client
        self.device_info: ER605DeviceInfo | None = None

        # Tier 2 (medium) time-based cache
        self._medium_poll_interval = medium_poll_interval  # 0 = manual only
        self._medium_last_fetch: float = 0.0
        self._medium_cache_interfaces: list[ER605InterfaceData] = []
        self._medium_cache_ipv6: list[ER605Ipv6InterfaceData] = []
        self._medium_cache_uptime: int = 0

        # Tier 3 (slow / ipstats) time-based cache
        self._ipstats_poll_interval = ipstats_poll_interval  # 0 = manual only
        self._ipstats_last_fetch: float = 0.0
        self._ipstats_cache: list[ER605IpstatEntry] = []
        self.ipstats_generation: int = 0

        # Manual refresh flags — set by service calls, consumed by _fetch_all
        self._force_medium: bool = False
        self._force_ipstats: bool = False

    # ── Setup (called once by __init__.py after coordinator is created) ───────

    async def async_setup(self) -> ER605DeviceInfo:
        """Login and fetch static device info.  Returns ER605DeviceInfo.

        Raises ConfigEntryAuthFailed on wrong credentials.
        Raises UpdateFailed on connectivity problems.
        """
        await self._login()

        firmware   = await self._client.get_firmware()
        ifaces     = await self._client.get_interfaces()
        wan_mode   = await self._client.get_wan_mode()

        self.device_info = _build_device_info(firmware, ifaces, wan_mode)
        return self.device_info

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> ER605RouterData:
        """Fetch a complete snapshot.  Re-logins once on stale session."""
        try:
            return await self._fetch_all()
        except HttpSessionError:
            _LOGGER.debug("Session stale, re-logging in")
            await self._login()
            try:
                return await self._fetch_all()
            except (HttpSessionError, HttpError) as err:
                raise UpdateFailed(f"ER605 update failed after re-login: {err}") from err
        except HttpLoginError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HttpError as err:
            raise UpdateFailed(f"ER605 update failed: {err}") from err

    # ── Per-tier manual refresh (called by HA services) ───────────────────────

    async def async_refresh_fast(self) -> None:
        """Force a Tier 1 (fast) refresh — triggers a full coordinator poll."""
        await self.async_request_refresh()

    async def async_refresh_medium(self) -> None:
        """Force a Tier 2 (medium) refresh on the next poll cycle."""
        self._force_medium = True
        await self.async_request_refresh()

    async def async_refresh_ipstats(self) -> None:
        """Force a Tier 3 (ipstats) refresh on the next poll cycle."""
        self._force_ipstats = True
        await self.async_request_refresh()

    async def async_refresh_all(self) -> None:
        """Force all three tiers to refresh on the next poll cycle."""
        self._force_medium = True
        self._force_ipstats = True
        await self.async_request_refresh()

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _login(self) -> None:
        try:
            await self._client.login()
        except HttpLoginError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except HttpError as err:
            raise UpdateFailed(f"Cannot connect to ER605: {err}") from err

    async def _fetch_all(self) -> ER605RouterData:
        now = time.monotonic()

        # ── Tier 1: FAST — every poll cycle ──
        sys_raw    = await self._client.get_system_status()
        ports_raw  = await self._client.get_switch_state()
        ifstat_raw = await self._client.get_ifstat()

        # ── Tier 2: MEDIUM — on its own time-based interval (0 = manual only) ──
        run_medium = self._force_medium
        self._force_medium = False
        if not run_medium and self._medium_poll_interval > 0:
            run_medium = (now - self._medium_last_fetch >= self._medium_poll_interval)
        if run_medium:
            self._medium_last_fetch = now
            try:
                iface_raw  = await self._client.get_interfaces()
                ipv6_raw   = await self._client.get_ipv6_status()
                time_raw   = await self._client.get_time()
                self._medium_cache_interfaces = _parse_interfaces(iface_raw)
                self._medium_cache_ipv6       = _parse_ipv6(ipv6_raw)
                self._medium_cache_uptime     = int(time_raw.get("run", 0))
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Medium-tier fetch failed, using cached data: %s", err)

        # ── Tier 3: SLOW — ipstats on its own time-based interval (0 = manual only) ──
        run_ipstats = self._force_ipstats
        self._force_ipstats = False
        if not run_ipstats and self._ipstats_poll_interval > 0:
            run_ipstats = (now - self._ipstats_last_fetch >= self._ipstats_poll_interval)
        if run_ipstats:
            self._ipstats_last_fetch = now
            try:
                ipstats_raw = await self._client.get_ipstats()
                self._ipstats_cache = _parse_ipstats(ipstats_raw)
                self.ipstats_generation += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("ipstats fetch failed, using cached data: %s", err)

        return ER605RouterData(
            uptime_seconds = self._medium_cache_uptime,
            system         = _parse_system(sys_raw),
            interfaces     = self._medium_cache_interfaces,
            ipv6_interfaces= self._medium_cache_ipv6,
            physical_ports = _parse_ports(ports_raw),
            ifstat         = _parse_ifstat(ifstat_raw),
            ipstats        = self._ipstats_cache,
            poll_timestamp = now,
        )


# ── Parsing helpers ───────────────────────────────────────────────────────────

def _parse_system(raw: dict) -> ER605SystemData:
    cpu = raw.get("cpu_usage", {})
    mem = raw.get("mem_usage", {})
    return ER605SystemData(
        cpu_per_core = cpu if isinstance(cpu, dict) else {},
        mem_percent  = mem.get("mem", 0) if isinstance(mem, dict) else int(mem or 0),
    )


def _parse_interfaces(raw: list[dict]) -> list[ER605InterfaceData]:
    result = []
    for iface in raw:
        ip  = iface.get("ipaddr") or None
        gw  = iface.get("gateway") or None
        dns = iface.get("dns1") or None
        nm  = iface.get("netmask") or None
        result.append(ER605InterfaceData(
            name    = iface.get("t_name", ""),
            label   = iface.get("t_label", ""),
            is_wan  = iface.get("t_name", "").startswith("WAN"),
            is_up   = bool(iface.get("t_isup", False)),
            proto   = iface.get("t_proto", ""),
            mac     = iface.get("macaddr", "").replace("-", "").lower(),
            ip      = ip,
            gateway = gw,
            dns1    = dns,
            netmask = nm,
        ))
    return result


def _parse_ipv6(raw: list[dict]) -> list[ER605Ipv6InterfaceData]:
    def _addr(val: str | None) -> str | None:
        return None if not val or val == "::" else val

    result = []
    for wan in raw:
        result.append(ER605Ipv6InterfaceData(
            name    = wan.get("ifname", wan.get("interface", "")),
            label   = wan.get("t_label", ""),
            enabled = wan.get("enable", "off") == "on",
            is_up   = bool(wan.get("isup", False)),
            ip6addr = _addr(wan.get("ip6addr")),
            ip6gw   = _addr(wan.get("ip6gw")),
        ))
    return result


def _parse_ports(raw: list[dict]) -> list[ER605PhysicalPortData]:
    result = []
    for port in raw:
        connected = port.get("state") == "connected"
        result.append(ER605PhysicalPortData(
            port        = port.get("port", ""),
            connected   = connected,
            speed       = port.get("speed") if connected else None,
            duplex      = port.get("duplex") if connected else None,
            flowcontrol = port.get("flowcontrol") if connected else None,
        ))
    return result


def _parse_ipstats(raw: list[dict]) -> list[ER605IpstatEntry]:
    result = []
    for item in raw:
        addr = item.get("addr", "")
        if not addr:
            continue
        result.append(ER605IpstatEntry(
            addr     = addr,
            rx_bytes = int(item.get("rx_bytes", 0)),
            tx_bytes = int(item.get("tx_bytes", 0)),
            rx_bps   = int(item.get("rx_bps", 0)),
            tx_bps   = int(item.get("tx_bps", 0)),
            rx_pkts  = int(item.get("rx_pkts", 0)),
            tx_pkts  = int(item.get("tx_pkts", 0)),
            rx_pps   = int(item.get("rx_pps", 0)),
            tx_pps   = int(item.get("tx_pps", 0)),
        ))
    return result


def _parse_ifstat(raw: list[dict]) -> list[ER605IfstatEntry]:
    result = []
    for item in raw:
        zone = item.get("zone") or item.get("interface", "")
        if not zone:
            continue
        result.append(ER605IfstatEntry(
            zone     = zone,
            rx_bytes = int(item.get("rx_bytes", 0)),
            tx_bytes = int(item.get("tx_bytes", 0)),
            rx_bps   = int(item.get("rx_bps", 0)),
            tx_bps   = int(item.get("tx_bps", 0)),
            rx_pkts  = int(item.get("rx_pkts", 0)),
            tx_pkts  = int(item.get("tx_pkts", 0)),
            rx_pps   = int(item.get("rx_pps", 0)),
            tx_pps   = int(item.get("tx_pps", 0)),
        ))
    return result


def _build_device_info(
    firmware: dict,
    ifaces: list[dict],
    wan_mode: dict,
) -> ER605DeviceInfo:
    # Parse hardware version: "ER605 v2.20" → "v2"
    hw_raw = firmware.get("hardware_version", "")
    hw_ver = "unknown"
    for part in hw_raw.split():
        if part.lower().startswith("v") and part[1:2].isdigit():
            hw_ver = part.split(".")[0]   # "v2.20" → "v2"
            break

    # unique_id from WAN1 MAC
    wan_mac = next(
        (
            i.get("macaddr", "").replace("-", "").lower()
            for i in ifaces
            if i.get("t_name", "").startswith("WAN") and i.get("macaddr")
        ),
        None,
    )
    if not wan_mac:
        # Fallback: first interface MAC
        wan_mac = next(
            (i.get("macaddr", "").replace("-", "").lower()
             for i in ifaces if i.get("macaddr")),
            "unknown",
        )

    # Build port list from wanmode
    rates = wan_mode.get("rate", {})
    wan_ports = [
        ER605WanPortInfo(
            index      = p.get("index", ""),
            name       = p.get("name", ""),
            port_type  = str(p.get("type", "")),
            speed_bps  = int(rates[p["index"]]) if p.get("index") in rates else None,
        )
        for p in wan_mode.get("wan_names", [])
    ]

    return ER605DeviceInfo(
        model               = firmware.get("model", "ER605"),
        hw_version          = hw_ver,
        fw_version          = firmware.get("firmware_version", ""),
        unique_id           = wan_mac,
        wan_ports           = wan_ports,
        active_wan_indices  = wan_mode.get("wan_numbers", []),
    )
