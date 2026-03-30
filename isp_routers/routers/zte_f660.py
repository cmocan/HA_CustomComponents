"""ZTE ZXHN F660 / F6600R router client.

Login flow (discovered by browser inspection of F6600R firmware):
  1. GET  loginData/login_entry       -> JSON  { sess_token }
  2. GET  loginData/login_token       -> XML   <ajax_response_xml_root>TOKEN</ajax_response_xml_root>
  3. POST loginData/login_entry       -> JSON  { sess_token, login_need_refresh:true } = success
                                         Set-Cookie: SID_HTTPS_=<sid>
  4. POST loginData/login_changepwd   -> action=changepwd_cancel (MANDATORY)
       - Cookie header must include SID from step 3
       - _sessionTOKEN is a HARDCODED constant "iPFA8znE0U9PAGPqQ1qDTlud" from the router JS
         (NOT the dynamic sess_token from step 3)
       - Set-Cookie: SID_HTTPS_=<new_sid>  (rotated)
  5. GET  /  (with Cookie: SID from step 4)
       - Simulates the browser page reload triggered by login_need_refresh:true
       - This activates the session server-side; without it all data requests return SessionTimeout

Data fetch pattern (discovered by browser HAR capture):
  Each section requires a menuView request BEFORE the menuData request:
    menuView&_tag=ethWanStatus&Menu3Location=0  ->  menuData wan_internetstatus_lua.lua
    menuView&_tag=statusMgr&Menu3Location=0     ->  menuData devmgr_statusmgr_lua.lua
  The home-page accessdev endpoint needs no menuView.

Cookie management:
  aiohttp's CookieJar(unsafe=True) stores the SID_HTTPS_ cookie but does NOT send it in requests
  because it is issued from an IP address with Secure+SameSite=strict attributes.
  The SID is therefore tracked manually and injected into every request header.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import random
import re
import string
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import quote

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Hardcoded _sessionTOKEN used by the router's doChgpwd JS function.
# This is baked into the firmware HTML and never changes between sessions.
_CHANGEPWD_SESSION_TOKEN = "iPFA8znE0U9PAGPqQ1qDTlud"

# RSA 2048-bit public key hardcoded in router firmware (main_page.html).
# Used to compute the 'Check' header: RSA_encrypt(SHA256(POST_body)).
_RSA_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAodPTerkUVCYmv28SOfRV
7UKHVujx/HjCUTAWy9l0L5H0JV0LfDudTdMNPEKloZsNam3YrtEnq6jqMLJV4ASb
1d6axmIgJ636wyTUS99gj4BKs6bQSTUSE8h/QkUYv4gEIt3saMS0pZpd90y6+B/9
hZxZE/RKU8e+zgRqp1/762TB7vcjtjOwXRDEL0w71Jk9i8VUQ59MR1Uj5E8X3WIc
fYSK5RWBkMhfaTRM6ozS9Bqhi40xlSOb3GBxCmliCifOJNLoO9kFoWgAIw5hkSIb
GH+4Csop9Uy8VvmmB+B3ubFLN35qIa5OG5+SDXn4L7FeAA5lRiGxRi8tsWrtew8w
nwIDAQAB
-----END PUBLIC KEY-----"""

# Model choices exposed to the config flow (kept for future model-specific tuning)
ZTE_MODEL_CHOICES = ["f660", "f6600r", "h288a", "h388x", "f6640"]


# -- URL / parsing helpers ---------------------------------------------------

def _zte_url(host: str, type_: str, tag: str, **params: str) -> str:
    """Build a ZTE HTTPS API URL with a timestamp cache-buster."""
    ts = uuid.uuid4().int >> 64
    base = f"https://{host}/?_type={type_}&_tag={tag}"
    for k, v in params.items():
        base += f"&{k}={v}"
    return f"{base}&_={ts}"


