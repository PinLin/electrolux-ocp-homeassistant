# Electrolux Home Assistant Integration

[![GitHub Release](https://img.shields.io/github/release/PinLin/electrolux-homeassistant.svg?style=flat-square)](https://github.com/PinLin/electrolux-homeassistant/releases)
[![HACS Badge](https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat-square)](https://github.com/hacs/integration)
[![MIT License](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](LICENSE)

Home Assistant integration for Electrolux smart appliances. Connects with your Electrolux account to expose real-time status and remote control as native Home Assistant entities.

> **Consider [homeassistant-wellbeing](https://github.com/JohNan/homeassistant-wellbeing) first.**
> That project integrates against the official Electrolux Developer API — it's more stable and on clearer legal ground, so for most users it's the better default. This project takes a different path so you can sign in with a regular Electrolux email and password, but doesn't offer the same long-term guarantees.
>
> **Tested only on the Electrolux Pure A9 (PUREA9) air purifier so far.** Other appliance types may load without errors but will likely expose only partial sensors and no controls.

## Features

- **Account login** — sign in with your Electrolux email and password; no manual token extraction.
- **Real-time updates** — pushes state changes over WebSocket; no need to wait for the polling interval.
- **Dynamic sensors** — automatically maps reported properties (PM1/PM2.5/PM10, TVOC, eCO2, temperature, humidity, signal strength, …) to typed sensors with the right device class and units.
- **Problem detection** — error flags reported by the appliance are surfaced as `binary_sensor` entities with the `PROBLEM` device class (filter cover, fan motor, sensor faults, …).
- **Controls** — switches for UI light, child lock, and ionizer; a fan entity with preset modes pulled live from the appliance's capability descriptor (Auto / Manual / Smart / Sleep / …).
- **Multi-region** — country code is configurable (TW, FI, SE, …); the integration picks the correct regional endpoint automatically.

## Installation

### HACS (recommended)

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PinLin&repository=electrolux-homeassistant&category=integration)

1. Open **HACS** in Home Assistant.
2. Three-dot menu → **Custom repositories**.
3. Add `https://github.com/PinLin/electrolux-homeassistant`, category **Integration**.
4. Install, then restart Home Assistant.

### Manual

1. Copy `custom_components/electrolux/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=electrolux)

Go to **Settings → Devices & Services → Add Integration → Electrolux**:

1. Enter your Electrolux email, password, and country code (e.g. `TW`, `FI`, `SE`).
2. Linked appliances appear as devices in the integration.

Three account-level diagnostic sensors are also created — `Appliances`, `Last Update`, `Token Expiry` — for monitoring connection health.

## Entities

Entities are created dynamically based on what each appliance reports. For an air purifier (e.g. Pure A9):

| Entity | Type | Description |
|---|---|---|
| `fan.*` | `fan` | Power, percentage, and preset mode (Auto / Manual / Smart / Sleep) |
| `*_temperature` | `sensor` | Ambient temperature |
| `*_humidity` | `sensor` | Ambient humidity |
| `*_pm1` / `*_pm2_5` / `*_pm10` | `sensor` | Particulate matter concentration |
| `*_eco2` | `sensor` | Equivalent CO₂ (estimated from TVOC) |
| `*_tvoc` | `sensor` | Total volatile organic compounds |
| `*_filter_life` | `sensor` | Remaining filter life |
| `*_rssi` | `sensor` | Wi-Fi signal strength in dBm (diagnostic) |
| `*_signal_quality` | `sensor` | Vendor-reported signal quality (diagnostic) |
| `*_connection_state` | `sensor` | Appliance cloud connection state (diagnostic) |
| `*_filter_cover_open` | `binary_sensor` | Filter cover open (`PROBLEM`) |
| `*_pm2_5_sensor_error` (and others) | `binary_sensor` | Sensor / communication faults (`PROBLEM`) |
| `*_ui_light` | `switch` | UI indicator light |
| `*_safety_lock` | `switch` | Child safety lock |
| `*_ionizer` | `switch` | Ionizer |

`entity_id` is derived from the cloud appliance ID, so non-ASCII appliance names don't get pinyin-slugified.

## Notes

- PRs and issues for additional appliance types are welcome — open an issue if a property mapping is missing.

## License

[MIT License](LICENSE)
