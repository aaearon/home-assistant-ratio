# Ratio EV Charging — Home Assistant Integration

Home Assistant integration for [Ratio](https://ratio.energy/) EV chargers, backed by the [`aioratio`](https://pypi.org/project/aioratio/) async client library.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

## What this does

Adds your Ratio EV charger(s) to Home Assistant via the same cloud API the official mobile app uses. One integration instance per Ratio account; one HA device per charger; entities for live status, energy, and control.

This is an unofficial integration. Not affiliated with Ratio.

## Status

Early. Auth, polling, start/stop, charge-mode and active-vehicle selects, solar/schedule number controls, session history statistics, and dynamic charger discovery have been smoke-tested against a real Ratio Solar charger. See [Known limitations](#known-limitations) for the remaining caveats.

## Install

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/aaearon/home-assistant-ratio` as type **Integration**.
3. Install "Ratio EV Charging".
4. Restart Home Assistant.
5. **Settings → Devices & Services → Add Integration → "Ratio EV Charging"**, enter your Ratio app email + password.

### Manual

Copy `custom_components/ratio/` into your Home Assistant `config/custom_components/` directory and restart. Then add via the UI as above.

Home Assistant will install `aioratio==0.6.0` from PyPI automatically; no extra Python deps to manage.

## Removing the Integration

1. Go to **Settings → Devices & Services**.
2. Find the **Ratio EV Charging** entry and click the three-dot menu (⋮).
3. Select **Delete**.

Token files (`.storage/ratio_<entry_id>.tokens`) and preference storage are cleaned up automatically when the config entry is removed. No manual file deletion is needed.

## Supported Devices

- **Ratio Solar** — the primary charger model tested with this integration
- Any Ratio charger connected to the Ratio cloud should work, as the integration uses the same cloud API as the official mobile app

The integration discovers chargers automatically from your Ratio account. Each charger appears as a separate device in Home Assistant.

## What you get

One device per charger, with the following entities:

| Platform | Entity | Source |
|---|---|---|
| sensor | `actual_charging_power` (W) | `charge_session_status.actual_charging_power` |
| sensor | `cloud_connection_state` | `cloud_connection_state` |
| sensor | `charging_state` | `charger_status.indicators.charging_state` |
| sensor (diagnostic, disabled by default) | `firmware_update_status` | `charger_firmware_status.firmware_update_status` |
| sensor | `last_session_energy`, `last_session_duration`, `last_session_started_at`, `last_session_ended_at`, `last_session_vehicle` | derived from most recent session in history |
| binary_sensor | `vehicle_connected`, `charge_session_active`, `charging_paused`, `error`, `charging_disabled` (with `reason` attribute), `charging_authorized`, `power_reduced_by_dso` | derived from `charger_status.indicators` |
| binary_sensor (diagnostic) | `firmware_update_available`, `firmware_update_allowed` | `charger_firmware_status` |
| switch | `charging` | `start_charge` / `stop_charge`, gated on `is_charge_start_allowed` / `is_charge_stop_allowed` |
| select | `charge_mode` | `user_settings.charging_mode` (PUT via `set_user_settings`) |
| select | `active_vehicle` | HA-side preference passed to the next `start_charge`, persisted across restarts |
| number | `sun_on_delay_minutes`, `sun_off_delay_minutes`, `pure_solar_starting_current`, `smart_solar_starting_current` | `solar_settings` (GET/PUT) |
| number | `maximum_charging_current`, `minimum_charging_current` | `user_settings` (GET/PUT) |
| button | `grant_upgrade_permission` | approves queued firmware update jobs |
| sensor (diagnostic) | `cpc_serial_number`, `hardware_type`, `firmware_version` | `diagnostics` endpoint — product info |
| sensor (diagnostic, disabled by default) | `hardware_version`, `connectivity_firmware_version`, `connectivity_hardware_version` | `diagnostics` endpoint — product info |
| sensor (diagnostic) | `wifi_ssid`, `wifi_rssi` (dBm), `connection_medium` | `diagnostics` endpoint — network status |
| sensor (diagnostic, disabled by default) | `wifi_ip`, `ethernet_ip` | `diagnostics` endpoint — network status |
| sensor (diagnostic, disabled by default) | `cpms_name`, `cpms_url` | `diagnostics` endpoint — OCPP status |
| sensor (diagnostic) | `charge_point_identifier` | `installerOcpp` settings |
| binary_sensor (diagnostic) | `wifi_connected`, `ethernet_connected`, `backend_connected`, `ocpp_connected`, `time_synchronized` | `diagnostics` endpoint — connectivity |
| switch (config) | `ocpp_enabled` | `installerOcpp` settings — `enabled` field |
| select (config) | `cpms` | `installerOcpp` settings — CPMS selection from operator list |
| text (config) | `charge_point_identifier` | `installerOcpp` settings — writable OCPP CPID |
| — | `ratio:energy_<serial>` (external statistic) | long-term energy statistics imported from session history via `import_session_history` |

Polling interval defaults to **60 s** (one `chargers_overview()` call per cycle, regardless of how many chargers).

### Services

| Service | Target | Parameters | Response |
|---|---|---|---|
| `ratio.start_charge` | `device_id` | `vehicle_id?` | — |
| `ratio.stop_charge` | `device_id` | — | — |
| `ratio.set_schedule` | `device_id` | `slots` (list of `{start, end, days}`) | — |
| `ratio.add_vehicle` | — | `vehicle_name`, `license_plate?` | `{vehicle_id}` |
| `ratio.remove_vehicle` | — | `vehicle_id` | — |
| `ratio.import_session_history` | — | `begin_time`, `end_time` | `{imported: {serial: count}}` |

Target a specific charger via Home Assistant's device picker (`device_id`).

### Function Summary

| Function | How |
|----------|-----|
| Monitor charging state | Binary sensors and state sensors |
| Start / stop charging | Switch entity or `ratio.start_charge` / `ratio.stop_charge` services |
| Configure charge mode | Select entity (`Smart`, `SmartSolar`, `PureSolar`) |
| Set preferred vehicle | Select entity (used for next `start_charge`) |
| Adjust solar settings | Number entities (sun delays, starting currents) |
| Adjust current limits | Number entities (min/max charging current) |
| Manage vehicles | `ratio.add_vehicle` / `ratio.remove_vehicle` services |
| Set charge schedule | `ratio.set_schedule` service |
| Import session history | `ratio.import_session_history` service → long-term statistics |
| Approve firmware updates | Button entity |
| View diagnostics | Settings → Devices & Services → ⋮ → Download diagnostics |

## Configuration Parameters

The integration exposes several number and select entities that let you configure charger behavior directly from Home Assistant:

| Entity | Type | Description |
|--------|------|-------------|
| Charge mode | Select | Switching between `Smart`, `SmartSolar`, and `PureSolar` charging modes |
| Active vehicle | Select | Which vehicle to associate with the next `start_charge` command (HA-side preference, persisted across restarts) |
| CPMS | Select | CPMS (Central Point Management System) operator selection from the installer-provided list |
| Sun on delay | Number | Minutes of sustained solar surplus before solar charging starts |
| Sun off delay | Number | Minutes after solar surplus drops before solar charging stops |
| Pure solar starting current | Number | Minimum current (A) to begin a pure-solar session |
| Smart solar starting current | Number | Minimum current (A) to begin a smart-solar session |
| Maximum charging current | Number | Upper current limit (A) for the charger |
| Minimum charging current | Number | Lower current limit (A) for the charger |
| OCPP enabled | Switch | Enable or disable OCPP on the charger (only available when change is permitted by the API) |
| Charge point identifier | Text | Writable OCPP charge point ID; maximum length enforced by the API |

Number, select, switch, and text config entities write to the Ratio cloud API when changed.

## Data Update

The integration uses two polling coordinators:

| Coordinator | Interval | What it fetches |
|-------------|----------|-----------------|
| Main | 60 seconds | Charger status, user settings, solar settings, vehicles, diagnostics, OCPP settings |
| History | 300 seconds (5 min) | Completed charge sessions for long-term energy statistics |

After any command (start/stop charge, change settings), the main coordinator triggers an immediate refresh so entity states update without waiting for the next poll cycle.

The integration uses the `cloud_polling` IoT class — there is no local network communication. All data flows through the Ratio cloud REST API.

## Automation Examples

### Start charging when electricity price is low

```yaml
automation:
  - alias: "Start EV charging on low price"
    trigger:
      - platform: numeric_state
        entity_id: sensor.electricity_price
        below: 0.10
    condition:
      - condition: state
        entity_id: binary_sensor.ratio_<serial>_vehicle_connected
        state: "on"
    action:
      - service: switch.turn_on
        target:
          entity_id: switch.ratio_<serial>_charging
```

### Notify when a charging session completes

```yaml
automation:
  - alias: "Notify charging complete"
    trigger:
      - platform: state
        entity_id: binary_sensor.ratio_<serial>_charging
        from: "on"
        to: "off"
    action:
      - service: notify.mobile_app
        data:
          title: "Charging complete"
          message: >
            Session finished. Energy delivered:
            {{ states('sensor.ratio_<serial>_last_session_energy') }} Wh
```

### Switch to solar mode during the day

```yaml
automation:
  - alias: "Solar charging during daytime"
    trigger:
      - platform: sun
        event: sunrise
        offset: "+01:00:00"
    action:
      - service: select.select_option
        target:
          entity_id: select.ratio_<serial>_charge_mode
        data:
          option: "PureSolar"
```

Replace `<serial>` with your charger's serial number (lowercase). You can find the actual entity IDs in **Settings > Devices & Services > Ratio EV Charging > Entities**.

## Use Cases

### Solar-optimized charging

Use the `PureSolar` or `SmartSolar` charge mode to charge your EV primarily from solar panels. Adjust the sun on/off delay and starting current settings to match your solar installation's characteristics.

### Scheduled charging

Use the `ratio.set_schedule` service to set weekly charging windows, e.g. only charge during off-peak hours. Combine with automations to dynamically adjust the schedule based on energy prices.

### Multi-vehicle management

Register multiple vehicles with `ratio.add_vehicle` and use the Active Vehicle select to control which vehicle gets attributed to the next charging session. The preferred vehicle is persisted per charger across restarts.

## How it works

```
+--------------------------+
| Home Assistant           |
|                          |
|  config_flow.py  ----+   |
|  coordinator.py      |   |
|  sensor / switch /   |   |
|  select / number /   |   |
|  button / services   |   |
+--------------------|-----+
                     v
              +---------------+
              |   aioratio    |   <-- pinned: aioratio==0.6.0
              |  (PyPI lib)   |
              +-------|-------+
                      v
              +---------------+         +-----------+
              | AWS Cognito   |  +----> |  Ratio    |
              | (USER_SRP +   |  |      |  cloud    |
              |  DEVICE_SRP)  |  |      |  REST API |
              +-------|-------+  |      +-----------+
                      v          |
              tokens persisted to
              .storage/ratio_<entry_id>.tokens
              (atomic write, mode 0600)
```

- One `DataUpdateCoordinator` per config entry (account). All entities for all chargers under that account share it. Each poll calls `chargers_overview()` plus per-charger `user_settings`, `solar_settings`, `diagnostics`, and `ocpp_settings` in parallel; CPMS options are refreshed every 10th tick (~10 min). Entities select their slice from the aggregated `RatioData` snapshot.
- Token storage uses `aioratio.JsonFileTokenStore` rooted at `hass.config.path(".storage/ratio_<entry_id>.tokens")`. The Cognito DeviceKey/DeviceGroupKey/DevicePassword are persisted alongside the access/refresh tokens so subsequent restarts use the DEVICE_SRP_AUTH fast-path without re-prompting.
- On `RatioAuthError` during initial login or coordinator refresh, HA raises `ConfigEntryAuthFailed`, triggers reauth, and prompts for a new password. If setup fails after the client has connected, the client session is cleaned up before re-raising.

## Known limitations

- **Sensor coverage is bounded by the cloud API.** Voltage, current, and session/total energy are not exposed by the upstream `chargers_overview` endpoint and therefore not available as sensors. `actual_charging_power` is reported in watts.
- **`charge_mode` allowed values fall back to a hardcoded list** (`Smart`, `SmartSolar`, `PureSolar`) when the cloud omits `allowedValues`. If Ratio adds modes the fallback will need updating.
- **Password storage**: stored in HA config entry data and persisted in `.storage/core.config_entries` like other config entry data. It does not use `secrets.yaml`, and protection relies on Home Assistant host security rather than separate encryption in this integration.
- **Account-level services require a single config entry.** `add_vehicle`, `remove_vehicle`, and `import_session_history` raise an error if multiple Ratio config entries exist, since they operate on the account level and there is no device picker to disambiguate.
- **Rate limiting**: The Ratio cloud API enforces rate limits. The integration handles 429 responses with automatic backoff, but aggressive polling or frequent command calls may trigger temporary throttling.
- **DSO power reduction is read-only.** The `power_reduced_by_dso` binary sensor reflects whether the Distribution System Operator has reduced available power, but this cannot be controlled from HA — it is set by the DSO via the charger's smart grid interface.

## Diagnostics

`Settings → Devices & Services → Ratio EV Charging → ⋮ → Download diagnostics`. The dump redacts: email, password, all tokens, device key/group/password, charger serial numbers, license plates, CPMS URLs, charge point identifiers, WiFi SSIDs, and IP addresses.

## Troubleshooting

### Authentication errors

- **"Invalid email or password"** during setup: verify you can sign in with the same credentials in the Ratio mobile app.
- **Reauth prompted after working**: the Ratio cloud tokens expired and could not be refreshed. Re-enter your password when prompted. This can happen after extended cloud outages or password changes.

### Stale or missing data

- Entities showing "unavailable": the charger may be offline or the Ratio cloud may be unreachable. Check your charger's internet connection.
- Settings not updating: the integration polls every 60 seconds. If you changed a setting via the Ratio app, wait up to a minute for HA to reflect it.
- After a restart, entities may briefly show "unknown" until the first poll completes.

### Rate limiting

The Ratio cloud API enforces rate limits. If the integration hits a rate limit, the coordinator backs off automatically using HA's built-in exponential backoff. You'll see `rate limited; backing off` in the logs. Normal polling resumes once the limit window resets.

### Cloud connectivity

The integration requires an active internet connection to communicate with the Ratio cloud. It does not support local-only operation. If the Ratio cloud is down, all entities will become unavailable until connectivity is restored.

### Debug logging

To enable debug logs for the integration:

```yaml
logger:
  logs:
    custom_components.ratio: debug
    aioratio: debug
```

## Develop

```bash
git clone https://github.com/aaearon/home-assistant-ratio
cd home-assistant-ratio
pip install pytest-homeassistant-custom-component
pytest
```

Tests use `MockConfigEntry`, the real `device_registry` fixture, and a
`setup_integration` fixture that runs the full `async_setup_entry` path with a
mocked `RatioClient`. Coordinator tests call
`async_config_entry_first_refresh()` / `async_refresh()` (not the private
`_async_update_data()`). Time-dependent tests use the `freezer` fixture.

Bumping the library:

1. Land changes in [`aioratio`](https://github.com/aaearon/aioratio), tag a release, watch CI publish to PyPI.
2. Update `custom_components/ratio/manifest.json` `requirements` pin (e.g. `"aioratio==0.5.0"`).
3. Bump `manifest.json` `version` and tag the integration release.

The pin is `==`, not `>=`, matching HA Core convention.

## Notes for LLMs and contributors

The integration is intentionally a thin shell over `aioratio`. If you find yourself reaching for `boto3`, `warrant`, or HTTP code inside this repo, that work belongs in the library, not here. Cross-check the library's surface at https://github.com/aaearon/aioratio.

Files of interest:

- `custom_components/ratio/__init__.py` — entry setup/teardown, token store wiring, platform forwarding.
- `custom_components/ratio/coordinator.py` — single `DataUpdateCoordinator` per account; classifies `RatioAuthError` → `ConfigEntryAuthFailed`.
- `custom_components/ratio/config_flow.py` — user step + reauth.
- `custom_components/ratio/sensor.py`, `binary_sensor.py`, `switch.py`, `select.py`, `number.py`, `button.py`, `text.py` — `CoordinatorEntity` subclasses keyed by serial.
- `custom_components/ratio/statistics.py` — long-term energy statistics from session history.
- `custom_components/ratio/diagnostics.py` — redaction set.

## License

MIT.

## Disclaimer

Unofficial. Use at your own risk; the underlying API is reverse-engineered from the official mobile app and is not contractually stable. No affiliation with Ratio.
