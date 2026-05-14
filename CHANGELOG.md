# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

- Internal: tighten pyright surface to zero errors. Removed lazy `# type: ignore` suppressions in `number.py`, `services.py`, and `test_history_coordinator.py` by adopting structural fixes (literal-kwarg `dataclasses.replace`, widened `ServiceResponse` value, assert-then-use pattern). Entity property overrides remain `@property` (matching `CoordinatorEntity`'s dynamic semantics) with documented per-site `# pyright: ignore[reportIncompatibleVariableOverride]` suppressions to bridge the HA `Entity`/`CoordinatorEntity` base-class inconsistency. Test files using HA TypedDicts (`ConfigFlowResult`, `StatisticData`) use a small `_r`/`_sd` cast helper to satisfy `reportTypedDictNotRequiredAccess`.

## [0.10.0] — 2026-05-13

> ⚠️ **BLE feature is included but real-hardware validation is pending community reports.** Discovery/config flow and unit tests are green; the live GATT poll path against a charger has not been fully verified by the maintainer. Existing cloud-only installs are unaffected.

### Added

- **Optional Bluetooth (BLE) support** via the Inspiro IPC GATT protocol. BLE is disabled by default; existing installations are unaffected.
  - When a charger is Bluetooth-bonded with the HA host and BLE is enabled for it via the integration options, three new sensor groups appear per charger: per-phase **voltage** (V), per-phase **current** (A), and **BLE protocol version** — all sourced from the `GetChargerSensorValues` BLE command and all unavailable from the cloud API.
  - BLE sensors go **unavailable** when the charger is out of range or the phone app preempts the GATT link; existing cloud sensors are unaffected.
  - Bond loss (e.g. after a charger factory reset) surfaces as a **Repair issue** with re-bonding instructions.
  - New service: **`ratio.reconfigure_wifi`** (`device_id`, `ssid`, `password?`) — reconnects the charger to a different Wi-Fi SSID via BLE without the official app.
  - Bluetooth discovery: HA automatically discovers Ratio chargers in range (`RATIO_*` advertisement) and prompts to enable BLE for chargers already in your cloud account.
  - Options flow: enable/disable BLE per charger post-setup via **Settings → Devices & Services → Ratio EV Charging → Configure**.
  - BLE coordinator state (address, last poll success, last error) included in the diagnostics download.
- **`aioratio[ble]==0.10.1`** pinned — installs `bleak` + `bleak-retry-connector` as transitive dependencies only when BLE is used.

### Changed

- `manifest.json`: bumped `version` to `0.10.0`, updated `aioratio` requirement to `aioratio[ble]==0.10.1`, added `bluetooth` dependency and Bluetooth discovery matcher (`local_name: "RATIO_*"`, `manufacturer_id: 3071`).

## [0.9.1] — prior release

See git history.
