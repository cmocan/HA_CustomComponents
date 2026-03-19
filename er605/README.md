# TP-Link ER605 — Home Assistant Integration

A local-polling custom integration for the **TP-Link ER605 wired VPN router** (v1/v2).  
All data is fetched directly from the router's web API over your LAN — no cloud, no TP-Link account required.

---

## Features

- **WAN connectivity** — per-WAN up/down binary sensors with IP address
- **Real-time traffic rates** — per-zone download/upload in KB/s (WAN1, WAN2, LAN)
- **Cumulative traffic totals** — per-zone downloaded/uploaded bytes since last boot
- **LAN client tracking** — total and active client count, with a top-20 table sorted by current traffic rate as sensor attributes
- **System health** — CPU usage (averaged across cores), memory usage, uptime
- **Physical switch ports** — connected/disconnected + link speed per port
- **IPv6 WAN status** — per-WAN enabled/connected binary sensors + IPv6 address sensors
- **Configurable poll interval** — 10–300 s (default 30 s); LAN client stats slow-polled every 5 cycles (~2.5 min)
- **Diagnostics** — built-in HA diagnostics support (password redacted)
- **Re-authentication flow** — HA prompts for new credentials if the router rejects them

---

## Supported Hardware

| Model | Tested firmware |
|---|---|
| TP-Link ER605 v2 | 2.3.x |

Other ER605 hardware revisions likely work. Other ER-series models (ER7206, ER8411, etc.) may work if they share the same web API — not tested.

---

## Requirements

- Home Assistant 2024.1 or newer
- ER605 accessible from HA over HTTPS on its LAN IP (default port 443)
- Router web UI credentials (username + password)
- No external Python packages required — uses only `aiohttp` (bundled with HA)

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → **⋮** menu → **Custom repositories**
2. Add this repository URL, category **Integration**
3. Search for **TP-Link ER605** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/er605/` folder into your HA `custom_components/` directory
2. Restart Home Assistant

---

## Configuration

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **TP-Link ER605**
3. Enter:
   - **Router IP Address** — local IP of the router (e.g. `192.168.0.1`)
   - **Username** — web UI username (default: `admin`)
   - **Password** — web UI password
4. Submit — HA will verify connectivity and create the device

### Options

After setup, click **Configure** on the integration to change the **poll interval** (10–300 seconds, default 30).

---

## Authentication

The ER605 uses a custom 3-step RSA login flow:

1. Fetch router uptime (used as a password salt)
2. Fetch RSA public key from the router
3. Encrypt `PASSWORD_<uptime>` with no-padding RSA and submit — receive a `stok` session token + `sysauth` cookie

All of this is handled automatically using only Python's built-in `pow()` for modular exponentiation — no `cryptography` or `pyOpenSSL` dependency.

---

## Entities

### Binary Sensors

| Entity | Description | Default |
|---|---|---|
| `binary_sensor.er605_wan1_connected` | WAN1 link up/down | ✅ enabled |
| `binary_sensor.er605_wan2_connected` | WAN2 link up/down | ✅ enabled |
| `binary_sensor.er605_wan1_ipv6_enabled` | WAN1 IPv6 enabled | ☑ disabled |
| `binary_sensor.er605_wan2_ipv6_enabled` | WAN2 IPv6 enabled | ☑ disabled |
| `binary_sensor.er605_port_1_connected` | Switch port 1 connected | ☑ disabled |
| `binary_sensor.er605_port_2_connected` | Switch port 2 connected | ☑ disabled |
| `binary_sensor.er605_port_3_connected` | Switch port 3 connected | ☑ disabled |
| `binary_sensor.er605_port_4_connected` | Switch port 4 connected | ☑ disabled |
| `binary_sensor.er605_port_5_connected` | Switch port 5 connected | ☑ disabled |

### Sensors — System

| Entity | Unit | State class | Description |
|---|---|---|---|
| `sensor.er605_uptime` | s | total_increasing | Router uptime |
| `sensor.er605_cpu_usage` | % | measurement | CPU usage (avg across cores) |
| `sensor.er605_memory_usage` | % | measurement | Memory usage |
| `sensor.er605_active_wan_count` | — | measurement | Number of WAN links currently up |
| `sensor.er605_lan_clients_total` | — | measurement | All LAN IPs with recorded traffic |
| `sensor.er605_lan_clients_active` | — | measurement | LAN IPs with non-zero current rate |

`lan_clients_total` and `lan_clients_active` both expose a `clients` attribute containing a top-20 list (sorted by current combined rate, descending):

```yaml
clients:
  - addr: 192.168.0.10
    rx_bps: 8240    # KB/s
    tx_bps: 1200    # KB/s
    rx_bytes: 19823756
    tx_bytes: 4432100
  - ...
