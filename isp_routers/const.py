"""Constants for the ISP Routers integration."""

DOMAIN = "isp_routers"

# Config entry keys (host/username/password come from homeassistant.const)
CONF_ROUTER_TYPE   = "router_type"
CONF_POLL_INTERVAL = "poll_interval"
CONF_ZTE_MODEL     = "zte_model"

# Poll interval bounds
DEFAULT_POLL_INTERVAL = 30    # seconds
MIN_POLL_INTERVAL     = 5
MAX_POLL_INTERVAL     = 300

DEFAULT_TIMEOUT = 15          # seconds per HTTP request
