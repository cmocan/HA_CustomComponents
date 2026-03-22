"""Constants for the TP-Link ER605 integration."""

DOMAIN = "er605"

# ── Options keys (CONF_HOST / CONF_USERNAME / CONF_PASSWORD come from homeassistant.const)
CONF_POLL_INTERVAL = "poll_interval"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL = 5    # seconds (0 = disabled / manual only)
MIN_POLL_INTERVAL     = 0
MAX_POLL_INTERVAL     = 300
DEFAULT_TIMEOUT       = 10   # seconds per HTTP request

# ── API paths (relative, after /cgi-bin/luci/;stok=<TOKEN>/) ─────────────────
API_LOCALE        = "cgi-bin/luci/;stok=/locale?form=lang"
API_LOGIN         = "cgi-bin/luci/;stok=/login?form=login"

API_SYS_STATUS    = "admin/sys_status?form=all_usage"
API_CPU_NUM       = "admin/sys_status?form=cpu_num"
API_FIRMWARE      = "admin/firmware?form=upgrade"
API_IFACE_STATUS  = "admin/interface?form=status2"
API_WAN_MODE      = "admin/interface_wan?form=wanmode"
API_WAN_STATUS    = "admin/interface_wan?form=status"   # requires wan_id param
API_ONLINE_STATE  = "admin/online?form=state"
API_SWITCH_STATE  = "admin/switch?form=state"
API_IPV6_STATUS   = "admin/ipv6?form=wanv6_status_info"
API_TIME          = "admin/time?form=settings"
API_IFSTAT        = "admin/ifstat?form=list"
API_IPSTATS       = "admin/ipstats?form=list"
API_POLICY_ROUTE  = "admin/policy_route?form=policy_route"

# ── Polling tunables ───────────────────────────────────────────────────────────
CONF_MEDIUM_POLL_INTERVAL       = "medium_poll_interval"
DEFAULT_MEDIUM_POLL_INTERVAL    = 30    # seconds (0 = disabled / manual only)
MIN_MEDIUM_POLL_INTERVAL        = 0     # seconds
MAX_MEDIUM_POLL_INTERVAL        = 300   # 5 minutes

CONF_IPSTATS_POLL_INTERVAL    = "ipstats_poll_interval"
DEFAULT_IPSTATS_POLL_INTERVAL = 150   # seconds (0 = disabled)
MIN_IPSTATS_POLL_INTERVAL     = 5     # seconds
MAX_IPSTATS_POLL_INTERVAL     = 86400 # 24 hours
IPSTATS_TOP_N                 = 20    # max IPs returned in top-N attributes on LAN client sensors

# ── Feature flags (opt-in) ────────────────────────────────────────────────────
CONF_ENABLE_IPSTATS          = "enable_ipstats"
CONF_ENABLE_DNS_RESOLVING    = "enable_dns_resolving"
DEFAULT_ENABLE_IPSTATS       = False
DEFAULT_ENABLE_DNS_RESOLVING = False

# ── Error codes returned in JSON envelope ─────────────────────────────────────
EC_OK             = "0"
EC_WRONG_CREDS    = "700"   # wrong username/password → ConfigEntryAuthFailed
EC_FORM_NOT_FOUND = "1014"  # form does not exist (not a session error)
EC_NOT_ALLOWED    = "711"   # endpoint not allowed / not accessible

# ─────────────────────────────────────────────────────────────────────────────
# SNMP protocol constants
# ─────────────────────────────────────────────────────────────────────────────

# ── Config entry keys ─────────────────────────────────────────────────────────
CONF_PROTOCOL  = "protocol"        # "http" | "snmp"
CONF_COMMUNITY = "community"       # SNMPv2c community string
CONF_SNMP_PORT = "snmp_port"       # default 161

PROTOCOL_HTTP = "http"
PROTOCOL_SNMP = "snmp"

# ── SNMP poll interval defaults (different from HTTP) ─────────────────────────
DEFAULT_SNMP_POLL_INTERVAL        = 30    # seconds — Tier 1 (counter rates)
DEFAULT_SNMP_MEDIUM_POLL_INTERVAL = 300   # seconds — Tier 2 (WAN IPs, uptime)
DEFAULT_SNMP_STATIC_POLL_INTERVAL = 3600  # seconds — Tier 3 (sysDescr, sysName)
MIN_SNMP_STATIC_POLL_INTERVAL     = 60
MAX_SNMP_STATIC_POLL_INTERVAL     = 86400

# ── OIDs ─────────────────────────────────────────────────────────────────────
# System group
OID_SYS_DESCR    = "1.3.6.1.2.1.1.1.0"
OID_SYS_UPTIME   = "1.3.6.1.2.1.1.3.0"
OID_SYS_CONTACT  = "1.3.6.1.2.1.1.4.0"
OID_SYS_NAME     = "1.3.6.1.2.1.1.5.0"
OID_SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

# ifTable (MIB-2)
OID_IF_DESCR_BASE    = "1.3.6.1.2.1.2.2.1.2"   # ifDescr walk base
OID_IF_TYPE_BASE     = "1.3.6.1.2.1.2.2.1.3"   # ifType walk base
OID_IF_PHYS_ADDR     = "1.3.6.1.2.1.2.2.1.6"   # ifPhysAddress base (append .ifIndex)
OID_IF_ADMIN_STATUS  = "1.3.6.1.2.1.2.2.1.7"   # ifAdminStatus base
OID_IF_OPER_STATUS   = "1.3.6.1.2.1.2.2.1.8"   # ifOperStatus base

# ifXTable (IF-MIB, 64-bit)
OID_IF_HC_IN_BASE    = "1.3.6.1.2.1.31.1.1.1.6"  # ifHCInOctets base
OID_IF_HC_OUT_BASE   = "1.3.6.1.2.1.31.1.1.1.10" # ifHCOutOctets base
OID_IF_HIGH_SPEED    = "1.3.6.1.2.1.31.1.1.1.15" # ifHighSpeed base

# ipAddrTable
OID_IP_ADDR_TABLE    = "1.3.6.1.2.1.4.20"
OID_IP_ADDR_IFINDEX  = "1.3.6.1.2.1.4.20.1.2"   # IP → ifIndex
OID_IP_ADDR_NETMASK  = "1.3.6.1.2.1.4.20.1.3"

# HOST-RESOURCES-MIB (hrStorage)
OID_HR_STORAGE_TYPE  = "1.3.6.1.2.1.25.2.3.1.2"  # hrStorageType walk base
OID_HR_STORAGE_SIZE  = "1.3.6.1.2.1.25.2.3.1.5"  # hrStorageSize base
OID_HR_STORAGE_USED  = "1.3.6.1.2.1.25.2.3.1.6"  # hrStorageUsed base
OID_HR_STORAGE_RAM   = "1.3.6.1.2.1.25.2.1.2"    # hrStorageRam type OID

# ifType value for physical ethernet
IF_TYPE_ETHERNET = 6   # ethernetCsmacd
