# Home Assistant — Router Integrations

Two custom components for monitoring routers from Home Assistant via local polling (no cloud).

---

## Components

| Component | Router | Protocol |
|---|---|---|
| `er605` | TP-Link ER605 | HTTP + SNMP |
| `isp_routers` | Arris TG3442DE (Vodafone Station) | HTTPS |
| `isp_routers` | ZTE ZXHN F6600R (Orange) | HTTPS |

---

## `isp_routers` — Arris TG3442DE & ZTE ZXHN F6600R

### Entities

#### Arris TG3442DE

| Entity | Type | Notes |
|---|---|---|
| Uptime | Sensor | seconds, `total_increasing` |
| WAN IP | Sensor | external IP address |
| WAN Gateway | Sensor | |
| WAN DNS | Sensor | primary DNS |
| LAN Network | Sensor | e.g. `192.168.10.0/24` |
| LAN Port 1–4 Speed | Sensor | e.g. `1 Gbps`; `None` when inactive |
| Connected Devices | Sensor | total DHCP/ARP entries |
| Active Devices | Sensor | currently active |
| VoIP Lines | Sensor | registered SIP lines |
| WAN Connected | Binary Sensor | `connectivity` device class |
| Firewall | Binary Sensor | on/off |
| LAN Port 1–4 | Binary Sensor | `plug` device class |

#### ZTE ZXHN F6600R

| Entity | Type | Notes |
|---|---|---|
| Uptime | Sensor | seconds, `total_increasing` |
| WAN IP | Sensor | IPv4 or IPv6 GUA |
| WAN Gateway | Sensor | |
| WAN DNS | Sensor | primary DNS |
| LAN Port 1–4 Speed | Sensor | e.g. `1 Gbps`; `None` when inactive |
| WAN Connected | Binary Sensor | `connectivity` device class |
| LAN Port 1–4 | Binary Sensor | `plug` device class |

### Requirements

- `pycryptodome >= 3.20.0` (Arris AES-CCM encryption — installed automatically by HA)

### Installation

1. Copy `custom_components/isp_routers/` into your HA config directory:
   ```
   /config/custom_components/isp_routers/
   ```
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **ISP Routers**.
4. Select your router model, enter host/username/password and click Submit.

> Default credentials: `admin` / (your Wi-Fi password printed on the device label)

### Configuration

| Field | Default | Range |
|---|---|---|
| Poll interval | 30 s | 5 – 300 s |

The poll interval can be changed after setup via **Configure** on the integration card.

### How it works

Each poll does a full **login → fetch → logout** cycle over the router's local HTTPS/HTTP interface. No data leaves your network.

**Arris login** replicates the browser's AES-CCM challenge-response flow (PBKDF2-SHA256 key derivation, SJCL-compatible encryption) discovered by HAR capture of the router's web UI.

**ZTE login** replicates the five-step browser flow: `login_entry` → `login_token` → POST credentials (SHA-256 hashed) → cancel password-change prompt → `GET /` to activate session.

---

## `er605` — TP-Link ER605

Monitors the TP-Link ER605 business router via its HTTP management API and SNMP.

### Requirements

- `pysnmp >= 7.1, < 8.0` (installed automatically by HA)
- SNMP must be enabled on the ER605 (Management → SNMP)

### Installation

1. Copy `custom_components/er605/` into your HA config directory:
   ```
   /config/custom_components/er605/
   ```
2. Restart Home Assistant.
3. Go to **Settings → Devices & Services → Add Integration** and search for **TP-Link ER605**.

---

## Development

### Repository structure

```
custom_components/
  er605/           TP-Link ER605 integration
  isp_routers/     Multi-router integration
    routers/
      arris_tg3442de.py   Arris client + entity descriptors
      zte_f660.py         ZTE client + entity descriptors
dev_tools/
  http_probe/      HTTP probes for TP-Link ER605 API reverse-engineering
  http_probe_for_ha/  Final validation probes for both routers
  snmp_probe/      SNMP probes for ER605
har/               Browser HAR captures used during API analysis
docs/              Architecture notes, capability maps, design specs
tests/             Pytest suite for the er605 integration
```

### Running tests

```bash
pytest tests/
```

No Home Assistant install required — the ER605 integration tests run standalone.

### Adding a new router

1. Create `custom_components/isp_routers/routers/your_router.py`
2. Implement `RouterClient` (subclass from `router_registry.py`)
3. Define `SensorEntityDescription` / `BinarySensorEntityDescription` lists
4. Call `ROUTER_REGISTRY["your_key"] = RouterStrategy(...)` at module level
5. Import the module in `config_flow.py`

The platform files (`sensor.py`, `binary_sensor.py`, `device_tracker.py`) require no changes — they drive off the registry automatically.
