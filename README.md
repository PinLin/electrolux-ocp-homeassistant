# Electrolux OCP — Home Assistant Integration

[![GitHub Release](https://img.shields.io/github/release/PinLin/electrolux-ocp-homeassistant.svg?style=flat-square)](https://github.com/PinLin/electrolux-ocp-homeassistant/releases)
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

[![Add to HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=PinLin&repository=electrolux-ocp-homeassistant&category=integration)

1. Open **HACS** in Home Assistant.
2. Three-dot menu → **Custom repositories**.
3. Add `https://github.com/PinLin/electrolux-ocp-homeassistant`, category **Integration**.
4. Install, then restart Home Assistant.

### Manual

1. Copy `custom_components/electrolux_ocp/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

## Configuration

[![Add Integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=electrolux_ocp)

Go to **Settings → Devices & Services → Add Integration → Electrolux**:

1. Enter your Electrolux email, password, and country code (e.g. `TW`, `FI`, `SE`).
2. Linked appliances appear as devices in the integration.

Three account-level diagnostic sensors are also created — `Appliances`, `Last Update`, `Token Expiry` — for monitoring connection health.

### Configuration parameters

| Field | Required | Description |
|---|---|---|
| Email | Yes | The email address registered with your Electrolux account. |
| Password | Yes | Used once to sign in and obtain a refresh token. The password itself is **not** persisted; the integration only keeps the rotating tokens. |
| Country code | Yes | Two-letter ISO country code (`TW`, `FI`, `SE`, `DE`, `GB`, …). Determines which regional API and WebSocket endpoint the integration talks to. |

There is no YAML configuration: the integration is configured entirely through the Home Assistant UI. Re-authentication is triggered automatically when the refresh token is rejected; you'll be prompted for the password again.

## Data updates

The integration is `cloud_push` — appliance state is delivered through a long-lived WebSocket connection, so most changes show up in Home Assistant within a second.

A 30-minute polling cycle backs the WebSocket up by:

- refreshing the access token before its 12-hour TTL expires,
- re-syncing the appliance list (newly added or removed appliances are picked up automatically and the device registry is cleaned accordingly),
- recovering the WebSocket connection if it died silently.

Polling cadence is fixed (not user-configurable) and intentionally slow so the integration does not put unnecessary load on the OCP gateway.

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

The `Signal Strength` (dBm RSSI) and `Signal Quality` sensors are diagnostic and disabled by default — enable them from the entity registry if you want to graph radio performance.

## Examples

A few automation snippets that use entities created by this integration. Replace `pure_a9_living_room` with your own appliance slug.

**Notify when filter life is low:**

```yaml
alias: Pure A9 filter low
trigger:
  - platform: numeric_state
    entity_id: sensor.electrolux_ocp_<id>_filterlife
    below: 10
action:
  - service: notify.mobile_app
    data:
      message: "Pure A9 filter is at {{ states('sensor.electrolux_ocp_<id>_filterlife') }}%."
```

**Open the filter cover problem alert:**

```yaml
alias: Pure A9 filter cover open
trigger:
  - platform: state
    entity_id: binary_sensor.electrolux_ocp_<id>_dooropen
    to: "on"
action:
  - service: notify.mobile_app
    data:
      message: "Pure A9 filter cover is open."
```

**Switch to Sleep mode at night:**

```yaml
alias: Pure A9 night mode
trigger:
  - platform: time
    at: "23:00:00"
action:
  - service: fan.set_preset_mode
    target:
      entity_id: fan.electrolux_ocp_<id>
    data:
      preset_mode: sleep
```

## Use cases

- **Indoor air quality dashboard.** Combine `temperature`, `humidity`, `pm2_5`, `tvoc`, and `eco2` sensors on a Lovelace card to show the current air state of every room with a Pure A9.
- **Auto fan via PM2.5.** Use a numeric trigger on the PM2.5 sensor to switch the fan into Smart or Manual at higher speeds when air quality drops.
- **Filter maintenance reminder.** Subscribe to the `filter_life` sensor and notify when it falls below 10% so the filter is replaced before performance degrades.
- **Quiet hours.** Schedule the fan into Sleep preset at night and back to Auto in the morning.
- **Health surface.** Use the diagnostic `Last Update` and `Token Expiry` sensors in a system dashboard to spot cloud connectivity problems early.

## Troubleshooting

The integration creates three account-level diagnostic sensors that are designed for this purpose:

| Sensor | Tells you |
|---|---|
| `sensor.electrolux_ocp_appliances` | How many appliances the integration has discovered. Should match what you see in the Electrolux app. |
| `sensor.electrolux_ocp_last_update` | Timestamp of the most recent successful polling refresh. If this stops updating, polling is stuck. |
| `sensor.electrolux_ocp_token_expiry` | When the current access token expires. Falling behind means token rotation has stalled. |

**Re-authentication banner.** Home Assistant prompts for re-authentication when the refresh token has been rejected — usually because the account password changed, or because the OCP backend invalidated the session. Click **Reconfigure** and enter the current password.

**`429` / `cas_3404` rate limiting.** OCP rate-limits aggressive token refreshing. The integration keeps a local cooldown so a misconfigured installation does not trigger this on its own; if you do see it, leave the integration alone for a few minutes and it will recover automatically.

**WebSocket disconnects in logs.** Brief disconnects with `WebSocket closed, reconnecting…` lines are normal; OCP closes idle connections periodically. Persistent failures with `WebSocket auth failed` after token refresh indicate a real authentication issue — re-authenticate from **Settings → Devices & Services**.

**Diagnostic dump.** From the integration page, choose **Download diagnostics** to capture a redacted snapshot of the current state (tokens, account ID, MAC addresses, and serial numbers are scrubbed). Attach it to bug reports.

**Known limitation: only one device line confirmed.** The integration was built around the Electrolux Pure A9 (`PUREA9`) air purifier and is verified there. Other appliance types may load and surface partial sensors but will likely not expose controls until a per-device-line capability map is added.

## Removing the integration

1. Go to **Settings → Devices & Services**.
2. Find **Electrolux OCP**, open the three-dot menu, choose **Delete**.
3. Home Assistant unloads the integration, cancels the WebSocket task, and removes all entities and devices it created.

If you also want to remove the integration files, uninstall it through HACS (or delete `custom_components/electrolux_ocp/`) and restart Home Assistant.

## Notes

- PRs and issues for additional appliance types are welcome — open an issue if a property mapping is missing.

## Disclaimer

This project is an unofficial, community-maintained integration. It is **not affiliated with, endorsed by, or supported by Electrolux Group, AEG, or any of their subsidiaries**. "Electrolux", "AEG", and related trademarks are the property of their respective owners.

The integration interacts with private Electrolux services originally intended for the official Electrolux mobile applications. These services may change, become rate-limited, or be withdrawn at any time without notice, which may break this integration. Use at your own risk.

The software is provided "as is" without any warranty (see [LICENSE](LICENSE)) and the maintainers accept no liability for any consequences of its use, including but not limited to account suspension, appliance malfunction, or data loss.

## License

[MIT License](LICENSE)
