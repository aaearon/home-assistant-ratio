# Changelog

All notable changes to this project will be documented in this file.

## [0.10.0] — 2026-05-13

### Added

- **Optional Bluetooth (BLE) support** via the Inspiro IPC GATT protocol. BLE is disabled by default; existing installations are unaffected.
  - When a charger is Bluetooth-bonded with the HA host and BLE is enabled for it via the integration options, three new sensor groups appear per charger: per-phase **voltage** (V), per-phase **current** (A), and **BLE protocol version** — all sourced from the `GetChargerSensorValues` BLE command and all unavailable from the cloud API.
  - BLE sensors go **unavailable** when the charger is out of range or the phone app preempts the GATT link; existing cloud sensors are unaffected.
  - Bond loss (e.g. after a charger factory reset) surfaces as a **Repair issue** with re-bonding instructions.
  - New service: **`ratio.reconfigure_wifi`** (`device_id`, `ssid`, `password?`) — reconnects the charger to a different Wi-Fi SSID via BLE without the official app.
  - Bluetooth discovery: HA automatically discovers Ratio chargers in range (`RATIO_P*` advertisement) and prompts to enable BLE for chargers already in your cloud account.
  - Options flow: enable/disable BLE per charger post-setup via **Settings → Devices & Services → Ratio EV Charging → Configure**.
  - BLE coordinator state (address, last poll success, last error) included in the diagnostics download.
- **`aioratio[ble]==0.10.0`** pinned — installs `bleak` as a transitive dependency only when BLE is used.

### Changed

- `manifest.json`: bumped `version` to `0.10.0`, updated `aioratio` requirement to `aioratio[ble]==0.10.0`, added `bluetooth` dependency and Bluetooth discovery matcher (`local_name: "RATIO_P*"`, `manufacturer_id: 3071`).

## [0.9.1] — prior release

See git history.
