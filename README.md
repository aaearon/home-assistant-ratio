# Ratio EV Charging — Home Assistant Integration

Home Assistant integration for [Ratio](https://ratio.energy/) EV chargers, backed by the [`aioratio`](https://pypi.org/project/aioratio/) async client library.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

## What this does

Adds your Ratio EV charger(s) to Home Assistant via the same cloud API the official mobile app uses. One integration instance per Ratio account; one HA device per charger; entities for live status, energy, and control.

This is an unofficial integration. Not affiliated with Ratio.

## Status

Early. Auth, polling, start/stop, charge-mode and active-vehicle selects, solar/schedule number controls, session history statistics, and dynamic charger discovery have been smoke-tested against a real charger. See [Known limitations](#known-limitations) for the remaining caveats.

## Install

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/aaearon/home-assistant-ratio` as type **Integration**.
3. Install "Ratio EV Charging".
4. Restart Home Assistant.
5. **Settings → Devices & Services → Add Integration → "Ratio EV Charging"**, enter your Ratio app email + password.

### Manual

Copy `custom_components/ratio/` into your Home Assistant `config/custom_components/` directory and restart. Then add via the UI as above.

Home Assistant will install `aioratio==0.5.0` from PyPI automatically; no extra Python deps to manage.

## What you get

One device per charger, with the following entities:

| Platform | Entity | Source |
|---|---|---|
| sensor | `actual_charging_power` (W) | `charge_session_status.actual_charging_power` |
| sensor | `cloud_connection_state` | `cloud_connection_state` |
| sensor | `charging_state` | `charger_status.indicators.charging_state` |
| sensor | `firmware_update_status` | `charger_firmware_status.firmware_update_status` |
| sensor | `last_session_energy`, `last_session_duration`, `last_session_started_at`, `last_session_ended_at`, `last_session_vehicle` | derived from most recent session in history |
| binary_sensor | `vehicle_connected`, `charge_session_active`, `charging_paused`, `error`, `charging_disabled` (with `reason` attribute), `charging_authorized`, `power_reduced_by_dso` | derived from `charger_status.indicators` |
| switch | `charging` | `start_charge` / `stop_charge`, gated on `is_charge_start_allowed` / `is_charge_stop_allowed` |
| select | `charge_mode` | `user_settings.charging_mode` (PUT via `set_user_settings`) |
| select | `active_vehicle` | HA-side preference passed to the next `start_charge`, persisted across restarts |
| number | `sun_on_delay_minutes`, `sun_off_delay_minutes`, `pure_solar_starting_current`, `smart_solar_starting_current` | `solar_settings` (GET/PUT) |
| number | `maximum_charging_current`, `minimum_charging_current` | `user_settings` (GET/PUT) |
| button | `grant_upgrade_permission` | approves queued firmware update jobs |
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
              |   aioratio    |   <-- pinned: aioratio==0.5.0
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

- One `DataUpdateCoordinator` per config entry (account). All entities for all chargers under that account share it. The coordinator calls `chargers_overview()` (a single aggregate cloud call) per poll and entities select their slice.
- Token storage uses `aioratio.JsonFileTokenStore` rooted at `hass.config.path(".storage/ratio_<entry_id>.tokens")`. The Cognito DeviceKey/DeviceGroupKey/DevicePassword are persisted alongside the access/refresh tokens so subsequent restarts use the DEVICE_SRP_AUTH fast-path without re-prompting.
- On `RatioAuthError` during initial login or coordinator refresh, HA raises `ConfigEntryAuthFailed`, triggers reauth, and prompts for a new password. If setup fails after the client has connected, the client session is cleaned up before re-raising.

## Known limitations

- **Sensor coverage is bounded by the cloud API.** Voltage, current, and session/total energy are not exposed by the upstream `chargers_overview` endpoint and therefore not available as sensors. `actual_charging_power` is reported in watts.
- **`charge_mode` allowed values fall back to a hardcoded list** (`Smart`, `SmartSolar`, `PureSolar`) when the cloud omits `allowedValues`. If Ratio adds modes the fallback will need updating.
- **Password storage**: stored in HA config entry data and persisted in `.storage/core.config_entries` like other config entry data. It does not use `secrets.yaml`, and protection relies on Home Assistant host security rather than separate encryption in this integration.
- **Account-level services require a single config entry.** `add_vehicle`, `remove_vehicle`, and `import_session_history` raise an error if multiple Ratio config entries exist, since they operate on the account level and there is no device picker to disambiguate.

## Diagnostics

`Settings → Devices & Services → Ratio EV Charging → ⋮ → Download diagnostics`. The dump redacts: email, password, all tokens, device key/group/password, charger serial numbers, license plates.

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
- `custom_components/ratio/sensor.py`, `binary_sensor.py`, `switch.py`, `select.py`, `number.py`, `button.py` — `CoordinatorEntity` subclasses keyed by serial.
- `custom_components/ratio/statistics.py` — long-term energy statistics from session history.
- `custom_components/ratio/diagnostics.py` — redaction set.

## License

MIT.

## Disclaimer

Unofficial. Use at your own risk; the underlying API is reverse-engineered from the official mobile app and is not contractually stable. No affiliation with Ratio.
