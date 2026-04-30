# Ratio EV Charging — Home Assistant Integration

Home Assistant integration for [Ratio](https://ratio.energy/) EV chargers, backed by the [`aioratio`](https://pypi.org/project/aioratio/) async client library.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

## What this does

Adds your Ratio EV charger(s) to Home Assistant via the same cloud API the official mobile app uses. One integration instance per Ratio account; one HA device per charger; entities for live status, energy, and control.

This is an unofficial integration. Not affiliated with Ratio.

## Status

Early. Auth, polling, and start/stop have been smoke-tested against a real charger. Some entity field paths and select dropdowns are TODO pending live refinement — see [Known limitations](#known-limitations).

## Why a new integration

There is an existing community integration ([RowanRamasray/Ratio_Ev_Charger](https://github.com/RowanRamasray/Ratio_Ev_Charger)). It works but bundles `boto3` + `warrant` directly inside the custom component, fuses protocol logic with Home Assistant internals, and only covers part of the API. This rewrite extracts the protocol layer into a separate library ([`aioratio`](https://github.com/aaearon/aioratio)) and keeps the HA integration thin.

## Install

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/aaearon/home-assistant-ratio` as type **Integration**.
3. Install "Ratio EV Charging".
4. Restart Home Assistant.
5. **Settings → Devices & Services → Add Integration → "Ratio EV Charging"**, enter your Ratio app email + password.

### Manual

Copy `custom_components/ratio/` into your Home Assistant `config/custom_components/` directory and restart. Then add via the UI as above.

Home Assistant will install `aioratio==0.1.0` from PyPI automatically; no extra Python deps to manage.

## What you get

One device per charger, with the following entities:

| Platform | Entity | Source |
|---|---|---|
| sensor | `actual_charging_power` | `charge_session_status.actual_charging_power` |
| sensor | `cloud_connection_state` | `cloud_connection_state` |
| sensor | `charging_state` | `charger_status.indicators.charging_state` |
| binary_sensor | `vehicle_connected`, `charge_session_active`, `charging_paused`, `error` | derived from `charger_status` / `charge_session_status` |
| switch | `charging` | `start_charge` / `stop_charge` |
| select | `charge_mode` (skeleton — TODO) | TODO — planned source: `user_settings.charging_mode` |
| select | `active_vehicle` (skeleton — TODO) | TODO — planned source: `vehicles()` |

Polling interval defaults to **60 s** (one `chargers_overview()` call per cycle, regardless of how many chargers).

### Services

- `ratio.start_charge(device_id, vehicle_id?)`
- `ratio.stop_charge(device_id)`
- `ratio.set_schedule(device_id, slots)`

Target a specific charger via Home Assistant's device picker (`device_id`).

## How it works

```
+--------------------------+
| Home Assistant           |
|                          |
|  config_flow.py  ----+   |
|  coordinator.py      |   |
|  sensor / switch /   |   |
|  select / services   |   |
+--------------------|-----+
                     v
              +---------------+
              |   aioratio    |   <-- pinned: aioratio==0.1.0
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
- On `RatioAuthError` from the coordinator, HA triggers reauth and prompts for a new password.

## Known limitations

These are explicitly TODO and tracked as issues / PRs:

- **Select entities are skeletons.** `charge_mode` and `active_vehicle` create the entities but options are empty and setters are no-ops. Use the services for now.
- **Sensor coverage is conservative** until live payload captures confirm field availability/units. Power unit (W vs kW), session/total energy, current/voltage are not yet exposed if not present on the live `Indicators` model.
- **Switch start/stop is not idempotent** and ignores `is_charge_start_allowed` / `is_charge_stop_allowed` from the model.
- **Newly-added chargers** post-setup require an integration reload to get entities (no dynamic discovery yet).
- **Rate-limit handling**: `RatioRateLimitError` (HTTP 429) currently surfaces as a generic update failure with no Retry-After backoff.
- **Password storage**: stored in HA config entry data and persisted in `.storage/core.config_entries` like other config entry data. It does not use `secrets.yaml`, and protection relies on Home Assistant host security rather than separate encryption in this integration.

## Diagnostics

`Settings → Devices & Services → Ratio EV Charging → ⋮ → Download diagnostics`. The dump redacts: email, password, all tokens, device key/group/password, charger serial numbers, license plates.

## Develop

```bash
git clone https://github.com/aaearon/home-assistant-ratio
cd home-assistant-ratio
pip install pytest-homeassistant-custom-component
pytest
```

Bumping the library:

1. Land changes in [`aioratio`](https://github.com/aaearon/aioratio), tag a release, watch CI publish to PyPI.
2. Update `custom_components/ratio/manifest.json` `requirements` pin (e.g. `"aioratio==0.2.0"`).
3. Bump `manifest.json` `version` and tag the integration release.

The pin is `==`, not `>=`, matching HA Core convention.

## Notes for LLMs and contributors

The integration is intentionally a thin shell over `aioratio`. If you find yourself reaching for `boto3`, `warrant`, or HTTP code inside this repo, that work belongs in the library, not here. Cross-check the library's surface at https://github.com/aaearon/aioratio.

Files of interest:

- `custom_components/ratio/__init__.py` — entry setup/teardown, token store wiring, platform forwarding.
- `custom_components/ratio/coordinator.py` — single `DataUpdateCoordinator` per account; classifies `RatioAuthError` → `ConfigEntryAuthFailed`.
- `custom_components/ratio/config_flow.py` — user step + reauth.
- `custom_components/ratio/sensor.py`, `binary_sensor.py`, `switch.py`, `select.py` — `CoordinatorEntity` subclasses keyed by serial.
- `custom_components/ratio/diagnostics.py` — redaction set.

## License

MIT.

## Disclaimer

Unofficial. Use at your own risk; the underlying API is reverse-engineered from the official mobile app and is not contractually stable. No affiliation with Ratio.
