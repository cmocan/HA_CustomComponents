"""Router strategy registry and client abstract base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

try:
    from homeassistant.components.binary_sensor import BinarySensorEntityDescription
    from homeassistant.components.sensor import SensorEntityDescription
except ImportError:
    class SensorEntityDescription:  # type: ignore[no-redef]
        def __init__(self, key: str = "", **kwargs) -> None:
            self.key = key
            for k, v in kwargs.items():
                setattr(self, k, v)

    class BinarySensorEntityDescription:  # type: ignore[no-redef]
        def __init__(self, key: str = "", **kwargs) -> None:
            self.key = key
            for k, v in kwargs.items():
                setattr(self, k, v)

if TYPE_CHECKING:
    from .data import DslChannel, RouterData


# ── Error types ──────────────────────────────────────────────────────────────

class AuthError(Exception):
    """Raised by RouterClient.async_login() on bad credentials."""

class FetchError(Exception):
    """Raised by RouterClient.async_fetch_data() on retrieval failure."""


# ── Channel template types ───────────────────────────────────────────────────

@dataclass
class ChannelSensorTemplate:
    """Template for a per-DOCSIS-channel sensor, expanded at platform setup.

    name_template is formatted via .format(channel_id=channel.channel_id).
    direction_filter restricts expansion: None = both directions.
    """
    key_suffix: str
    name_template: str
    unit: str
    device_class: str | None
    value_fn: Callable[[DslChannel], float | None]
    direction_filter: str | None = None


@dataclass
class ChannelBinarySensorTemplate:
    """Template for a per-DOCSIS-channel binary sensor, same expansion pattern."""
    key_suffix: str
    name_template: str
    device_class: str | None
    value_fn: Callable[[DslChannel], bool]
    direction_filter: str | None = None


# ── Router strategy ──────────────────────────────────────────────────────────

@dataclass
class RouterStrategy:
    """Descriptor for one router type. No logic — just references and lists."""
    display_name: str
    client_class: type[RouterClient]
    sensor_descs: list = field(default_factory=list)
    binary_sensor_descs: list = field(default_factory=list)
    channel_sensor_templates: list[ChannelSensorTemplate] = field(default_factory=list)
    channel_binary_sensor_templates: list[ChannelBinarySensorTemplate] = field(default_factory=list)
    supports_device_tracker: bool = True


# ── Router client ABC ────────────────────────────────────────────────────────

class RouterClient(ABC):
    """Abstract base class every router client must implement.

    All implementations must:
    - Accept **kwargs in __init__ (entry.data may contain extra keys like
      'router_type' that are stripped before construction, but clients must be
      tolerant of unknown kwargs regardless).
    - Track _logged_in: bool internally.
    - Implement async_logout() as a no-op when _logged_in is False (prevents
      ZTE lockout on failed login).
    """

    def __init__(self, host: str, username: str, password: str, **kwargs) -> None:
        self._host = host
        self._username = username
        self._password = password
        self._logged_in: bool = False

    @abstractmethod
    async def async_login(self) -> None:
        """Authenticate. Raise AuthError on bad credentials.
        Must set self._logged_in = True on success."""

    @abstractmethod
    async def async_fetch_data(self) -> RouterData:
        """Fetch all data. Called only after async_login(). Raise FetchError on failure."""

    @abstractmethod
    async def async_logout(self) -> None:
        """Clean up session. Must not raise. Must be a no-op if _logged_in is False."""

    @abstractmethod
    async def async_close(self) -> None:
        """Close the underlying aiohttp.ClientSession."""

    @abstractmethod
    async def async_get_unique_id(self) -> str:
        """Return a stable unique identifier (e.g. router MAC).
        Called once after a successful async_login() during config flow validation."""


# ── Registry (populated by router modules at import) ─────────────────────────

ROUTER_REGISTRY: dict[str, RouterStrategy] = {}
# Entries are added by routers/arris_tg3442de.py and routers/zte_f660.py
# at module import time via ROUTER_REGISTRY["key"] = RouterStrategy(...)
