# Ratio EV Charging — Home Assistant Integration

Home Assistant integration for [Ratio](https://www.ratio-electric.com/) EV chargers, backed by the [`aioratio`](https://pypi.org/project/aioratio/) async client library.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![CI](https://github.com/aaearon/home-assistant-ratio/actions/workflows/ci.yaml/badge.svg)](https://github.com/aaearon/home-assistant-ratio/actions/workflows/ci.yaml)

Adds your Ratio EV charger(s) to Home Assistant via the same cloud API the official mobile app uses. One integration instance per Ratio account; one HA device per charger, discovered automatically. Entities for live status, energy, and control. Smoke-tested against a real Ratio Solar charger; any cloud-connected Ratio charger should work. Unofficial; not affiliated with Ratio. See [Known limitations](#known-limitations) for caveats.

## Install

### HACS (recommended)

1. HACS → three-dot menu → **Custom repositories**.
2. Add `https://github.com/aaearon/home-assistant-ratio` as type **Integration**.
3. Install "Ratio EV Charging".
4. Restart Home Assistant.
5. **Settings → Devices & Services → Add Integration → "Ratio EV Charging"**, enter your Ratio app email + password.

### Manual

Copy `custom_components/ratio/` into your Home Assistant `config/custom_components/` directory and restart. Then add via the UI as above. Home Assistant installs `aioratio[ble]` from PyPI automatically.

To remove: **Settings → Devices & Services → ⋮ → Delete**. Token files and preference storage are cleaned up automatically.

## What you get

One device per charger, with the following entities:

| Platform | Entity | Source |
|---|---|---|
| sensor | `actual_charging_power` (W) | `charge_session_status.actual_charging_power` |
| sensor | `cloud_connection_state` | `cloud_connection_state` |
| sensor | `charging_state` | `charger_status.indicators.charging_state` |
| sensor (diagnostic, disabled by default) | `firmware_update_status` | `charger_firmware_status.firmware_update_status` |
| sensor | `last_session_energy`, `last_session_duration`, `last_session_started_at`, `last_session_ended_at`, `last_session_vehicle` | derived from most recent session in history |
| binary_sensor | `vehicle_connected`, `charging`, `charge_session_active`, `charging_paused`, `error`, `charging_disabled` (with `reason` attribute), `charging_authorized`, `power_reduced_by_dso` | derived from `charger_status.indicators`. `charging` reports whether current is flowing (`chargingState in {Charging, ChargingWithVentilation, PausedByEVSE}`); `charge_session_active` ("Session active") reports the raw cloud `isChargeSessionActive` flag, which stays on through the post-stop VehicleDetected phase. On installs that pre-date this release the new sensor lands at `binary_sensor.ratio_<serial>_charging_2` because the slug is already taken; fresh installs get clean `_charging` / `_session_active` entity IDs. |
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
| binary_sensor (diagnostic) | `wifi_connected`, `ethernet_connected`, `backend_connected`, `ocpp_connected` | `diagnostics` endpoint — connectivity |
| binary_sensor (diagnostic, disabled by default) | `time_synchronized` | `diagnostics` endpoint — only reported by some firmwares |
| sensor (diagnostic, BLE only) | `voltage_phase_1/2/3` (V) | BLE `GetChargerSensorValues` — only available when Bluetooth is enabled |
| sensor (diagnostic, BLE only) | `current_phase_1/2/3` (A) | BLE `GetChargerSensorValues` — only available when Bluetooth is enabled |
| sensor (diagnostic, BLE only) | `ble_protocol_version` | Inspiro IPC Version characteristic — only available when Bluetooth is enabled |
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
| `ratio.set_schedule` | `device_id` | `slots` (list of `{start, end, days}`), `enabled?` (default `true`) | — |
| `ratio.add_vehicle` | — | `vehicle_name`, `license_plate?` | `{vehicle_id}` |
| `ratio.remove_vehicle` | — | `vehicle_id` | — |
| `ratio.import_session_history` | — | `begin_time`, `end_time` | `{imported: {serial: count}}` |
| `ratio.reconfigure_wifi` *(BLE only)* | `device_id` | `ssid`, `password?` | — |

Target a specific charger via Home Assistant's device picker (`device_id`). After any command, the coordinator triggers an immediate refresh.

## Bluetooth (optional)

The charger exposes a BLE GATT service (Inspiro IPC) the mobile app uses alongside the cloud. Enabling BLE per charger adds **per-phase voltage/current sensors** (not available from the cloud at all) and the **`ratio.reconfigure_wifi`** service. Cloud setup is required first; BLE is additive.

### Prerequisites

A Bluetooth adapter on the HA host (or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy/) in range — best-effort, see caveats below). The charger must be **bonded** (OS-level pairing) with the HA host before HA can connect.

### Bonding the charger (one-time)

On the HA host, pair via OS Bluetooth tools. Example with `bluetoothctl`:

```bash
bluetoothctl
scan on          # wait for RATIO_P<serial> to appear
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
scan off
```

The charger advertises as `RATIO_P<serial>` (e.g. `RATIO_P00000000013428`). Use the stable identity address shown after pairing, not the rotating scan MAC.

### Enabling and operating BLE

After pairing, HA shows a **"Discovered: Ratio Charger \<serial\>"** notification under **Settings → Devices & Services**. Click **Configure** to enable; per-phase sensors appear within ~45 s. Disable later via **Configure** on the integration. Per-charger **BLE poll period** (seconds, 1–60, default 3) is exposed in the same Configure dialog — leave at 3 s unless you have a reason to deviate.

Only one BLE central can connect at a time — opening the Ratio mobile app preempts HA, and BLE entities show **unavailable** until the app releases the link (~45 s). If the bond is lost (factory reset, re-pair from phone), HA creates a **Repair issue** with re-bond instructions.

### Bluetooth proxies (ESPHome) — caveats

Pairing via ESPHome proxy is **best-effort**. Constraints:

- **ESPHome firmware.** Active pairing requires **2024.6+** with `bluetooth_proxy: active: true`. Older firmware (`passive: true` only) surfaces as `NotImplementedError` in the HA log.
- **Bond persistence.** Bonds live in the proxy's NVS, not HA. Re-flashing or wiping the proxy drops the bond and re-triggers the Repair issue.
- **Diagnosing failures.** Pairing logs the scanner backend, source, and `proxy=True/False`; failures escalate to `WARNING`. If sensors stay unavailable, enable debug logging and share lines on [issue #27](https://github.com/aaearon/home-assistant-ratio/issues/27):

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.ratio: debug
      aioratio.ble: debug
      homeassistant.components.bluetooth: debug
      bleak: debug
      bleak_esphome: debug
  ```

If the proxy doesn't support active pairing, the cloud path keeps working — disable BLE via **Configure** to clear the Repair issue.

## Automation Example

Start charging when the electricity price drops, if a vehicle is plugged in:

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

Replace `<serial>` with your charger's serial number (lowercase); find actual entity IDs under **Settings → Devices & Services → Ratio EV Charging → Entities**.

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
              |   aioratio    |   <-- pinned in manifest.json
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

- **Cloud sensor coverage is bounded by the API.** Per-phase voltage and current are not exposed by `chargers_overview` and are only available when [Bluetooth is enabled](#bluetooth-optional). `actual_charging_power` (W) is available from the cloud. Session/total energy is not exposed by the cloud API.
- **`charge_mode` allowed values fall back to a hardcoded list** (`Smart`, `SmartSolar`, `PureSolar`) when the cloud omits `allowedValues`. If Ratio adds modes the fallback will need updating.
- **Password storage**: stored in HA config entry data and persisted in `.storage/core.config_entries` like other config entry data. It does not use `secrets.yaml`, and protection relies on Home Assistant host security rather than separate encryption in this integration.
- **Account-level services require a single config entry.** `add_vehicle`, `remove_vehicle`, and `import_session_history` raise an error if multiple Ratio config entries exist, since they operate on the account level and there is no device picker to disambiguate.
- **Rate limiting**: The Ratio cloud API enforces rate limits. The integration handles 429 responses with automatic backoff, but aggressive polling or frequent command calls may trigger temporary throttling.
- **Changing the maximum charging current can silently lower the smart-solar starting current.** In one observed case, a `maximumChargingCurrent` write of 16 → 15 A also rewrote `smartSolarStartingCurrent` from 16 to 15 A server-side, and restoring the maximum to 16 A did **not** restore it. The cascade happens in the Ratio cloud and cannot be prevented from HA. This is a single observation — the clamping rule (which values trigger it, and how the new value is derived) is not characterised. If you rely on a specific smart-solar starting current, write it explicitly after changing the maximum charging current.
- **DSO power reduction is read-only.** The `power_reduced_by_dso` binary sensor reflects whether the Distribution System Operator has reduced available power, but this cannot be controlled from HA — it is set by the DSO via the charger's smart grid interface.
- **`import_session_history` rejects already-processed windows.** If `begin_time` predates the history-import baseline (latest imported session timestamp, advanced by the live polling coordinator) for any charger, the service raises `ServiceValidationError`. This prevents non-monotonic energy statistics that would result from backfilling sessions earlier than the baseline. Workaround: re-add the integration to reset the baseline before backfilling.

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
- Settings changed **from Home Assistant** take about 10 seconds to show their confirmed value. The cloud needs a few seconds to make a write visible to a subsequent read, so the post-write refresh is deliberately deferred (`POST_WRITE_SETTLE_SECONDS`) rather than reading back stale data.

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
pip install pytest-homeassistant-custom-component aioratio ruff mypy
pytest
ruff check custom_components tests   # lint
ruff format custom_components tests  # format
mypy custom_components/ratio/        # type check
```

Tests use `MockConfigEntry`, the real `device_registry` fixture, and a
`setup_integration` fixture that runs the full `async_setup_entry` path with a
mocked `RatioClient`. Coordinator tests call
`async_config_entry_first_refresh()` / `async_refresh()` (not the private
`_async_update_data()`). Time-dependent tests use the `freezer` fixture.

Bumping the library: land changes in [`aioratio`](https://github.com/aaearon/aioratio), publish the release to PyPI, then bump the pin in **three** places — missing the CI ones makes CI install the old library while the code imports the new types, which fails at import, not subtly:

1. `custom_components/ratio/manifest.json` — the `requirements` entry (`aioratio[ble]==X.Y.Z`).
2. `.github/workflows/ci.yaml` — the `mypy` job's `pip install` line.
3. `.github/workflows/ci.yaml` — the `pytest` job's `pip install` line.

The pin is `==`, matching HA Core convention. The integration's own `version` in `manifest.json` is bumped separately, as part of cutting an integration release. `pyproject.toml` holds only tool configuration — there is no dependency there to bump.

### Cloud write contract

The Ratio cloud API is asymmetric. GET returns descriptor objects
(`{"value": 16, "lowerLimit": 6, "upperLimit": 32}`); PUT takes bare
serializer-native values, sparse at the top level — the app sends only the keys
the current screen changed. Write paths therefore use the `*Update` DTOs
(`UserSettingsUpdate`, `SolarSettingsUpdate`, `OcppSettingsUpdate`,
`ChargeScheduleUpdate`) and never write a GET model back. Every write test
asserts the exact body, and its keys come from the matching
`$$serializer.java` in the decompiled app, never from recollection.

`tests/conftest.py` mocks every cloud setter, so the per-platform tests stop at
the DTO. `tests/test_cloud_contract.py` closes that gap: it wires the **real**
`RatioClient` to a recording transport (the same pattern as `aioratio`'s own
`tests/test_client.py`) and asserts the JSON that would go on the wire —
`_coerce_body()`, the `{transactionId, <kind>Settings}` envelope, the `?id=`
parameter and the `set_charge_schedule()` type guard included. Any write path
whose contract with the library matters belongs there as well as in its
platform test; a breaking library change should turn it red.

Range validation on writes is fail-closed. `_default_min` / `_default_max` on
the number entities exist only so the frontend slider has two floats to render
when the cache is empty — they are **not** charger limits (the reference
charger reports `upperLimit` 16 where the constant says 32). Writes validate
against `_bounds()`, which returns the charger's own `lowerLimit`/`upperLimit`
or `None`, and a `None` refuses the write with `number_bounds_unknown`.

`_bounds()` returns `None` unless both limits are finite and `lower <= upper`;
`"NaN"` parses to a float, and a `NaN` bound would make every range comparison
false and wave any value through. `lower == upper` is legitimate (a charger
pinned to one value). A `None` also makes the entity **unavailable** — Home
Assistant's `number.set_value` handler range-checks and clamps against the
display fallbacks before the integration sees the call, so leaving an
unwritable entity available produced two different errors for the same state
depending on where the requested value fell.

Writes are not immediately readable. A PUT takes roughly 3-6 seconds to become
visible to a subsequent GET, so a command does not refresh inline; it arms a
one-shot settle timer (`POST_WRITE_SETTLE_SECONDS`, 10 s) and returns. An
inline refresh lands inside the propagation window, caches the pre-write
values, and leaves them on screen until the next 60 s poll.

The settle timer is deliberately **not** HA's request-refresh debouncer, which
fails this job in two independent ways. Non-immediate, it anchors its timer to
the *first* call of a burst — `Debouncer._async_schedule_or_call_now` only
flips `_execute_at_end_of_timer` when a timer already exists and never
reschedules — so writes at t=0 and t=9 would refresh at t=10, one second after
the second write. And `DataUpdateCoordinator._async_refresh` calls
`self._debounced_refresh.async_cancel()`, so any ordinary 60 s poll landing
inside the settle window would swallow the confirming read entirely. The
dedicated one-shot re-arms from the latest write and cannot be consumed by a
poll; it still delegates to `async_request_refresh()` so the debouncer's
execute lock keeps serialising refreshes. It is cancelled on entry unload
(`async_shutdown`, which the coordinator registers via
`config_entry.async_on_unload`) so it can never fire through a closed client.

The cloud also applies its own cross-document cascades: lowering
`userSettings.maximumChargingCurrent` was observed to lower
`solarSettings.smartSolarStartingCurrent` to match, one-directionally — raising
the maximum again did not restore it. Nothing is sent for the second document;
the server rewrites it. This is why the post-write refresh must re-read *all*
settings documents rather than only the one written, and why no optimistic
state is set for the cascaded value: the clamping rule is uncharacterised, so
any predicted value would be a guess.

## Notes for contributors

The integration is intentionally a thin shell over [`aioratio`](https://github.com/aaearon/aioratio). If you find yourself reaching for `boto3`, `warrant`, or HTTP code inside this repo, that work belongs in the library, not here.

## License

MIT.

## Disclaimer

Unofficial. Use at your own risk; the underlying API is reverse-engineered from the official mobile app and is not contractually stable. No affiliation with Ratio.