def _parse_instances(xml_text: str, container_tag: str) -> list[dict[str, str]]:
    """Parse ZTE XML: return list of dicts from <Instance> blocks inside container_tag.

    ZTE XML format:
      <OBJ_FOO_ID>
        <Instance>
          <ParaName>Key1</ParaName><ParaValue>Val1</ParaValue>
          <ParaName>Key2</ParaName><ParaValue>Val2</ParaValue>
        </Instance>
      </OBJ_FOO_ID>
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    container = root.find(container_tag)
    if container is None:
        return []
    results: list[dict[str, str]] = []
    for inst in container.findall("Instance"):
        children = list(inst)
        d: dict[str, str] = {}
        i = 0
        while i + 1 < len(children):
            name_el, val_el = children[i], children[i + 1]
            if name_el.tag == "ParaName" and val_el.tag == "ParaValue":
                d[name_el.text or ""] = val_el.text or ""
            i += 2
        if d:
            results.append(d)
    return results


def _xml_root_text(xml_text: str) -> str:
    """Return the text content of the XML root element (used for login_token)."""
    try:
        return ET.fromstring(xml_text).text or ""
    except ET.ParseError:
        return ""


def _is_success(xml_text: str) -> bool:
    try:
        root = ET.fromstring(xml_text)
        err = root.findtext("IF_ERRORSTR") or ""
        return err.upper() in {"SUCC", "SUCCESS", "OK"}
    except ET.ParseError:
        return False


def _extract_session_tmp_token(html: str) -> str:
    """Extract _sessionTmpToken from menuView HTML (hex-encoded JS string)."""
    m = re.search(r'_sessionTmpToken\s*=\s*"((?:\\x[0-9a-fA-F]{2})+)"', html)
    if m:
        hex_str = m.group(1)
        return bytes(
            int(h, 16)
            for h in re.findall(r'\\x([0-9a-fA-F]{2})', hex_str)
        ).decode('ascii')
    m = re.search(r'_sessionTmpToken\s*=\s*"([^"]+)"', html)
    if m:
        return m.group(1)
    return ""


def _rsa_encrypt_check(data: str) -> str:
    """RSA-PKCS1v15 encrypt *data* with the router's public key, return base64."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key = serialization.load_pem_public_key(_RSA_PUBLIC_KEY_PEM.encode())
    encrypted = public_key.encrypt(data.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def _compute_check_header(post_body: str) -> str:
    """Return the Check header value: RSA(SHA256(post_body))."""
    digest = hashlib.sha256(post_body.encode()).hexdigest()
    return _rsa_encrypt_check(digest)


def _aes_encrypt_value(plaintext: str, key_str: str, iv_str: str) -> str:
    """AES-256-CBC encrypt a field value (ZeroPadding), return base64.

    Matches the router firmware's CryptoJS.AES.encrypt:
      key = SHA256(key_str)           → 32 bytes (AES-256)
      iv  = SHA256(iv_str)[:16]       → 16 bytes (CryptoJS truncates to block size)
      padding = ZeroPadding (pad with \\x00 to 16-byte boundary)
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(key_str.encode()).digest()          # 32 bytes
    iv = hashlib.sha256(iv_str.encode()).digest()[:16]       # 16 bytes
    data = plaintext.encode()
    # ZeroPadding: pad to 16-byte boundary
    pad_len = (16 - len(data) % 16) % 16
    data += b"\x00" * pad_len
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    enc = cipher.encryptor()
    ct = enc.update(data) + enc.finalize()
    return base64.b64encode(ct).decode()


def _aes_decrypt_value(ciphertext_b64: str, key_str: str, iv_str: str) -> str:
    """AES-256-CBC decrypt a base64 field value, strip ZeroPadding.

    For XML response decryption:
      key_str = _sessionTmpToken
      iv_str  = _sessionTmpToken[::-1]  (reversed)
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.sha256(key_str.encode()).digest()
    iv = hashlib.sha256(iv_str.encode()).digest()[:16]
    ct = base64.b64decode(ciphertext_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    dec = cipher.decryptor()
    pt = dec.update(ct) + dec.finalize()
    return pt.rstrip(b"\x00").decode()


def _extract_sid(set_cookie_headers: list[str]) -> str:
    """Return the last SID_HTTPS_ value from a list of Set-Cookie header strings."""
    sid = ""
    for sc in set_cookie_headers:
        if "SID_HTTPS_" in sc:
            sid = sc.split(";")[0].split("=", 1)[1]
    return sid


# -- EncryptionType → hidden-field mapping (from firmware JS) -----------------
# Maps the EncryptionType <select> value to the hidden form fields the browser
# would set via EncryptionParameter[value] in menuView_wlanBasic_m3_3.html.
_ENCRYPTION_TYPE_MAP: dict[str, dict[str, str]] = {
    "No Security": {
        "BeaconType": "None",
    },
    "WEP-OpenSystem": {
        "BeaconType": "Basic",
        "WEPAuthMode": "None",
    },
    "WEP-ShareKey": {
        "BeaconType": "Basic",
        "WEPAuthMode": "SharedAuthentication",
    },
    "WPA-PSK-TKIP": {
        "BeaconType": "WPA",
        "WPAAuthMode": "PSKAuthentication",
        "WPAEncryptType": "TKIPEncryption",
    },
    "WPA-PSK-AES": {
        "BeaconType": "WPA",
        "WPAAuthMode": "PSKAuthentication",
        "WPAEncryptType": "AESEncryption",
    },
    "WPA-PSK-TKIP/AES": {
        "BeaconType": "WPA",
        "WPAAuthMode": "PSKAuthentication",
        "WPAEncryptType": "TKIPandAESEncryption",
    },
    "WPA2-PSK-AES": {
        "BeaconType": "11i",
        "11iAuthMode": "PSKAuthentication",
        "11iEncryptType": "AESEncryption",
    },
    "WPA2-PSK-TKIP": {
        "BeaconType": "11i",
        "11iAuthMode": "PSKAuthentication",
        "11iEncryptType": "TKIPEncryption",
    },
    "WPA2-PSK-TKIP/AES": {
        "BeaconType": "11i",
        "11iAuthMode": "PSKAuthentication",
        "11iEncryptType": "TKIPandAESEncryption",
    },
    "WPA/WPA2-PSK-TKIP": {
        "BeaconType": "WPAand11i",
        "WPAAuthMode": "PSKAuthentication",
        "11iAuthMode": "PSKAuthentication",
        "WPAEncryptType": "TKIPEncryption",
        "11iEncryptType": "TKIPEncryption",
    },
    "WPA/WPA2-PSK-AES": {
        "BeaconType": "WPAand11i",
        "WPAAuthMode": "PSKAuthentication",
        "11iAuthMode": "PSKAuthentication",
        "WPAEncryptType": "AESEncryption",
        "11iEncryptType": "AESEncryption",
    },
    "WPA/WPA2-PSK-TKIP/AES": {
        "BeaconType": "WPAand11i",
        "WPAAuthMode": "PSKAuthentication",
        "11iAuthMode": "PSKAuthentication",
        "WPAEncryptType": "TKIPandAESEncryption",
        "11iEncryptType": "TKIPandAESEncryption",
    },
    "WPA3-SAE": {
        "BeaconType": "WPA3",
        "WPA3AuthMode": "SAEAuthentication",
        "WPA3EncryptType": "AESEncryption",
    },
    "WPA2/WPA3-SAE": {
        "BeaconType": "11iandWPA3",
        "11iAuthMode": "PSKAuthentication",
        "WPA3AuthMode": "SAEAuthentication",
        "11iEncryptType": "TKIPandAESEncryption",
        "WPA3EncryptType": "AESEncryption",
    },
    "WPA2-PSK-AES/WPA3-SAE-AES": {
        "BeaconType": "11iandWPA3",
        "11iAuthMode": "PSKAuthentication",
        "WPA3AuthMode": "SAEAuthentication",
        "11iEncryptType": "AESEncryption",
        "WPA3EncryptType": "AESEncryption",
    },
}

# Encryption types that use PSK (WPA passphrase) — determines _PSKCONIG flag
_PSK_ENCRYPTION_TYPES = frozenset(k for k, v in _ENCRYPTION_TYPE_MAP.items()
                                  if any("PSK" in v.get(f, "") or "SAE" in v.get(f, "")
                                         for f in ("WPAAuthMode", "11iAuthMode", "WPA3AuthMode")))

# Encryption types that use WEP keys — determines _WEPCONIG flag
_WEP_ENCRYPTION_TYPES = frozenset(k for k in _ENCRYPTION_TYPE_MAP
                                  if k.startswith("WEP"))

# AP index → _InstID prefix mapping (dynamic, but these are the known F6600R APs)
_AP_LABELS = {
    1: "DEV.WIFI.AP1",
    2: "DEV.WIFI.AP2",
    5: "DEV.WIFI.AP5",
    6: "DEV.WIFI.AP6",
}


# -- Client ------------------------------------------------------------------

try:
    from ..router_registry import AuthError, FetchError, RouterClient
    from ..data import ConnectedDevice, LanPort, RouterData, WanStatus
except ImportError:
    # Standalone execution outside HA (dev tools)
    import sys as _sys
    import os as _os
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))
    from router_registry import AuthError, FetchError, RouterClient  # noqa: E402
    from data import ConnectedDevice, LanPort, RouterData, WanStatus  # noqa: E402


