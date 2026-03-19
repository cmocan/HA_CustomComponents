"""Constants for the TP-Link ER605 integration."""

DOMAIN = "er605"

# ── Options keys (CONF_HOST / CONF_USERNAME / CONF_PASSWORD come from homeassistant.const)
CONF_POLL_INTERVAL = "poll_interval"

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_POLL_INTERVAL = 5    # seconds
MIN_POLL_INTERVAL     = 1
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

# ── Polling tunables ───────────────────────────────────────────────────────────
CONF_MEDIUM_POLL_INTERVAL       = "medium_poll_interval"
DEFAULT_MEDIUM_POLL_INTERVAL    = 30    # seconds
MIN_MEDIUM_POLL_INTERVAL        = 5     # seconds
MAX_MEDIUM_POLL_INTERVAL        = 300   # 5 minutes

CONF_IPSTATS_POLL_INTERVAL    = "ipstats_poll_interval"
DEFAULT_IPSTATS_POLL_INTERVAL = 150   # seconds (0 = disabled)
MIN_IPSTATS_POLL_INTERVAL     = 5     # seconds
MAX_IPSTATS_POLL_INTERVAL     = 86400 # 24 hours
IPSTATS_TOP_N                 = 20    # max IPs returned in top-N attributes on LAN client sensors

# ── Error codes returned in JSON envelope ─────────────────────────────────────
EC_OK             = "0"
EC_WRONG_CREDS    = "700"   # wrong username/password → ConfigEntryAuthFailed
EC_FORM_NOT_FOUND = "1014"  # form does not exist (not a session error)
