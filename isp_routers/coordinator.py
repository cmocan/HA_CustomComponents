"""DataUpdateCoordinator for the ISP Routers integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

try:
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import ConfigEntryAuthFailed
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
    from .const import DEFAULT_POLL_INTERVAL, DOMAIN
    from .router_registry import AuthError, FetchError, RouterClient, RouterStrategy
    from .data import RouterData
except ImportError:
    from const import DEFAULT_POLL_INTERVAL, DOMAIN  # type: ignore[no-redef]
    from router_registry import AuthError, FetchError, RouterClient, RouterStrategy  # type: ignore[no-redef]
    from data import RouterData  # type: ignore[no-redef]

    class HomeAssistant:  # type: ignore[no-redef]
        pass

    class ConfigEntryAuthFailed(Exception):  # type: ignore[no-redef]
        pass

    class UpdateFailed(Exception):  # type: ignore[no-redef]
        pass

    class DataUpdateCoordinator:  # type: ignore[no-redef]
        def __init__(self, *a, **kw): pass
        def __class_getitem__(cls, item): return cls

_LOGGER = logging.getLogger(__name__)


class IspRoutersCoordinator(DataUpdateCoordinator[RouterData]):
    """Coordinator for one ISP router instance (one config entry)."""

    def __init__(
        self,
        hass: HomeAssistant,
        strategy: RouterStrategy,
        client: RouterClient,
        poll_interval: int = DEFAULT_POLL_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name=f"{DOMAIN}_{strategy.display_name}",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._client = client
        self.strategy = strategy
        self.client_lock = asyncio.Lock()

    @property
    def client(self) -> RouterClient:
        return self._client

    async def _async_update_data(self) -> RouterData:
        """Login → fetch → logout. Always logout in finally."""
        async with self.client_lock:
            try:
                await self._client.async_login()
                return await self._client.async_fetch_data()
            except ConfigEntryAuthFailed:
                raise
            except AuthError as err:
                raise ConfigEntryAuthFailed(str(err)) from err
            except FetchError as err:
                raise UpdateFailed(str(err)) from err
            except Exception as err:
                raise UpdateFailed(
                    f"Unexpected error from {self.strategy.display_name}: {err}"
                ) from err
            finally:
                await self._client.async_logout()   # no-op if login never succeeded

    async def async_close(self) -> None:
        """Close the underlying HTTP session."""
        await self._client.async_close()
