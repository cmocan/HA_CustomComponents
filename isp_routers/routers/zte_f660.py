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
import hashlib
import logging
import time
import uuid
import xml.etree.ElementTree as ET

import aiohttp

_LOGGER = logging.getLogger(__name__)

# Hardcoded _sessionTOKEN used by the router's doChgpwd JS function.
# This is baked into the firmware HTML and never changes between sessions.
_CHANGEPWD_SESSION_TOKEN = "iPFA8znE0U9PAGPqQ1qDTlud"

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


def _extract_sid(set_cookie_headers: list[str]) -> str:
    """Return the last SID_HTTPS_ value from a list of Set-Cookie header strings."""
    sid = ""
    for sc in set_cookie_headers:
        if "SID_HTTPS_" in sc:
            sid = sc.split(";")[0].split("=", 1)[1]
    return sid


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

        wan                  = self._parse_wan(wan1_xml, wan2_xml)
        model, firmware, uptime = self._parse_info(info_xml)
        lan_ports            = self._parse_lan_ports(lan_xml)

        return RouterData(
            model=model,
            firmware=firmware,
            uptime_seconds=uptime,
            connected_devices=[],
            wan_status=wan,
            lan_ports=lan_ports,
            docsis_channels=[],
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

    def _parse_info(self, xml: str) -> tuple[str | None, str | None, int | None]:
        """Return (model, firmware, uptime_seconds) from devmgr_statusmgr_lua.lua."""
        model = firmware = None
        uptime: int | None = None
        for inst in _parse_instances(xml, "OBJ_DEVINFO_ID"):
            model    = inst.get("ModelName") or None
            firmware = inst.get("SoftwareVer") or None
        for inst in _parse_instances(xml, "OBJ_POWERONTIME_ID"):
            try:
                uptime = int(inst.get("PowerOnTime", ""))
            except (ValueError, TypeError):
                pass
        return model, firmware, uptime

    @staticmethod
    def _parse_lan_ports(xml: str) -> list[LanPort]:
        """Parse LAN port status from status_lan_info_lua.lua.

        Container: OBJ_PON_PORT_BASIC_STATUS_ID
        _InstID:   DEV.ETH.IF1 … DEV.ETH.IF4  (maps to port_id 1-4)
        Status:    '0' = up/active,  '1' = no link
        Speed:     '1' = no link,  '2' = 100 Mbps,  '3' = 1000 Mbps
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
            ports.append(LanPort(
                port_id=port_id,
                is_active=is_active,
                bitrate=_SPEED_MAP.get(speed_raw) if is_active else None,
            ))
        return ports

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
        name="WAN DNS",
        icon="mdi:dns",
    ),
    # LAN ports
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
]

ZTE_BINARY_SENSOR_DESCS: list = [
    BinarySensorEntityDescription(
        key="wan_connected",
        name="WAN Connected",
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
    supports_device_tracker=False,
)
