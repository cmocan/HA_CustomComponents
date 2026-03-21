"""Async reverse-DNS cache for external IP addresses.

Resolves public IPs to hostnames via PTR lookup (socket.gethostbyaddr),
caches results persistently in HA storage, and never re-resolves a known IP.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import TYPE_CHECKING

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.storage import Store
except ImportError:
    class HomeAssistant:  # type: ignore[no-redef]
        pass

    class Store:  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            pass
        async def async_load(self):
            return None
        async def async_save(self, data):
            pass

# Allow tests to monkey-patch _Store
_Store = Store

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY     = "er605_dns_cache"
STORAGE_VERSION = 1
MAX_CONCURRENT  = 5   # max parallel PTR lookups at once
MAX_BATCH       = 50  # max new IPs resolved per poll cycle
LOOKUP_TIMEOUT  = 3.0

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
]


def _is_private(ip: str) -> bool:
    """Return True for RFC-1918, loopback, link-local, or unparseable addresses."""
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _PRIVATE_NETWORKS)
    except ValueError:
        return True


class DnsResolverCache:
    """Persistent reverse-DNS cache.  One instance per config entry."""

    def __init__(self) -> None:
        self._cache: dict[str, str] = {}
        self._store: Store | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    async def async_load(self, hass: HomeAssistant) -> None:
        """Load persisted cache from HA storage.  Call once at entry setup."""
        self._store = _Store(hass, STORAGE_VERSION, STORAGE_KEY)
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._cache = data
            _LOGGER.debug("DNS cache loaded: %d entries", len(self._cache))

    async def resolve_new(
        self, hass: HomeAssistant, ips: list[str]
    ) -> list[tuple[str, str]]:
        """Resolve IPs not yet in cache.

        Returns list of (ip, hostname) for entries added in this call only.
        Skips private IPs and IPs already in cache.
        Resolves at most MAX_CONCURRENT IPs per call.
        On failure or timeout stores raw IP as hostname (never retried).
        """
        to_resolve = [
            ip for ip in ips
            if ip not in self._cache and not _is_private(ip)
        ][:MAX_BATCH]

        if not to_resolve:
            return []

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        loop = asyncio.get_running_loop()

        async def _limited(ip: str) -> tuple[str, str]:
            async with semaphore:
                return await self._lookup_one(loop, ip)

        tasks = [_limited(ip) for ip in to_resolve]
        results: list[tuple[str, str]] = await asyncio.gather(*tasks)

        newly_added = []
        for ip, hostname in results:
            if ip not in self._cache:
                self._cache[ip] = hostname
                newly_added.append((ip, hostname))

        if newly_added and self._store:
            await self._store.async_save(self._cache)

        return newly_added

    def get(self, ip: str) -> str | None:
        """Return cached hostname or None if not yet resolved."""
        return self._cache.get(ip)

    @property
    def cache(self) -> dict[str, str]:
        """Read-only snapshot of the full {ip: hostname} cache."""
        return dict(self._cache)

    # ── Internal ──────────────────────────────────────────────────────────────

    async def _lookup_one(
        self, loop: asyncio.AbstractEventLoop, ip: str
    ) -> tuple[str, str]:
        """Reverse-lookup one IP.  Returns (ip, hostname); falls back to raw IP."""
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(None, socket.gethostbyaddr, ip),
                timeout=LOOKUP_TIMEOUT,
            )
            hostname = result[0]
            _LOGGER.debug("PTR %s -> %s", ip, hostname)
            return ip, hostname
        except Exception:  # timeout, OSError, or anything else  # noqa: BLE001
            _LOGGER.debug("PTR %s -> (no result, storing raw IP)", ip)
            return ip, ip