total_clients: 18
top_n: 20
```

### Sensors — Per WAN

Entities are created dynamically for each detected WAN interface. Example IDs for a 2-WAN setup:

| Entity | Unit | Description | Default |
|---|---|---|---|
| `sensor.er605_wan1_ip_address` | — | WAN1 current IP | ✅ enabled |
| `sensor.er605_wan1_gateway` | — | WAN1 gateway IP | ☑ disabled |
| `sensor.er605_wan1_dns` | — | WAN1 primary DNS | ☑ disabled |
| `sensor.er605_wan1_ipv6_address` | — | WAN1 IPv6 address | ☑ disabled |
| `sensor.er605_wan2_ip_address` | — | WAN2 current IP | ✅ enabled |
| `sensor.er605_wan2_gateway` | — | WAN2 gateway IP | ☑ disabled |
| `sensor.er605_wan2_dns` | — | WAN2 primary DNS | ☑ disabled |
| `sensor.er605_wan2_ipv6_address` | — | WAN2 IPv6 address | ☑ disabled |

### Sensors — Traffic (per zone)

Zones: `WAN1`, `WAN2`, and all LAN zones reported by the router.

| Entity pattern | Unit | State class | Default |
|---|---|---|---|
| `sensor.er605_ifstat_<zone>_rx_bps` | KB/s | measurement | ✅ enabled |
| `sensor.er605_ifstat_<zone>_tx_bps` | KB/s | measurement | ✅ enabled |
| `sensor.er605_ifstat_<zone>_rx_bytes` | B | total_increasing | ✅ WAN / ☑ LAN |
| `sensor.er605_ifstat_<zone>_tx_bytes` | B | total_increasing | ✅ WAN / ☑ LAN |

### Sensors — Physical Port Speed

| Entity | Unit | Default |
|---|---|---|
| `sensor.er605_port_1_speed` | — (`"1000M"`, `"100M"`, …) | ☑ disabled |
| `sensor.er605_port_2_speed` | — | ☑ disabled |
| … | — | ☑ disabled |

---

## Polling

| Data | Interval |
|---|---|
| System, interfaces, traffic rates, ports | Every poll cycle (default 30 s) |
| LAN IP traffic stats (`ipstats`) | Every 5 cycles (~2.5 min at 30 s) |

The slow poll for ipstats avoids hammering the router — it caches the last result between cycles.  
Both intervals can be tuned via the options flow: the main poll interval and the IP stats poll interval (0 = disabled).

---

## Dashboard Card

Requires **Mushroom Cards** installed via HACS → Frontend (https://github.com/piitaya/lovelace-mushroom).

Paste the YAML below into **Dashboard → Add card → Manual card**.

> Some entities are disabled by default — enable them first:\
> **Settings → Devices & Services → ER605 → (entity) → Enable**\
> Required for totals: `sensor.er605_ifstat_wan1_rx_bytes`, `tx_bytes`, `wan2_rx_bytes`, `tx_bytes`

<details>
<summary>Click to expand dashboard YAML</summary>

```yaml
type: vertical-stack
cards:

  # ── Title ───────────────────────────────────────────────────────────────────
  - type: custom:mushroom-title-card
    title: TP-Link ER605
    subtitle: >
      {% set u = states('sensor.er605_uptime') | int(-1) %}
      {% if u < 0 %}
        Uptime: N/A
      {% else %}
        {% set d = u // 86400 %}
        {% set h = (u % 86400) // 3600 %}
        {% set m = (u % 3600) // 60 %}
        Uptime: {{ [d ~ 'd' if d > 0, h ~ 'h' if h > 0, m ~ 'm'] | select | join(' ') or '0m' }}
      {% endif %}

  # ── Quick chips ─────────────────────────────────────────────────────────────
  - type: custom:mushroom-chips-card
    chips:
      - type: template
        entity: sensor.er605_active_wan_count
        icon: mdi:wan
        content: "{{ states('sensor.er605_active_wan_count') | default('?', true) }} WAN"
        icon_color: >
          {{ 'green' if states('sensor.er605_active_wan_count') | int(0) > 0 else 'red' }}

      - type: template
        entity: sensor.er605_cpu_usage
        icon: mdi:cpu-64-bit
        content: "CPU {{ states('sensor.er605_cpu_usage') | int(0) }}%"
        icon_color: >
          {% set v = states('sensor.er605_cpu_usage') | int(0) %}
          {{ 'red' if v >= 90 else 'orange' if v >= 70 else 'green' }}

      - type: template
        entity: sensor.er605_memory_usage
        icon: mdi:memory
        content: "RAM {{ states('sensor.er605_memory_usage') | int(0) }}%"
        icon_color: >
          {% set v = states('sensor.er605_memory_usage') | int(0) %}
          {{ 'red' if v >= 90 else 'orange' if v >= 70 else 'green' }}

      - type: template
        entity: sensor.er605_lan_clients_active
        icon: mdi:lan-connect
        content: >
          {{ states('sensor.er605_lan_clients_active') | default('?', true) }}
          / {{ states('sensor.er605_lan_clients_total') | default('?', true) }} clients
        icon_color: >
          {{ 'blue' if states('sensor.er605_lan_clients_active') | int(0) > 0 else 'grey' }}

  # ── WAN Status ──────────────────────────────────────────────────────────────
  - type: custom:mushroom-title-card
    title: WAN

  - type: grid
    columns: 2
    square: false
    cards:
      - type: custom:mushroom-template-card
        entity: binary_sensor.er605_wan1_connected
        primary: WAN 1
        secondary: >
          {% set s = states('binary_sensor.er605_wan1_connected') %}
          {% if s == 'on' %}
            {{ states('sensor.er605_wan1_ip_address') }}
          {% elif s == 'off' %}
            Disconnected
          {% else %}
            N/A
          {% endif %}
        icon: >
          {{ 'mdi:check-network' if is_state('binary_sensor.er605_wan1_connected', 'on')
             else 'mdi:close-network-outline' }}
        icon_color: >
          {% set s = states('binary_sensor.er605_wan1_connected') %}
          {{ 'green' if s == 'on' else 'red' if s == 'off' else 'disabled' }}
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: binary_sensor.er605_wan2_connected
        primary: WAN 2
        secondary: >
          {% set s = states('binary_sensor.er605_wan2_connected') %}
          {% if s == 'on' %}
            {{ states('sensor.er605_wan2_ip_address') }}
          {% elif s == 'off' %}
            Disconnected
          {% else %}
            N/A
          {% endif %}
        icon: >
          {{ 'mdi:check-network' if is_state('binary_sensor.er605_wan2_connected', 'on')
             else 'mdi:close-network-outline' }}
        icon_color: >
          {% set s = states('binary_sensor.er605_wan2_connected') %}
          {{ 'green' if s == 'on' else 'red' if s == 'off' else 'disabled' }}
        tap_action:
          action: more-info

  # ── WAN Traffic Rates ───────────────────────────────────────────────────────
  - type: custom:mushroom-title-card
    title: WAN Traffic

  - type: grid
    columns: 2
    square: false
    cards:
      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan1_rx_bps
        primary: WAN1 Download
        secondary: >
          {% set v = states('sensor.er605_ifstat_wan1_rx_bps') | float(0) %}
          {{ (v / 1024) | round(2) ~ ' MB/s' if v >= 1024 else v | round(1) ~ ' KB/s' }}
        icon: mdi:download-network
        icon_color: >
          {{ 'blue' if states('sensor.er605_ifstat_wan1_rx_bps') | float(0) > 0 else 'grey' }}
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan1_tx_bps
        primary: WAN1 Upload
        secondary: >
          {% set v = states('sensor.er605_ifstat_wan1_tx_bps') | float(0) %}
          {{ (v / 1024) | round(2) ~ ' MB/s' if v >= 1024 else v | round(1) ~ ' KB/s' }}
        icon: mdi:upload-network
        icon_color: >
          {{ 'green' if states('sensor.er605_ifstat_wan1_tx_bps') | float(0) > 0 else 'grey' }}
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan2_rx_bps
        primary: WAN2 Download
        secondary: >
          {% set v = states('sensor.er605_ifstat_wan2_rx_bps') | float(0) %}
          {{ (v / 1024) | round(2) ~ ' MB/s' if v >= 1024 else v | round(1) ~ ' KB/s' }}
        icon: mdi:download-network
        icon_color: >
          {{ 'blue' if states('sensor.er605_ifstat_wan2_rx_bps') | float(0) > 0 else 'grey' }}
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan2_tx_bps
        primary: WAN2 Upload
        secondary: >
          {% set v = states('sensor.er605_ifstat_wan2_tx_bps') | float(0) %}
          {{ (v / 1024) | round(2) ~ ' MB/s' if v >= 1024 else v | round(1) ~ ' KB/s' }}
        icon: mdi:upload-network
        icon_color: >
          {{ 'green' if states('sensor.er605_ifstat_wan2_tx_bps') | float(0) > 0 else 'grey' }}
        tap_action:
          action: more-info

  # ── WAN Data Totals (enable rx_bytes / tx_bytes entities first) ─────────────
  - type: grid
    columns: 2
    square: false
    cards:
      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan1_rx_bytes
        primary: WAN1 Total ↓
        secondary: >
          {% set b = states('sensor.er605_ifstat_wan1_rx_bytes') | float(0) %}
          {{ (b / 1073741824) | round(2) ~ ' GB' if b >= 1073741824
             else (b / 1048576) | round(1) ~ ' MB' if b >= 1048576
             else (b / 1024) | round(1) ~ ' KB' }}
        icon: mdi:download
        icon_color: blue
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan1_tx_bytes
        primary: WAN1 Total ↑
        secondary: >
          {% set b = states('sensor.er605_ifstat_wan1_tx_bytes') | float(0) %}
          {{ (b / 1073741824) | round(2) ~ ' GB' if b >= 1073741824
             else (b / 1048576) | round(1) ~ ' MB' if b >= 1048576
             else (b / 1024) | round(1) ~ ' KB' }}
        icon: mdi:upload
        icon_color: green
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan2_rx_bytes
        primary: WAN2 Total ↓
        secondary: >
          {% set b = states('sensor.er605_ifstat_wan2_rx_bytes') | float(0) %}
          {{ (b / 1073741824) | round(2) ~ ' GB' if b >= 1073741824
             else (b / 1048576) | round(1) ~ ' MB' if b >= 1048576
             else (b / 1024) | round(1) ~ ' KB' }}
        icon: mdi:download
        icon_color: blue
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_ifstat_wan2_tx_bytes
        primary: WAN2 Total ↑
        secondary: >
          {% set b = states('sensor.er605_ifstat_wan2_tx_bytes') | float(0) %}
          {{ (b / 1073741824) | round(2) ~ ' GB' if b >= 1073741824
             else (b / 1048576) | round(1) ~ ' MB' if b >= 1048576
             else (b / 1024) | round(1) ~ ' KB' }}
        icon: mdi:upload
        icon_color: green
        tap_action:
          action: more-info

  # ── System ──────────────────────────────────────────────────────────────────
  - type: custom:mushroom-title-card
    title: System

  - type: grid
    columns: 2
    square: false
    cards:
      - type: custom:mushroom-template-card
        entity: sensor.er605_cpu_usage
        primary: CPU Usage
        secondary: >
          {% set v = states('sensor.er605_cpu_usage') %}
          {{ v ~ ' %' if v not in ['unavailable','unknown','none'] else 'N/A' }}
        icon: mdi:cpu-64-bit
        icon_color: >
          {% set v = states('sensor.er605_cpu_usage') | int(0) %}
          {{ 'red' if v >= 90 else 'orange' if v >= 70 else 'green' }}
        tap_action:
          action: more-info

      - type: custom:mushroom-template-card
        entity: sensor.er605_memory_usage
        primary: Memory
        secondary: >
          {% set v = states('sensor.er605_memory_usage') %}
          {{ v ~ ' %' if v not in ['unavailable','unknown','none'] else 'N/A' }}
        icon: mdi:memory
        icon_color: >
          {% set v = states('sensor.er605_memory_usage') | int(0) %}
          {{ 'red' if v >= 90 else 'orange' if v >= 70 else 'green' }}
        tap_action:
          action: more-info

  # ── LAN Clients ─────────────────────────────────────────────────────────────
  - type: custom:mushroom-title-card
    title: LAN Clients
    subtitle: >
      {{ states('sensor.er605_lan_clients_active') }} active
      / {{ states('sensor.er605_lan_clients_total') }} total
      · top 20 shown · updated every ~2 min

  - type: markdown
    title: Active Clients
    content: |
      {% set clients = state_attr('sensor.er605_lan_clients_active', 'clients') -%}
      {% if clients and clients | length > 0 -%}
      | # | IP Address | ↓ KB/s | ↑ KB/s | ↓ MB total | ↑ MB total |
      |:--|:--|--:|--:|--:|--:|
      {% for c in clients -%}
      | {{ loop.index }} | {{ c.addr }} | {{ c.rx_bps }} | {{ c.tx_bps }} | {{ (c.rx_bytes / 1048576) | round(1) }} | {{ (c.tx_bytes / 1048576) | round(1) }} |
      {% endfor -%}
      {% else -%}
      *No clients with active traffic right now.*
      {%- endif %}

  - type: markdown
    title: All LAN Clients (top 20)
    content: |
      {% set clients = state_attr('sensor.er605_lan_clients_total', 'clients') -%}
      {% if clients and clients | length > 0 -%}
      | # | IP Address | ↓ KB/s | ↑ KB/s | ↓ MB total | ↑ MB total |
      |:--|:--|--:|--:|--:|--:|
      {% for c in clients -%}
      | {{ loop.index }} | {{ c.addr }} | {{ c.rx_bps }} | {{ c.tx_bps }} | {{ (c.rx_bytes / 1048576) | round(1) }} | {{ (c.tx_bytes / 1048576) | round(1) }} |
      {% endfor -%}
      {% else -%}
      *No LAN clients recorded yet — waiting for first ipstats poll (~2 min).*
      {%- endif %}

  # ── Switch Ports ─────────────────────────────────────────────────────────────
  # Enable port entities first: Settings → Devices → ER605 → each port entity
  - type: custom:mushroom-title-card
    title: Switch Ports

  - type: grid
    columns: 5
    square: true
    cards:
      - type: custom:mushroom-template-card
        primary: P1
        entity: binary_sensor.er605_port_1_connected
        icon: mdi:ethernet
        icon_color: >
          {% set s = states('binary_sensor.er605_port_1_connected') %}
          {{ 'green' if s == 'on' else 'grey' if s == 'off' else 'disabled' }}
        tap_action:
          action: none

      - type: custom:mushroom-template-card
        primary: P2
        entity: binary_sensor.er605_port_2_connected
        icon: mdi:ethernet
        icon_color: >
          {% set s = states('binary_sensor.er605_port_2_connected') %}
          {{ 'green' if s == 'on' else 'grey' if s == 'off' else 'disabled' }}
        tap_action:
          action: none

      - type: custom:mushroom-template-card
        primary: P3
        entity: binary_sensor.er605_port_3_connected
        icon: mdi:ethernet
        icon_color: >
          {% set s = states('binary_sensor.er605_port_3_connected') %}
          {{ 'green' if s == 'on' else 'grey' if s == 'off' else 'disabled' }}
        tap_action:
          action: none

      - type: custom:mushroom-template-card
        primary: P4
        entity: binary_sensor.er605_port_4_connected
        icon: mdi:ethernet
        icon_color: >
          {% set s = states('binary_sensor.er605_port_4_connected') %}
          {{ 'green' if s == 'on' else 'grey' if s == 'off' else 'disabled' }}
        tap_action:
          action: none

      - type: custom:mushroom-template-card
        primary: P5
        entity: binary_sensor.er605_port_5_connected
        icon: mdi:ethernet
        icon_color: >
          {% set s = states('binary_sensor.er605_port_5_connected') %}
          {{ 'green' if s == 'on' else 'grey' if s == 'off' else 'disabled' }}
        tap_action:
          action: none
```

</details>

---

## Diagnostics

The integration supports HA's built-in diagnostics.  
Go to **Settings → Devices & Services → ER605 → Download diagnostics** to get a JSON snapshot of the last poll (password is automatically redacted).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "Cannot connect" during setup | Wrong IP or router unreachable | Confirm you can open `https://<router-ip>` in a browser from the HA host |
| "Invalid auth" during setup | Wrong username or password | Use the same credentials as the ER605 web UI |
| Entities stuck at `unavailable` | Session expired and re-login failed | Check HA logs; try reloading the integration |
| LAN client sensors always `0` / attributes empty | ipstats not yet fetched | Wait ~2.5 min; check HA logs for ipstats errors |
| WAN total byte sensors missing | Entities disabled by default | Enable in Settings → Devices & Services → ER605 |
| `sysauth` cookie errors in logs | HA's shared session rejecting IP cookies | Should not happen — the integration uses its own `CookieJar(unsafe=True)` |

---

## License

MIT