class ZteClient(RouterClient):
    """Client for ZTE ZXHN F660 / F6600R and compatible routers."""

    # Maps HA service field names → internal composite keys for configure_wifi.
    # Prefix encodes the target AP: ap1_ = AP1 (2.4G primary), etc.
    WIFI_FIELD_MAP: dict[str, str] = {
        "enable_wifi":      "radio_enable",
        "enable_1":         "ap1_enable",
        "ssid_1":           "ap1_essid",
        "passphrase_1":     "ap1_passphrase",
        "ssid_broadcast_1": "ap1_ssid_broadcast",
        "encryption_1":     "ap1_encryption",
        "enable_2":         "ap2_enable",
        "ssid_2":           "ap2_essid",
        "passphrase_2":     "ap2_passphrase",
        "ssid_broadcast_2": "ap2_ssid_broadcast",
        "encryption_2":     "ap2_encryption",
        "enable_5":         "ap5_enable",
        "ssid_5":           "ap5_essid",
        "passphrase_5":     "ap5_passphrase",
        "ssid_broadcast_5": "ap5_ssid_broadcast",
        "encryption_5":     "ap5_encryption",
        "enable_6":         "ap6_enable",
        "ssid_6":           "ap6_essid",
        "passphrase_6":     "ap6_passphrase",
        "ssid_broadcast_6": "ap6_ssid_broadcast",
        "encryption_6":     "ap6_encryption",
    }

    def __init__(self, host: str, username: str, password: str, **kwargs) -> None:
        super().__init__(host, username, password, **kwargs)
        self._session_token: str = ""
        self._sid_cookie: str = ""       # SID_HTTPS_ value tracked manually
        self._session: aiohttp.ClientSession | None = None

    @property
    def _sid_hdr(self) -> dict[str, str]:
        """Cookie header dict with the current SID, for injection into every request."""
        return {"Cookie": f"SID_HTTPS_={self._sid_cookie}"} if self._sid_cookie else {}

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            # CookieJar(unsafe=True) stores cookies from IP-address hosts, but due to
            # the Secure+SameSite=strict attributes it does NOT send them automatically.
            # We track the SID manually and inject it via _sid_hdr in every request.
            cookie_jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=cookie_jar,
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
        return self._session

    # -- Auth ----------------------------------------------------------------

    async def async_login(self) -> None:
        """Five-step ZTE auth flow (discovered via browser inspection + HAR capture)."""
        session = self._get_session()
        host = self._host
        timeout = aiohttp.ClientTimeout(total=15)

        try:
            # Step 1: GET login_entry -> JSON with sess_token
            async with session.get(
                _zte_url(host, "loginData", "login_entry"), timeout=timeout
            ) as resp:
                resp.raise_for_status()
                data1 = await resp.json(content_type=None)
            sess_token: str = data1.get("sess_token", "")

            # Step 2: GET login_token -> XML root text is the raw numeric token
            async with session.get(
                _zte_url(host, "loginData", "login_token"), timeout=timeout
            ) as resp:
                resp.raise_for_status()
                raw2 = await resp.text()
            login_token = _xml_root_text(raw2)

            # Step 3: POST login with SHA256(password + login_token)
            hashed = hashlib.sha256((self._password + login_token).encode()).hexdigest()
            async with session.post(
                f"https://{host}/?_type=loginData&_tag=login_entry",
                data={
                    "action": "login",
                    "Password": hashed,
                    "Username": self._username,
                    "_sessionTOKEN": sess_token,
                },
                timeout=timeout,
            ) as resp:
                resp.raise_for_status()
                data3 = await resp.json(content_type=None)
                sc3 = resp.headers.getall("Set-Cookie", [])

            if data3.get("loginErrMsg"):
                raise AuthError(f"ZTE: {data3['loginErrMsg']}")

            # Capture SID from step 3 (needed for step 4 header injection)
            sid = _extract_sid(sc3)
            new_sess_token: str = data3.get("sess_token", sess_token)

            # Step 4: MANDATORY -- cancel the password-change prompt.
            # The Cookie header must include the SID from step 3.
            # _sessionTOKEN is a HARDCODED value from the router JS (not the dynamic token).
            async with session.post(
                f"https://{host}/?_type=loginData&_tag=login_changepwd",
                data={
                    "action": "changepwd_cancel",
                    "Password": "",
                    "Username": self._username,
                    "NewPassword": "",
                    "_sessionTOKEN": _CHANGEPWD_SESSION_TOKEN,
                    "encode": "",
                },
                headers={"Cookie": f"SID_HTTPS_={sid}"},
                timeout=timeout,
            ) as resp:
                await resp.read()
                sc4 = resp.headers.getall("Set-Cookie", [])

            # Use the rotated SID from step 4 (server may issue a new cookie)
            sid = _extract_sid(sc4) or sid

            # Step 5: GET / to activate the session server-side.
            # The browser does top.location.href = top.location.href after changepwd,
            # which triggers a full page reload. Without this GET the session is not
            # activated and ALL subsequent data requests return SessionTimeout.
            async with session.get(
                f"https://{host}/",
                headers={"Cookie": f"SID_HTTPS_={sid}"},
                timeout=timeout,
            ) as resp:
                await resp.read()

        except AuthError:
            raise
        except Exception as exc:
            raise FetchError(f"ZTE login error: {exc}") from exc

        self._session_token = new_sess_token
        self._sid_cookie = sid
        self._logged_in = True

    # -- WiFi toggle ---------------------------------------------------------

    async def async_set_wifi_enabled(self, enabled: bool) -> None:
        """Enable or disable all WiFi radios (2.4 GHz + 5 GHz).

        The ZTE firmware requires a single POST with indexed fields for ALL
        radios, a Check header (RSA-signed SHA256 of the body), and a dynamic
        _sessionTmpToken extracted from the menuView HTML.
        """
        if not self._logged_in:
            raise AuthError("ZTE: must be logged in before toggling WiFi")

        session = self._get_session()
        host = self._host
        sid_hdr = self._sid_hdr
        timeout = aiohttp.ClientTimeout(total=30)

        # 1. menuView wlanBasic — required to enter the WLAN context and get
        #    the dynamic _sessionTmpToken.
        async with session.get(
            _zte_url(host, "menuView", "wlanBasic", Menu3Location="0"),
            headers=sid_hdr, timeout=timeout,
        ) as resp:
            wlan_html = await resp.text()

        token = _extract_session_tmp_token(wlan_html)
        if not token:
            raise FetchError("ZTE: could not extract _sessionTmpToken from wlanBasic")

        # 2. GET current radio state + timer config.
        async with session.get(
            _zte_url(host, "menuData", "wlan_wlanbasiconoff_lua.lua"),
            headers=sid_hdr, timeout=timeout,
        ) as resp:
            onoff_xml = await resp.text()

        radios = _parse_instances(onoff_xml, "OBJ_WLANSETTING_ID")
        timers = _parse_instances(onoff_xml, "OBJ_WLANTIME_ID")
        timer_cfg = _parse_instances(onoff_xml, "OBJ_WLANTIMECFG_ID")

        if not radios:
            raise FetchError("ZTE: no radio instances found in wlanbasiconoff")

        # 3. Build single POST body with indexed fields (matching browser format).
        target = "1" if enabled else "0"
        timer_enable = "0"
        if timer_cfg:
            timer_enable = timer_cfg[0].get("TimerEnable", "0")

        time_vals = {"TimeStartHour": "6", "TimeStartMin": "0",
                     "TimeEndHour": "0", "TimeEndMin": "0"}
        if timers:
            for k in time_vals:
                time_vals[k] = timers[0].get(k, time_vals[k])

        parts = ["IF_ACTION=Apply", "RadioStatus=",
                 f"TimerEnable={timer_enable}"]
        for idx, inst in enumerate(radios):
            parts.append(f"_InstID_{idx}={inst.get('_InstID', '')}")
            parts.append(f"Band_{idx}={inst.get('Band', '')}")
            parts.append(f"RadioStatus_{idx}={target}")
        parts.extend([
            "_InstID=IGD", "Band=",
            f"TimeStartHour={time_vals['TimeStartHour']}",
            f"TimeStartMin={time_vals['TimeStartMin']}",
            f"TimeEndHour={time_vals['TimeEndHour']}",
            f"TimeEndMin={time_vals['TimeEndMin']}",
            "Btn_cancel_WlanBasicAdConf=",
            "Btn_apply_WlanBasicAdConf=",
            f"_sessionTOKEN={token}",
        ])
        post_body = "&".join(parts)

        # 4. POST with Check header.
        check = _compute_check_header(post_body)
        headers = {
            **sid_hdr,
            "Check": check,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        async with session.post(
            f"https://{host}/?_type=menuData&_tag=wlan_wlanbasiconoff_lua.lua",
            data=post_body,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            result = await resp.text()

        if not _is_success(result):
            raise FetchError(f"ZTE WiFi toggle failed: {result[:200]}")
        _LOGGER.debug("ZTE WiFi set to %s — OK", "ON" if enabled else "OFF")

    # -- WiFi config ---------------------------------------------------------

    async def async_set_wifi_config(self, overrides: dict[str, Any]) -> None:
        """Apply WiFi configuration overrides to one or more SSIDs.

        *overrides* uses the internal composite keys from WIFI_FIELD_MAP:
          radio_enable          → master radio on/off (delegates to async_set_wifi_enabled)
          ap{N}_enable          → per-SSID enable (bool → "1"/"0")
          ap{N}_essid           → SSID name
          ap{N}_passphrase      → WPA passphrase (8-63 chars)
          ap{N}_ssid_broadcast  → broadcast SSID (bool; True → ESSIDHideEnable="0")
          ap{N}_encryption      → EncryptionType dropdown value
        """
        if not self._logged_in:
            raise AuthError("ZTE: must be logged in before configuring WiFi")

        overrides = dict(overrides)  # work on a copy

        # 1. Handle master radio toggle separately
        if "radio_enable" in overrides:
            await self.async_set_wifi_enabled(bool(overrides.pop("radio_enable")))
            if not overrides:
                return

        session = self._get_session()
        host = self._host
        sid_hdr = self._sid_hdr
        timeout = aiohttp.ClientTimeout(total=30)

        # 2. Enter WLAN context — menuView wlanBasic → extract _sessionTmpToken
        async with session.get(
            _zte_url(host, "menuView", "wlanBasic", Menu3Location="0"),
            headers=sid_hdr, timeout=timeout,
        ) as resp:
            wlan_html = await resp.text()

        token = _extract_session_tmp_token(wlan_html)
        if not token:
            raise FetchError("ZTE: could not extract _sessionTmpToken from wlanBasic")

        # 3. Read current SSID config
        async with session.get(
            _zte_url(host, "menuData", "wlan_wlansssidconf_lua.lua"),
            headers=sid_hdr, timeout=timeout,
        ) as resp:
            ssid_xml = await resp.text()

        ap_instances = _parse_instances(ssid_xml, "OBJ_WLANAP_ID")
        psk_instances = _parse_instances(ssid_xml, "OBJ_WLANPSK_ID")
        wep_instances = _parse_instances(ssid_xml, "OBJ_WLANWEPKEY_ID")

        if not ap_instances:
            raise FetchError("ZTE: no AP instances found in wlan_wlansssidconf_lua")

        # Build lookup: AP number → current config
        ap_by_num: dict[int, dict[str, str]] = {}
        for ap in ap_instances:
            inst_id = ap.get("_InstID", "")          # e.g. "DEV.WIFI.AP1"
            m = re.search(r"AP(\d+)$", inst_id)
            if m:
                ap_by_num[int(m.group(1))] = ap

        psk_by_ap: dict[int, dict[str, str]] = {}
        for psk in psk_instances:
            inst_id = psk.get("_InstID", "")         # e.g. "DEV.WIFI.AP1.PSK1"
            m = re.search(r"AP(\d+)\.PSK", inst_id)
            if m:
                psk_by_ap[int(m.group(1))] = psk

        wep_by_ap: dict[int, list[dict[str, str]]] = {}
        for wep in wep_instances:
            inst_id = wep.get("_InstID", "")         # e.g. "DEV.WIFI.AP1.WEP1"
            m = re.search(r"AP(\d+)\.WEP", inst_id)
            if m:
                ap_num = int(m.group(1))
                wep_by_ap.setdefault(ap_num, []).append(wep)

        # 4. Decrypt current passphrases
        dec_key = token
        dec_iv = token[::-1]
        for ap_num, psk in psk_by_ap.items():
            encrypted = psk.get("KeyPassphrase", "")
            if encrypted:
                try:
                    psk["_decrypted_passphrase"] = _aes_decrypt_value(
                        encrypted, dec_key, dec_iv
                    )
                except Exception:
                    _LOGGER.warning("ZTE: failed to decrypt passphrase for AP%d", ap_num)
                    psk["_decrypted_passphrase"] = ""

        # 5. Group overrides by AP number
        # Keys are like "ap1_enable", "ap5_essid", etc.
        changes_by_ap: dict[int, dict[str, Any]] = {}
        for key, value in overrides.items():
            m = re.match(r"ap(\d+)_(.+)", key)
            if not m:
                _LOGGER.warning("ZTE: unrecognized override key: %s", key)
                continue
            ap_num = int(m.group(1))
            field = m.group(2)
            if ap_num not in ap_by_num:
                raise FetchError(f"ZTE: AP{ap_num} not found on this router")
            changes_by_ap.setdefault(ap_num, {})[field] = value

        if not changes_by_ap:
            _LOGGER.warning("ZTE configure_wifi: no valid overrides — nothing to do")
            return

        # 6. POST each modified SSID
        for ap_num, changes in changes_by_ap.items():
            ap = ap_by_num[ap_num]
            psk = psk_by_ap.get(ap_num, {})
            weps = sorted(wep_by_ap.get(ap_num, []),
                          key=lambda w: w.get("_InstID", ""))

            # Determine current encryption type from hidden fields
            current_passphrase = psk.get("_decrypted_passphrase", "")

            # Apply overrides to working copies
            if "enable" in changes:
                ap["Enable"] = "1" if changes["enable"] else "0"
            if "essid" in changes:
                ap["ESSID"] = str(changes["essid"])
            if "passphrase" in changes:
                current_passphrase = str(changes["passphrase"])
            if "ssid_broadcast" in changes:
                # broadcast=True → ESSIDHideEnable="0" (inverted)
                ap["ESSIDHideEnable"] = "0" if changes["ssid_broadcast"] else "1"
            if "encryption" in changes:
                enc_type = str(changes["encryption"])
                if enc_type not in _ENCRYPTION_TYPE_MAP:
                    raise FetchError(
                        f"ZTE: unknown encryption type {enc_type!r}. "
                        f"Valid: {', '.join(_ENCRYPTION_TYPE_MAP)}"
                    )
                # Set hidden fields from the encryption map
                enc_fields = _ENCRYPTION_TYPE_MAP[enc_type]
                for field_name, field_val in enc_fields.items():
                    ap[field_name] = field_val

            # Determine config flags
            beacon = ap.get("BeaconType", "")
            is_psk = any(
                "PSK" in ap.get(f, "") or "SAE" in ap.get(f, "")
                for f in ("WPAAuthMode", "11iAuthMode", "WPA3AuthMode")
            )
            is_wep = beacon == "Basic"
            psk_config = "Y" if is_psk else "N"
            wep_config = "Y" if is_wep else "N"

            # Determine the EncryptionType select value from current hidden fields
            # (needed for the POST body; the browser always sends it)
            enc_type_val = self._resolve_encryption_type(ap)

            # Generate random AES key+iv for this submission
            crypto_key = "".join(random.choices(string.digits, k=16))
            crypto_iv = "".join(random.choices(string.digits, k=16))

            # Encrypt passphrase
            encrypted_passphrase = _aes_encrypt_value(
                current_passphrase, crypto_key, crypto_iv
            ) if current_passphrase else ""

            # Encrypt WEP keys (re-encrypt with new random key)
            encrypted_weps: list[str] = []
            for w in weps[:4]:
                wep_val = w.get("WEPKey", "")
                if wep_val:
                    # Decrypt the current value first, then re-encrypt
                    try:
                        plain_wep = _aes_decrypt_value(wep_val, dec_key, dec_iv)
                    except Exception:
                        plain_wep = ""
                    encrypted_weps.append(
                        _aes_encrypt_value(plain_wep, crypto_key, crypto_iv)
                        if plain_wep else ""
                    )
                else:
                    encrypted_weps.append("")
            # Pad to 4 entries
            while len(encrypted_weps) < 4:
                encrypted_weps.append("")

            # Build POST body in DOM order (matching browser's InitialPostData)
            ap_inst = ap.get("_InstID", "")
            psk_inst = psk.get("_InstID", f"{ap_inst}.PSK1")
            wep_insts = [w.get("_InstID", f"{ap_inst}.WEP{i+1}")
                         for i, w in enumerate(weps[:4])]
            while len(wep_insts) < 4:
                wep_insts.append(f"{ap_inst}.WEP{len(wep_insts)+1}")

            # Guest WiFi fields — read from XML if present, else defaults
            guest_inst = ap.get("_InstID_GUEST", "")
            guest_flag = ap.get("_GUEST", "N")
            guest_wifi = ap.get("GuestWifi", "")

            parts = [
                "IF_ACTION=Apply",
                f"_InstID={ap_inst}",
                f"_WEPCONIG={wep_config}",
                f"_PSKCONIG={psk_config}",
                f"BeaconType={ap.get('BeaconType', '')}",
                f"WEPAuthMode={ap.get('WEPAuthMode', '')}",
                f"WPAAuthMode={ap.get('WPAAuthMode', '')}",
                f"11iAuthMode={ap.get('11iAuthMode', '')}",
                f"WPAEncryptType={ap.get('WPAEncryptType', '')}",
                f"11iEncryptType={ap.get('11iEncryptType', '')}",
                f"WPA3AuthMode={ap.get('WPA3AuthMode', '')}",
                f"WPA3EncryptType={ap.get('WPA3EncryptType', '')}",
                f"_InstID_WEP0={wep_insts[0]}",
                f"_InstID_WEP1={wep_insts[1]}",
                f"_InstID_WEP2={wep_insts[2]}",
                f"_InstID_WEP3={wep_insts[3]}",
                f"_InstID_PSK={psk_inst}",
                f"MasterAuthServerIp={ap.get('MasterAuthServerIp', '')}",
                f"_InstID_GUEST={guest_inst}",
                f"_GUEST={guest_flag}",
                f"GuestWifi={guest_wifi}",
                f"Enable={ap.get('Enable', '1')}",
                f"ESSID={ap.get('ESSID', '')}",
                f"ESSIDHideEnable={ap.get('ESSIDHideEnable', '0')}",
                f"PMFEnable={ap.get('PMFEnable', '0')}",
                f"EncryptionType={enc_type_val}",
                f"KeyPassphrase={quote(encrypted_passphrase, safe='')}",
                f"WEPKeyIndex={ap.get('WEPKeyIndex', '1')}",
                "ShowWEPKey=",
                f"WEPKey00={quote(encrypted_weps[0], safe='')}",
                f"WEPKey01={quote(encrypted_weps[1], safe='')}",
                f"WEPKey02={quote(encrypted_weps[2], safe='')}",
                f"WEPKey03={quote(encrypted_weps[3], safe='')}",
                f"VapIsolationEnable={ap.get('VapIsolationEnable', '0')}",
                f"MaxUserNum={ap.get('MaxUserNum', '32')}",
                "Btn_cancel_WLANSSIDConf=",
                "Btn_apply_WLANSSIDConf=",
            ]

            # Append encode parameter (RSA-encrypted AES key+iv)
            need_encode = bool(encrypted_passphrase or any(encrypted_weps))
            if need_encode:
                encoded_key_iv = _rsa_encrypt_check(f"{crypto_key}+{crypto_iv}")
                parts.append(f"encode={quote(encoded_key_iv, safe='')}")

            # Append session token
            parts.append(f"_sessionTOKEN={token}")

            post_body = "&".join(parts)

            # POST with Check header
            check = _compute_check_header(post_body)
            headers = {
                **sid_hdr,
                "Check": check,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }

            async with session.post(
                f"https://{host}/?_type=menuData&_tag=wlan_wlansssidconf_lua.lua",
                data=post_body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                result = await resp.text()

            if not _is_success(result):
                raise FetchError(
                    f"ZTE WiFi config failed for AP{ap_num}: {result[:200]}"
                )
            _LOGGER.debug("ZTE WiFi config applied to AP%d — OK", ap_num)

    @staticmethod
    def _resolve_encryption_type(ap: dict[str, str]) -> str:
        """Determine the EncryptionType select value from the hidden field values."""
        for enc_type, fields in _ENCRYPTION_TYPE_MAP.items():
            if all(ap.get(k) == v for k, v in fields.items()):
                return enc_type
        # Fallback: if BeaconType is "None", it's "No Security"
        if ap.get("BeaconType") == "None":
            return "No Security"
        return "WPA2-PSK-AES"

    async def async_logout(self) -> None:
        """POST logout. No-op if never logged in."""
        if not self._logged_in or self._session is None:
            return
        self._logged_in = False
        try:
            async with self._session.post(
                f"https://{self._host}/?_type=loginData&_tag=logout_entry",
                data={"IF_LogOff": "1", "_sessionTOKEN": self._session_token},
                headers=self._sid_hdr,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                await resp.read()
        except Exception:
            pass  # best-effort

    async def async_close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    # -- Fetch ---------------------------------------------------------------

    async def async_fetch_data(self) -> RouterData:
        session = self._get_session()
        host = self._host
        timeout = aiohttp.ClientTimeout(total=15)
        sid_hdr = self._sid_hdr

        try:
            # Each data section requires a menuView "page open" request first.

            # 0. Firewall and WiFi state — no menuView needed; must be fetched
            #    BEFORE any menuView navigation (localNetStatus context blocks them).
            async with session.get(
                _zte_url(host, "menuData", "firewall_homepage_lua.lua"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                firewall_xml = await resp.text()

            async with session.get(
                _zte_url(host, "menuData", "wlan_homepage_lua.lua"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                wlan_xml = await resp.text()

            async with session.get(
                _zte_url(host, "menuData", "accessdev_landevs_lua.lua"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                dev_xml = await resp.text()

            # 1. WAN status
            async with session.get(
                _zte_url(host, "menuView", "ethWanStatus", Menu3Location="0"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                await resp.read()

            async with session.get(
                _zte_url(host, "menuData", "wan_internetstatus_lua.lua",
                         TypeUplink="2", pageType="1"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                wan1_xml = await resp.text()
            async with session.get(
                _zte_url(host, "menuData", "wan_internetstatus_lua.lua",
                         TypeUplink="1", pageType="1"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                wan2_xml = await resp.text()

            # 2. Device info (model, firmware, uptime)
            async with session.get(
                _zte_url(host, "menuView", "statusMgr", Menu3Location="0"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                await resp.read()

            async with session.get(
                _zte_url(host, "menuData", "devmgr_statusmgr_lua.lua"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                info_xml = await resp.text()

            # 3. LAN port status (must be last — localNetStatus context blocks accessdev)
            async with session.get(
                _zte_url(host, "menuView", "localNetStatus", Menu3Location="0"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                await resp.read()

            async with session.get(
                _zte_url(host, "menuData", "status_lan_info_lua.lua"),
                headers=sid_hdr, timeout=timeout,
            ) as resp:
                lan_xml = await resp.text()

        except Exception as exc:
            raise FetchError(f"ZTE fetch error: {exc}") from exc

        wan                                      = self._parse_wan(wan1_xml, wan2_xml)
        model, firmware, uptime, cpu_usage, mem_usage = self._parse_info(info_xml)
        lan_ports                                = self._parse_lan_ports(lan_xml)
        firewall_level, firewall_enabled         = self._parse_firewall(firewall_xml)
        wifi_enabled                             = self._parse_wlan(wlan_xml)
        devices                                  = self._parse_devices(dev_xml)

        return RouterData(
            model=model,
            firmware=firmware,
            uptime_seconds=uptime,
            connected_devices=devices,
            wan_status=wan,
            lan_ports=lan_ports,
            docsis_channels=[],
            cpu_usage=cpu_usage,
            mem_usage=mem_usage,
            firewall_level=firewall_level,
            firewall_enabled=firewall_enabled,
            wifi_enabled=wifi_enabled,
            poll_monotonic=time.monotonic(),
        )

    # -- Parsers -------------------------------------------------------------

    def _parse_devices(self, xml: str) -> list[ConnectedDevice]:
        instances = _parse_instances(xml, "OBJ_ACCESSDEV_ID")
        devices: list[ConnectedDevice] = []
        for inst in instances:
            mac = inst.get("MACAddress", "").strip().lower().replace("-", ":").replace(" ", "")
            if not mac or mac == "00:00:00:00:00:00":
                continue
            ip = inst.get("IPAddress", "").strip() or None
            hostname = inst.get("HostName", "").strip() or None
            devices.append(ConnectedDevice(
                mac=mac,
                ip=ip,
                hostname=hostname,
                is_active=True,  # devices in the list are currently connected
                network_type=None,
                port=None,
            ))
        return devices

    def _parse_wan(self, wan1_xml: str, wan2_xml: str) -> list[WanStatus]:
        """Parse WAN status from both TypeUplink responses."""
        seen: set[str] = set()
        result: list[WanStatus] = []
        for xml in (wan1_xml, wan2_xml):
            for inst in _parse_instances(xml, "ID_WAN_COMFIG"):
                if inst.get("Enable") != "1":
                    continue
                name = inst.get("WANCName", "WAN")
                if name in seen:
                    continue
                seen.add(name)
                # Prefer IPv4, fall back to IPv6 GUA
                ip = (
                    inst.get("IpAddr", "").strip()
                    or inst.get("Gua1", "").strip()
                    or None
                )
                gateway = (
                    inst.get("Gateway", "").strip()
                    or inst.get("Gateway6", "").strip()
                    or None
                )
                dns1 = inst.get("Dns1", "").strip() or inst.get("Dns1v6", "").strip() or None
                dns2 = inst.get("Dns2", "").strip() or inst.get("Dns2v6", "").strip() or None
                result.append(WanStatus(
                    name=name,
                    is_up=True,
                    ip=ip,
                    gateway=gateway,
                    dns1=dns1,
                    dns2=dns2,
                ))
        # If no WAN connection found, report as down
        if not result:
            result.append(WanStatus(name="WAN", is_up=False, ip=None, gateway=None, dns1=None, dns2=None))
        return result

    def _parse_info(self, xml: str) -> tuple[str | None, str | None, int | None, int | None, int | None]:
        """Return (model, firmware, uptime_seconds, cpu_usage, mem_usage) from devmgr_statusmgr_lua.lua."""
        model = firmware = None
        uptime: int | None = None
        cpu_usage: int | None = None
        mem_usage: int | None = None
        for inst in _parse_instances(xml, "OBJ_DEVINFO_ID"):
            model    = inst.get("ModelName") or None
            firmware = inst.get("SoftwareVer") or None
        for inst in _parse_instances(xml, "OBJ_POWERONTIME_ID"):
            try:
                uptime = int(inst.get("PowerOnTime", ""))
            except (ValueError, TypeError):
                pass
        for inst in _parse_instances(xml, "OBJ_CPUMEMUSAGE_ID"):
            try:
                cpu_usage = int(inst.get("CpuUsage1", ""))
            except (ValueError, TypeError):
                pass
            try:
                mem_usage = int(inst.get("MemUsage", ""))
            except (ValueError, TypeError):
                pass
        return model, firmware, uptime, cpu_usage, mem_usage

    @staticmethod
    def _parse_lan_ports(xml: str) -> list[LanPort]:
        """Parse LAN port status from status_lan_info_lua.lua.

        Container: OBJ_PON_PORT_BASIC_STATUS_ID
        _InstID:   DEV.ETH.IF1 … DEV.ETH.IF4  (maps to port_id 1-4)
        Status:    '0' = up/active,  '1' = no link
        Speed:     '1' = no link,  '2' = 100 Mbps,  '3' = 1000 Mbps
        InBytes/OutBytes: cumulative byte counters since boot
        """
        _SPEED_MAP = {"2": "100 Mbps", "3": "1 Gbps"}
        ports: list[LanPort] = []
        for inst in _parse_instances(xml, "OBJ_PON_PORT_BASIC_STATUS_ID"):
            inst_id = inst.get("_InstID", "")          # e.g. "DEV.ETH.IF3"
            if not inst_id.startswith("DEV.ETH.IF"):
                continue
            try:
                port_id = int(inst_id.removeprefix("DEV.ETH.IF"))
            except ValueError:
                continue
            is_active = inst.get("Status") == "0"
            speed_raw = inst.get("Speed", "")
            try:
                rx_bytes: int | None = int(inst.get("InBytes", "") or "0")
            except (ValueError, TypeError):
                rx_bytes = None
            try:
                tx_bytes: int | None = int(inst.get("OutBytes", "") or "0")
            except (ValueError, TypeError):
                tx_bytes = None
            ports.append(LanPort(
                port_id=port_id,
                is_active=is_active,
                bitrate=_SPEED_MAP.get(speed_raw) if is_active else None,
                rx_bytes=rx_bytes,
                tx_bytes=tx_bytes,
            ))
        return ports

    @staticmethod
    def _parse_firewall(xml: str) -> tuple[str | None, bool | None]:
        """Return (level, anti_attack_enabled) from firewall_homepage_lua.lua."""
        for inst in _parse_instances(xml, "OBJ_FWLEVEL_ID"):
            level = inst.get("Level") or None
            anti_attack: bool | None = inst.get("AntiAttack") == "1"
            return level, anti_attack
        return None, None

    @staticmethod
    def _parse_wlan(xml: str) -> bool | None:
        """Return WiFi radio state (True=on) from wlan_homepage_lua.lua."""
        for inst in _parse_instances(xml, "OBJ_WLANRADIO_ID"):
            return inst.get("RadioSwitch") == "1"
        return None

    async def async_get_unique_id(self) -> str:
        """Return serial number from device info endpoint."""
        session = self._get_session()
        timeout = aiohttp.ClientTimeout(total=15)
        try:
            # Open the device-info section first
            async with session.get(
                _zte_url(self._host, "menuView", "statusMgr", Menu3Location="0"),
                headers=self._sid_hdr, timeout=timeout,
            ) as resp:
                await resp.read()
            async with session.get(
                _zte_url(self._host, "menuData", "devmgr_statusmgr_lua.lua"),
                headers=self._sid_hdr, timeout=timeout,
            ) as resp:
                xml = await resp.text()
            for inst in _parse_instances(xml, "OBJ_DEVINFO_ID"):
                serial = inst.get("SerialNumber", "").strip()
                if serial:
                    return serial
        except Exception:
            pass
        return hashlib.sha256(self._host.encode()).hexdigest()[:12]


# -- Entity descriptors ------------------------------------------------------

try:
    from homeassistant.components.binary_sensor import BinarySensorEntityDescription
    from homeassistant.components.sensor import SensorEntityDescription
except ImportError:
    try:
        from ..router_registry import SensorEntityDescription, BinarySensorEntityDescription  # type: ignore[no-redef]
    except ImportError:
        from router_registry import SensorEntityDescription, BinarySensorEntityDescription  # type: ignore[no-redef]

ZTE_SENSOR_DESCS: list = [
    SensorEntityDescription(
        key="uptime",
        name="Uptime",
        device_class="duration",
        native_unit_of_measurement="s",
        state_class="total_increasing",
    ),
    # WAN
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
        name="WAN DNS 1",
        icon="mdi:dns",
    ),
    SensorEntityDescription(
        key="wan_dns2",
        name="WAN DNS 2",
        icon="mdi:dns",
    ),
    # System
    SensorEntityDescription(
        key="cpu_usage",
        name="CPU Usage",
        native_unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:cpu-64-bit",
    ),
    SensorEntityDescription(
        key="mem_usage",
        name="Memory Usage",
        native_unit_of_measurement="%",
        state_class="measurement",
        icon="mdi:memory",
    ),
    # Firewall
    SensorEntityDescription(
        key="firewall_level",
        name="Firewall Level",
        icon="mdi:shield",
    ),
    # LAN ports — link speed
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
    # LAN ports — traffic counters
    SensorEntityDescription(
        key="lan_port_1_rx_bytes",
        name="LAN Port 1 Received",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:download-network",
    ),
    SensorEntityDescription(
        key="lan_port_1_tx_bytes",
        name="LAN Port 1 Sent",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:upload-network",
    ),
    SensorEntityDescription(
        key="lan_port_2_rx_bytes",
        name="LAN Port 2 Received",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:download-network",
    ),
    SensorEntityDescription(
        key="lan_port_2_tx_bytes",
        name="LAN Port 2 Sent",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:upload-network",
    ),
    SensorEntityDescription(
        key="lan_port_3_rx_bytes",
        name="LAN Port 3 Received",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:download-network",
    ),
    SensorEntityDescription(
        key="lan_port_3_tx_bytes",
        name="LAN Port 3 Sent",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:upload-network",
    ),
    SensorEntityDescription(
        key="lan_port_4_rx_bytes",
        name="LAN Port 4 Received",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:download-network",
    ),
    SensorEntityDescription(
        key="lan_port_4_tx_bytes",
        name="LAN Port 4 Sent",
        device_class="data_size",
        native_unit_of_measurement="B",
        state_class="total_increasing",
        icon="mdi:upload-network",
    ),
]

ZTE_BINARY_SENSOR_DESCS: list = [
    BinarySensorEntityDescription(
        key="wan_connected",
        name="WAN Connected",
        device_class="connectivity",
    ),
    BinarySensorEntityDescription(
        key="firewall_enabled",
        name="Firewall Anti-Attack",
        device_class="safety",
    ),
    BinarySensorEntityDescription(
        key="wifi_enabled",
        name="WiFi",
        device_class="connectivity",
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


# -- Self-register into ROUTER_REGISTRY --------------------------------------

try:
    from ..router_registry import ROUTER_REGISTRY, RouterStrategy
except ImportError:
    from router_registry import ROUTER_REGISTRY, RouterStrategy  # noqa: E402

ROUTER_REGISTRY["zte_f660"] = RouterStrategy(
    display_name="ZTE ZXHN F660 / F6600R",
    client_class=ZteClient,
    sensor_descs=ZTE_SENSOR_DESCS,
    binary_sensor_descs=ZTE_BINARY_SENSOR_DESCS,
    channel_sensor_templates=[],
    channel_binary_sensor_templates=[],
    supports_device_tracker=True,
)
