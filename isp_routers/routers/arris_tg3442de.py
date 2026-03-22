"""Arris TG3442DE router client (Vodafone cable modem)."""
from __future__ import annotations

import asyncio
import binascii
import hashlib
import json
import logging
import random
import re
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _extract_js_var(html: str, var_name: str) -> str | None:
    """Extract a JS variable value from the Arris login page HTML."""
    pattern = rf'var\s+{re.escape(var_name)}\s*=\s*["\']([^"\']*)["\']'
    m = re.search(pattern, html)
    return m.group(1) if m else None


def _pbkdf2_key(password: str, salt: bytes, iterations: int = 1000) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations, dklen=16)


def _aes_ccm_encrypt(key: bytes, iv: bytes, auth_data: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    """Encrypt with AES-CCM (128-bit tag, matching SJCL DEFAULT_SJCL_TAGLENGTH=128).
    Returns (ciphertext, tag).
    """
    from Crypto.Cipher import AES
    cipher = AES.new(key, AES.MODE_CCM, nonce=iv, mac_len=16)
    cipher.update(auth_data)
    ciphertext, tag = cipher.encrypt_and_digest(plaintext)
    return ciphertext, tag


def _aes_ccm_decrypt(key: bytes, iv: bytes, auth_data: bytes, ciphertext_with_tag: bytes) -> bytes:
    """Decrypt AES-CCM ciphertext (last 16 bytes are the tag). Returns plaintext."""
    from Crypto.Cipher import AES
    ciphertext = ciphertext_with_tag[:-16]
    tag = ciphertext_with_tag[-16:]
    cipher = AES.new(key, AES.MODE_CCM, nonce=iv, mac_len=16)
    cipher.update(auth_data)
    return cipher.decrypt_and_verify(ciphertext, tag)


# ── Client ────────────────────────────────────────────────────────────────────

try:
    from ..router_registry import AuthError, FetchError, RouterClient
    from ..data import ConnectedDevice, DslChannel, LanPort, RouterData, WanStatus
except ImportError:
    # Standalone execution outside HA (dev tools)
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from router_registry import AuthError, FetchError, RouterClient  # noqa: E402
    from data import ConnectedDevice, DslChannel, LanPort, RouterData, WanStatus  # noqa: E402


class ArrisClient(RouterClient):
    """Client for Arris TG3442DE (Vodafone Station cable modem)."""

    def __init__(self, host: str, username: str, password: str, **kwargs) -> None:
        super().__init__(host, username, password, **kwargs)
        self._csrf_nonce: str = ""
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # CookieJar(unsafe=True) is required to store cookies issued from IP addresses.
            self._session = aiohttp.ClientSession(
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": f"http://{self._host}",
                    "Referer": f"http://{self._host}/",
                },
            )
        return self._session

    def _url(self, path: str) -> str:
        return f"http://{self._host}/{path.lstrip('/')}"

    @staticmethod
    def _nonce() -> str:
        """Return a 5-digit anti-cache nonce matching the browser's getNonce() logic."""
        return str(random.random())[2:7]

    # ── Auth ─────────────────────────────────────────────────────────────────

    async def async_login(self) -> None:
        """Two-step AES-CCM login (discovered via browser inspection of Arris TG3442DE firmware).

        Login flow:
          1. GET /  -> HTML containing myIv, mySalt, currentSessionId JS variables
          2. Derive key via PBKDF2-SHA256(password, mySalt, 1000 iters, 128-bit)
             Encrypt JSON payload with AES-CCM-128 (auth_data="loginPassword")
          3. POST /php/ajaxSet_Password.php (application/json)
             -> p_status="AdminMatch" on success (admin credentials matched)
             -> encryptData field contains AES-CCM(key, nonce, auth_data="nonce") of csrf_nonce
          4. Decrypt encryptData to get csrf_nonce; set as csrfNonce header for all requests

        Cookie notes:
          - PHPSESSID is issued on GET / and must accompany the login POST
          - CookieJar(unsafe=True) is required for IP-address cookie acceptance
        """
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=15)
        nonce = self._nonce()

        # Step 1: GET / — get PHPSESSID cookie and extract IV, salt, session_id
        try:
            async with session.get(self._url("/"), timeout=timeout) as resp:
                resp.raise_for_status()
                html = await resp.text()
        except Exception as exc:
            raise FetchError(f"Arris: cannot reach login page: {exc}") from exc

        iv_hex     = _extract_js_var(html, "myIv")
        salt_hex   = _extract_js_var(html, "mySalt")
        session_id = _extract_js_var(html, "currentSessionId") or ""

        if not iv_hex or not salt_hex:
            _LOGGER.debug("Arris login page HTML (first 3000 chars):\n%s", html[:3000])
            raise FetchError("Arris: could not extract IV/salt from login page")

        try:
            iv   = binascii.unhexlify(iv_hex)
            salt = binascii.unhexlify(salt_hex)
        except binascii.Error as exc:
            raise FetchError(f"Arris: bad IV/salt encoding: {exc}") from exc

        # Step 2: POST logout — clear any existing admin session (browser always does this)
        try:
            async with session.post(
                self._url(f"/php/logout.php?_n={nonce}"),
                headers={"csrfNonce": "undefined"},
                data=None,
                timeout=timeout,
            ) as resp:
                await resp.read()
        except Exception:
            pass  # ignore logout errors

        # Step 3: Derive key, encrypt password payload
        # Plaintext is the exact JS string: '{"Password": "<pwd>", "Nonce": "<sid>"}'
        key = _pbkdf2_key(self._password, salt)
        plaintext = (
            '{"Password": "' + self._password + '", "Nonce": "' + session_id + '"}'
        ).encode()
        ciphertext, tag = _aes_ccm_encrypt(key, iv, b"loginPassword", plaintext)
        encrypted_hex = (ciphertext + tag).hex()   # SJCL returns hex, not base64

        # Step 4: POST encrypted credentials — server issues new PHPSESSID on success
        try:
            async with session.post(
                self._url(f"/php/ajaxSet_Password.php?_n={nonce}"),
                headers={"csrfNonce": "undefined"},
                json={"EncryptData": encrypted_hex, "Name": self._username, "AuthData": "loginPassword"},
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                result = await resp.json(content_type=None)
        except AuthError:
            raise
        except Exception as exc:
            raise FetchError(f"Arris: password POST failed: {exc}") from exc

        status = str(result.get("p_status", ""))
        if "Match" not in status and status != "Default":
            if "Lockout" in status:
                raise AuthError(f"Arris: account locked out (too many failed attempts)")
            raise AuthError(f"Arris: login failed (p_status={status!r})")

        # Step 5: Decrypt encryptData to get csrf_nonce
        try:
            enc_bytes = binascii.unhexlify(result.get("encryptData", ""))
            self._csrf_nonce = _aes_ccm_decrypt(key, iv, b"nonce", enc_bytes).decode()
        except Exception as exc:
            raise FetchError(f"Arris: could not decrypt csrf_nonce: {exc}") from exc

        # Step 6: GET / with new PHPSESSID — browser always does this to activate the session
        try:
            async with session.get(self._url("/"), timeout=timeout) as resp:
                await resp.read()
        except Exception as exc:
            raise FetchError(f"Arris: session activation GET failed: {exc}") from exc

        session.headers.update({"csrfNonce": self._csrf_nonce})
        self._logged_in = True

    async def async_logout(self) -> None:
        """POST logout. No-op if never logged in."""
        if not self._logged_in or self._session is None:
            return
        self._logged_in = False
        try:
            async with self._session.post(
                self._url(f"/php/logout.php?_n={self._nonce()}"),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                await resp.read()
        except Exception:
            pass

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # ── Fetch ────────────────────────────────────────────────────────────────

    async def async_fetch_data(self) -> RouterData:
        """Fetch status, DOCSIS, and overview pages concurrently."""
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=15)

        nonce = self._nonce()
        try:
            responses = await asyncio.gather(
                session.get(self._url(f"/php/status_status_data.php?_n={nonce}"), timeout=timeout),
                session.get(self._url(f"/php/status_docsis_data.php?_n={nonce}"), timeout=timeout),
                session.get(self._url(f"/php/overview_data.php?_n={nonce}"), timeout=timeout),
            )
            status_html   = await responses[0].text()
            docsis_html   = await responses[1].text()
            overview_html = await responses[2].text()
            for resp in responses:
                resp.release()
        except Exception as exc:
            raise FetchError(f"Arris: data fetch failed: {exc}") from exc

        uptime     = self._parse_uptime(status_html)
        firmware   = self._parse_js_var(status_html, "js_FWVersion")
        devices    = self._parse_devices(overview_html)
        voip_lines = self._parse_voip_lines(overview_html)
        wan        = self._parse_wan(status_html)
        firewall   = self._parse_js_var(status_html, "js_FirewallConfig")
        lan_net    = self._parse_js_var(status_html, "js_ipv4LANaddr")
        lan_ports  = self._parse_lan_ports(status_html)

        return RouterData(
            model="TG3442DE",
            firmware=firmware,
            uptime_seconds=uptime,
            connected_devices=devices,
            wan_status=wan,
            firewall_enabled=(firewall.lower() == "on") if firewall else None,
            lan_network=lan_net,
            lan_ports=lan_ports,
            docsis_channels=self._parse_docsis(docsis_html),
            voip_lines=voip_lines,
            poll_monotonic=time.monotonic(),
        )

    # ── Parse helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_js_var(html: str, var_name: str) -> str | None:
        m = re.search(rf"(?:var\s+)?{re.escape(var_name)}\s*=\s*[\"']([^\"']*)[\"']", html)
        return m.group(1).strip() if m else None

    @staticmethod
    def _parse_uptime(html: str) -> int | None:
        """Uptime returned as 'days,hours,minutes' string."""
        raw = ArrisClient._parse_js_var(html, "js_UptimeSinceReboot")
        if not raw:
            return None
        try:
            parts = [int(x) for x in raw.split(",")]
            if len(parts) == 3:
                d, h, m = parts
                return d * 86400 + h * 3600 + m * 60
        except ValueError:
            pass
        return None

    @staticmethod
    def _parse_power(raw: str) -> float | None:
        """Parse PowerLevel field — format is 'X.X/Y.Y' (relative/absolute) or 'X.X~Y.Y'.
        Takes the first value (relative dBmV).
        """
        try:
            # Split on / or ~ and take first part, then strip non-numeric chars
            first = re.split(r"[/~]", str(raw))[0].strip()
            clean = re.sub(r"[^\d.\-]", "", first)
            return float(clean) if clean else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_frequency(raw) -> float | None:
        """Parse Frequency field — SC-QAM: integer MHz; OFDM: '864~959' range string.
        Returns the start frequency in MHz.
        """
        try:
            # Take first value if range (e.g. '864~959')
            first = str(raw).split("~")[0].strip()
            return float(first)   # already in MHz
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _parse_docsis(html: str) -> list[DslChannel]:
        channels: list[DslChannel] = []

        # Downstream
        ds_match = re.search(r"(?:var\s+)?json_dsData\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if ds_match:
            try:
                for entry in json.loads(ds_match.group(1)):
                    try:
                        channels.append(DslChannel(
                            channel_id=int(entry.get("ChannelID", 0)),
                            direction="downstream",
                            frequency_mhz=ArrisClient._parse_frequency(entry.get("Frequency")),
                            power_dbmv=ArrisClient._parse_power(entry.get("PowerLevel", "")),
                            snr_db=ArrisClient._parse_power(entry.get("SNRLevel", "")),
                            locked=str(entry.get("LockStatus", "")) in {"1", "Locked"},
                        ))
                    except (ValueError, TypeError, KeyError):
                        pass
            except json.JSONDecodeError:
                pass

        # Upstream
        us_match = re.search(r"(?:var\s+)?json_usData\s*=\s*(\[.*?\]);", html, re.DOTALL)
        if us_match:
            try:
                for entry in json.loads(us_match.group(1)):
                    try:
                        channels.append(DslChannel(
                            channel_id=int(entry.get("ChannelID", 0)),
                            direction="upstream",
                            frequency_mhz=ArrisClient._parse_frequency(entry.get("Frequency")),
                            power_dbmv=ArrisClient._parse_power(entry.get("PowerLevel", "")),
                            snr_db=None,   # upstream does not report SNR
                            locked=str(entry.get("LockStatus", "")) in {"1", "Locked"},
                        ))
                    except (ValueError, TypeError, KeyError):
                        pass
            except json.JSONDecodeError:
                pass

        return channels

    @staticmethod
    def _parse_devices(html: str) -> list[ConnectedDevice]:
        """Parse connected devices from overview_data.php.

        Router reports devices in three separate arrays:
          json_lanAttachedDevice        — wired LAN
          json_primaryWlanAttachedDevice — primary WiFi
          json_guestWlanAttachedDevice   — guest WiFi
        Fields per entry: MAC, Active (bool), HostName, IPv4, Interface
        """
        devices: list[ConnectedDevice] = []
        for var, network_type in [
            ("json_lanAttachedDevice",         "LAN"),
            ("json_primaryWlanAttachedDevice",  "WLAN"),
            ("json_guestWlanAttachedDevice",    "WLAN"),
        ]:
            match = re.search(rf"(?:var\s+)?{re.escape(var)}\s*=\s*(\[.*?\]);", html, re.DOTALL)
            if not match:
                continue
            try:
                for entry in json.loads(match.group(1)):
                    mac = str(entry.get("MAC", "")).lower().replace("-", ":").strip()
                    if not mac or mac == "00:00:00:00:00:00":
                        continue
                    devices.append(ConnectedDevice(
                        mac=mac,
                        ip=str(entry.get("IPv4", "")).strip() or None,
                        hostname=str(entry.get("HostName", "")).strip() or None,
                        is_active=bool(entry.get("Active", False)),
                        network_type=network_type,
                        port=str(entry.get("Interface", "")).strip() or None,
                    ))
            except (json.JSONDecodeError, TypeError):
                pass
        return devices

    @staticmethod
    def _parse_voip_lines(html: str) -> int | None:
        """VoIP line count from js_numbersPhone variable."""
        raw = re.search(r"(?:var\s+)?js_numbersPhone\s*=\s*[\"'](\d+)[\"']", html)
        if raw:
            try:
                return int(raw.group(1))
            except ValueError:
                pass
        return None

    @staticmethod
    def _parse_wan(html: str) -> list[WanStatus]:
        """Build WanStatus from status_status_data.php JS variables."""
        ip      = ArrisClient._parse_js_var(html, "js_ipv4addr")
        gateway = ArrisClient._parse_js_var(html, "js_ipv4gateway")
        dns1    = ArrisClient._parse_js_var(html, "js_ipv4PrimDNS")
        dns2    = ArrisClient._parse_js_var(html, "js_ipv4SecondDNS")
        if not ip:
            return [WanStatus(name="WAN", is_up=False, ip=None, gateway=None, dns1=None, dns2=None)]
        return [WanStatus(name="WAN", is_up=True, ip=ip, gateway=gateway, dns1=dns1, dns2=dns2)]

    @staticmethod
    def _parse_lan_ports(html: str) -> list[LanPort]:
        """Parse LAN port status and bitrate from status_status_data.php."""
        ports: list[LanPort] = []
        for i in range(1, 5):
            status  = ArrisClient._parse_js_var(html, f"js_ethernet_port{i}_status")
            bitrate = ArrisClient._parse_js_var(html, f"js_ethernet_port{i}_bitrate")
            if status is None:
                continue
            ports.append(LanPort(
                port_id=i,
                is_active=(status.lower() == "active"),
                bitrate=bitrate if bitrate and bitrate != "-" else None,
            ))
        return ports

    async def async_get_unique_id(self) -> str:
        """Return CM MAC from overview page."""
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            async with session.get(
                self._url(f"/php/overview_data.php?_n={self._nonce()}"), timeout=timeout
            ) as resp:
                html = await resp.text()
            mac = self._parse_js_var(html, "js_CmMac")
            if mac:
                return mac.lower().replace("-", ":").strip()
        except Exception:
            pass
        return hashlib.sha256(self._host.encode()).hexdigest()[:12]


# ── Entity descriptors ────────────────────────────────────────────────────────

try:
    from homeassistant.components.binary_sensor import BinarySensorEntityDescription
    from homeassistant.components.sensor import SensorEntityDescription
except ImportError:
    try:
        from ..router_registry import SensorEntityDescription, BinarySensorEntityDescription  # type: ignore[no-redef]
    except ImportError:
        from router_registry import SensorEntityDescription, BinarySensorEntityDescription  # type: ignore[no-redef]

try:
    from ..router_registry import (
        ROUTER_REGISTRY,
        ChannelBinarySensorTemplate,
        ChannelSensorTemplate,
        RouterStrategy,
    )
except ImportError:
    from router_registry import (  # noqa: E402
        ROUTER_REGISTRY,
        ChannelBinarySensorTemplate,
        ChannelSensorTemplate,
        RouterStrategy,
    )

ARRIS_SENSOR_DESCS: list = [
    # System
    SensorEntityDescription(
        key="uptime",
        name="Uptime",
        device_class="duration",
        native_unit_of_measurement="s",
        state_class="total_increasing",
    ),
    # Internet (WAN)
    SensorEntityDescription(
        key="wan_ip",
        name="WAN IP",
        icon="mdi:ip-network",
    ),
    SensorEntityDescription(
        key="wan_gateway",
        name="WAN Gateway",
        icon="mdi:router-network",
    ),
    SensorEntityDescription(
        key="wan_dns1",
        name="WAN DNS",
        icon="mdi:dns",
    ),
    # LAN
    SensorEntityDescription(
        key="lan_network",
        name="LAN Network",
        icon="mdi:lan",
    ),
    SensorEntityDescription(
        key="lan_port_1_speed",
        name="LAN Port 1 Speed",
        icon="mdi:ethernet",
    ),
    SensorEntityDescription(
        key="lan_port_2_speed",
        name="LAN Port 2 Speed",
        icon="mdi:ethernet",
    ),
    SensorEntityDescription(
        key="lan_port_3_speed",
        name="LAN Port 3 Speed",
        icon="mdi:ethernet",
    ),
    SensorEntityDescription(
        key="lan_port_4_speed",
        name="LAN Port 4 Speed",
        icon="mdi:ethernet",
    ),
    # Devices
    SensorEntityDescription(
        key="connected_devices_total",
        name="Connected Devices",
        icon="mdi:devices",
        state_class="measurement",
    ),
    SensorEntityDescription(
        key="active_devices",
        name="Active Devices",
        icon="mdi:lan-connect",
        state_class="measurement",
    ),
    SensorEntityDescription(
        key="voip_lines",
        name="VoIP Lines",
        icon="mdi:phone",
    ),
]

ARRIS_BINARY_SENSOR_DESCS: list = [
    BinarySensorEntityDescription(
        key="wan_connected",
        name="WAN Connected",
        device_class="connectivity",
    ),
    BinarySensorEntityDescription(
        key="firewall_enabled",
        name="Firewall",
        icon="mdi:shield-check",
    ),
    BinarySensorEntityDescription(
        key="lan_port_1_active",
        name="LAN Port 1",
        device_class="plug",
    ),
    BinarySensorEntityDescription(
        key="lan_port_2_active",
        name="LAN Port 2",
        device_class="plug",
    ),
    BinarySensorEntityDescription(
        key="lan_port_3_active",
        name="LAN Port 3",
        device_class="plug",
    ),
    BinarySensorEntityDescription(
        key="lan_port_4_active",
        name="LAN Port 4",
        device_class="plug",
    ),
]

ARRIS_CHANNEL_SENSOR_TEMPLATES: list[ChannelSensorTemplate] = []       # DOCSIS not exposed
ARRIS_CHANNEL_BINARY_SENSOR_TEMPLATES: list[ChannelBinarySensorTemplate] = []  # DOCSIS not exposed


# ── Self-register ─────────────────────────────────────────────────────────────

ROUTER_REGISTRY["arris_tg3442de"] = RouterStrategy(
    display_name="Arris TG3442DE",
    client_class=ArrisClient,
    sensor_descs=ARRIS_SENSOR_DESCS,
    binary_sensor_descs=ARRIS_BINARY_SENSOR_DESCS,
    channel_sensor_templates=ARRIS_CHANNEL_SENSOR_TEMPLATES,
    channel_binary_sensor_templates=ARRIS_CHANNEL_BINARY_SENSOR_TEMPLATES,
    supports_device_tracker=True,
)
