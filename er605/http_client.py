"""Async HTTP client for the TP-Link ER605 web API.

Handles the full authentication flow:
  1. POST /locale?form=lang           — fetch uptime (password salt)
  2. POST /login?form=login {get}     — fetch RSA public key
  3. Encrypt PASSWORD_<uptime>        — custom no-padding RSA
  4. POST /login?form=login {login}   — obtain stok + sysauth cookie

After login, call post() for authenticated requests.  The stok is
embedded in the URL path; the sysauth cookie is sent automatically
by the session's cookie jar.

IMPORTANT: The router sets a 'sysauth' cookie on the IP address host.
aiohttp's default CookieJar rejects IP-address cookies for security.
This client creates its own session with CookieJar(unsafe=True) so
the cookie is stored and replayed correctly.

No external dependencies: aiohttp is bundled with Home Assistant.
RSA encryption uses only Python's built-in pow().
"""

from __future__ import annotations

import json
import logging

import aiohttp

from .const import (
    API_FIRMWARE,
    API_IFACE_STATUS,
    API_IFSTAT,
    API_IPSTATS,
    API_IPV6_STATUS,
    API_LOGIN,
    API_LOCALE,
    API_ONLINE_STATE,
    API_SWITCH_STATE,
    API_SYS_STATUS,
    API_TIME,
    API_WAN_MODE,
    EC_FORM_NOT_FOUND,
    EC_OK,
    EC_WRONG_CREDS,
)

_LOGGER = logging.getLogger(__name__)

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


class HttpLoginError(Exception):
    """Wrong credentials — error_code 700.  Triggers ConfigEntryAuthFailed."""


class HttpSessionError(Exception):
    """Stale/invalid stok — caller should re-login once and retry."""


class HttpError(Exception):
    """Any other HTTP or connectivity error."""


class ER605HttpClient:
    """Async HTTPS client managing a single authenticated ER605 session.

    Owns its own aiohttp.ClientSession so it can use CookieJar(unsafe=True)
    — required because the router sets the 'sysauth' cookie on an IP address
    host, which the shared HA session's CookieJar silently discards.

    Always call async_close() when done (or use async with).
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: int = 10,
    ) -> None:
        self._host     = host
        self._username = username
        self._password = password
        self._timeout  = aiohttp.ClientTimeout(total=timeout)
        self._base_url = f"https://{host}"
        self._stok: str | None = None
        # Own session: ssl=False for self-signed cert, unsafe=True so
        # the sysauth cookie (set on an IP-address host) is accepted.
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=False),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )

    async def async_close(self) -> None:
        """Close the underlying aiohttp session."""
        if not self._session.closed:
            await self._session.close()

    # ── Public: authentication ────────────────────────────────────────────────

    async def login(self) -> str:
        """Run the 3-step login flow.  Returns the new stok token.

        Note: the initial GET /webpages/login.html is skipped — the router
        sends both Content-Length and Transfer-Encoding in that response,
        which aiohttp's strict parser rejects.  The TLS session is
        established by the first POST to the locale endpoint instead.
        """
        login_headers = {
            **_BASE_HEADERS,
            "Origin":  self._base_url,
            "Referer": f"{self._base_url}/webpages/login.html",
        }

        # Step 1: fetch uptime (password salt) — also establishes TLS session
        uptime = await self._get_uptime(login_headers)

        # Step 2: fetch RSA public key
        n_hex, e_hex = await self._get_rsa_key(login_headers)

        # Step 3: encrypt and submit credentials
        self._stok = await self._do_login(login_headers, n_hex, e_hex, uptime)
        _LOGGER.debug("Login successful, stok=%s...", self._stok[:8])
        return self._stok

    # ── Public: authenticated API calls ──────────────────────────────────────

    async def post(
        self,
        path: str,
        method: str = "get",
        params: dict | None = None,
    ) -> dict:
        """POST an authenticated API request.

        path   — URL path after /cgi-bin/luci/;stok=<TOKEN>/
                 e.g. "admin/sys_status?form=all_usage"
        method — JSON "method" field value (default "get")
        params — optional dict merged into JSON "params" key
        """
        if self._stok is None:
            raise HttpSessionError("Not logged in")

        url     = f"{self._base_url}/cgi-bin/luci/;stok={self._stok}/{path}"
        payload: dict = {"method": method}
        if params:
            payload["params"] = params

        body = await self._api_post(url, payload)
        ec   = body.get("error_code", "")

        if ec == EC_WRONG_CREDS:
            raise HttpLoginError("Router rejected credentials (error_code 700)")
        if ec == EC_FORM_NOT_FOUND:
            raise HttpError(f"Endpoint not found (error_code 1014) for {path}")
        if ec != EC_OK:
            raise HttpSessionError(f"Unexpected error_code={ec} for {path} — may be stale session")

        return body

    # ── Public: high-level data fetchers ─────────────────────────────────────

    async def get_firmware(self) -> dict:
        return (await self.post(API_FIRMWARE)).get("result", {})

    async def get_interfaces(self) -> list[dict]:
        body = await self.post(API_IFACE_STATUS)
        return body.get("result", {}).get("normal", [])

    async def get_wan_mode(self) -> dict:
        return (await self.post(API_WAN_MODE)).get("result", {})

    async def get_system_status(self) -> dict:
        return (await self.post(API_SYS_STATUS)).get("result", {})

    async def get_online_state(self) -> list[dict]:
        body = await self.post(API_ONLINE_STATE)
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    async def get_switch_state(self) -> list[dict]:
        body = await self.post(API_SWITCH_STATE, params={})
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    async def get_ipv6_status(self) -> list[dict]:
        body = await self.post(API_IPV6_STATUS)
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    async def get_time(self) -> dict:
        return (await self.post(API_TIME)).get("result", {})

    async def get_ifstat(self) -> list[dict]:
        body = await self.post(API_IFSTAT, params={})
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    async def get_ipstats(self) -> list[dict]:
        body = await self.post(API_IPSTATS, params={})
        result = body.get("result", [])
        return result if isinstance(result, list) else []

    # ── Internal: login helpers ───────────────────────────────────────────────

    async def _get_uptime(self, headers: dict) -> str:
        url = f"{self._base_url}/{API_LOCALE}"
        async with self._session.post(
            url, data={"operation": "read"}, headers=headers,
            timeout=self._timeout,
        ) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        uptime = str(body.get("result", {}).get("uptime", "0"))
        _LOGGER.debug("Router uptime for salt: %s s", uptime)
        return uptime

    async def _get_rsa_key(self, headers: dict) -> tuple[str, str]:
        url     = f"{self._base_url}/{API_LOGIN}"
        payload = {"method": "get"}
        body    = await self._api_post(url, payload, headers=headers)
        params  = (
            body.get("data", {}).get("password")
            or body.get("result", {}).get("password")
        )
        if not params or len(params) < 2:
            raise HttpError(f"Unexpected RSA key response: {body}")
        return params[0], params[1]   # n_hex, e_hex

    async def _do_login(
        self, headers: dict, n_hex: str, e_hex: str, uptime: str
    ) -> str:
        plaintext    = f"{self._password}_{uptime}"
        enc_password = _rsa_encrypt_nopadding(plaintext, n_hex, e_hex)

        url     = f"{self._base_url}/{API_LOGIN}"
        payload = {
            "method": "login",
            "params": {
                "username": self._username,
                "password": enc_password,
            },
        }
        body = await self._api_post(url, payload, headers=headers)
        ec   = body.get("error_code", "")

        if ec == EC_WRONG_CREDS:
            raise HttpLoginError("Wrong username or password (error_code 700)")
        if ec != EC_OK:
            raise HttpError(f"Login failed, error_code={ec}: {body}")

        stok = (
            body.get("data", {}).get("stok")
            or body.get("result", {}).get("stok")
        )
        if not stok:
            raise HttpError(f"Login succeeded but no stok in response: {body}")
        return stok

    # ── Internal: raw HTTP helpers ────────────────────────────────────────────

    async def _api_post(
        self,
        url: str,
        payload: dict,
        headers: dict | None = None,
    ) -> dict:
        """POST with data=<url-encoded JSON> and return parsed JSON body."""
        h = {
            **_BASE_HEADERS,
            "Origin":  self._base_url,
            "Referer": f"{self._base_url}/webpages/index.html",
        }
        if headers:
            h = {**h, **headers}

        try:
            async with self._session.post(
                url,
                data={"data": json.dumps(payload, separators=(",", ":"))},
                headers=h,
                timeout=self._timeout,
            ) as resp:
                resp.raise_for_status()
                return await resp.json(content_type=None)
        except aiohttp.ClientResponseError as err:
            raise HttpError(f"HTTP {err.status} for {url}") from err
        except aiohttp.ClientError as err:
            raise HttpError(f"Connection error for {url}: {err}") from err


# ── RSA helper ────────────────────────────────────────────────────────────────

def _rsa_encrypt_nopadding(plaintext: str, n_hex: str, e_hex: str) -> str:
    """Replicate the router's custom RSA no-padding scheme (encrypt.js).

    Algorithm (from JavaScript source nopadding()):
      1. Encode plaintext as UTF-8 bytes.
      2. Right-zero-pad to the key length in bytes.
      3. Interpret as a big-endian integer m.
      4. Compute c = m^e mod n.
      5. Return c as a zero-padded lowercase hex string (256 chars for 1024-bit).
    """
    n = int(n_hex, 16)
    e = int(e_hex, 16)

    pt_bytes = plaintext.encode("utf-8")
    key_len  = (n.bit_length() + 7) // 8   # bytes

    if len(pt_bytes) > key_len:
        raise ValueError(
            f"Plaintext ({len(pt_bytes)} B) exceeds RSA key length ({key_len} B)"
        )

    padded = pt_bytes + b"\x00" * (key_len - len(pt_bytes))
    m      = int.from_bytes(padded, "big")
    c      = pow(m, e, n)

    return format(c, "x").zfill(key_len * 2)
