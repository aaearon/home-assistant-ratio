# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Changed

- **BLE poll cadence tuned from 3 s → 1 s.** Verified live: per-poll
  round-trip ≈ 1.3 s through the ESPHome BT proxy on charger
  `P00000000013428` with no observed retries. Faster than the official
  Ratio app's 3 s cadence — easier real-time tracking for load-balancing
  and solar surplus automations. If a charger's firmware turns out to
  rate-limit IPC requests at a lower bound, revert this constant.
- **BLE sensors now update every ~1 s instead of every 1–5 minutes.** The
  coordinator no longer connects/pairs/disconnects per poll; it now holds a
  single `BleClient` open continuously and consumes
  `aioratio.BleClient.poll_sensor_values(period=3.0)` — matching the cadence
  the official Ratio app uses (decompiled APK
  `ChargerInformationRepository.java:236` → `POLL_TIME.DEFAULT_BLE = 3s`).
  Each yield pushes a fresh `BleSnapshot` to `self.data` and notifies
  listeners. Combined with a stable `local_name`-keyed advert subscription,
  reconnects fire reliably even when the chosen scanner is an ESPHome
  Bluetooth proxy (which routes adverts under rotating RPAs the parent
  class's address-keyed callback can never resolve). Pins `aioratio[ble]==0.11.0`.
- The `ratio.reconfigure_wifi` service now re-uses the session loop's live
  `BleClient` when one is open, falling back to a one-shot connect only when
  the session is in its reconnect backoff. aioratio's transaction mutex
  serializes the Wi-Fi command against the running 3 s poll.

### Fixed

- **BLE poll fails on every cycle when connecting via an ESPHome BT proxy.** The proxy keeps its `is_paired_` flag per-connection and resets it on every disconnect, so the previous `_try_pair` (which paired on a *separate* `BleakClient` and then dropped the connection) succeeded but did not propagate to the next read — the proxy still issued GATT ops with `ESP_GATT_AUTH_REQ_NONE` and the charger rejected them with `status=15` (Insufficient encryption). Fixed in `aioratio==0.10.2` by moving the pair-and-retry into `BleakBleTransport` so it runs on the same `BleakClient` connection as the read. This integration now pins `aioratio[ble]==0.10.2` and the now-redundant `_try_pair` helper + `RatioBleNotBondedError` retry branch are removed from `RatioBleCoordinator._async_update`. A `RatioBleNotBondedError` reaching the coordinator after the upgrade means aioratio's pair-and-retry itself failed (charger rejected SMP, proxy lacks the `PAIRING` feature flag, etc.) — the repair issue still fires.

## [0.10.2] — 2026-05-15

### Fixed

- **BLE pairing failures were silently swallowed ([#27](https://github.com/aaearon/home-assistant-ratio/issues/27)).** `_try_pair` previously caught all exceptions and returned `False` without logging, so an upgrade-needed `NotImplementedError` from an ESPHome BT proxy was indistinguishable from a transient adapter glitch. The helper now (a) narrows the catch to `BleakError`/`TimeoutError`/`OSError` and logs the concrete exception class + message at `WARNING`, and (b) emits a firmware hint on `NotImplementedError` ("ESPHome BT proxies need firmware 2024.6+ with `bluetooth_proxy: active: true`"). A new `_scanner_info` helper tags every pairing-related log with the concrete scanner class, source, and a `proxy=` flag (using HA's public `BaseHaRemoteScanner` marker), giving reports of BLE-via-proxy issues enough signal to classify the failure mode (firmware-too-old vs. transient adapter glitch vs. wrong scanner routing).
- **Last-session sensors stuck on stale values after a long charging session ([#26](https://github.com/aaearon/home-assistant-ratio/issues/26)).** During a charge, repeated empty `session_history` polls advanced `_last_imported_end_time` to "now", shrinking the next poll's fetch window to the last hour. A session whose begin was more than `HISTORY_OVERLAP_SECONDS` (1 h) before unplug — typical for overnight charges — fell outside that window and was never imported. `_last_imported_end_time` is now anchored only to actual imported session ends, with a separate `_empty_poll_watermark` field handling the "how far back to look" optimization for chargers that haven't yet produced any completed session (so brand-new chargers don't repeat the full 30-day backfill on every poll). Existing affected installs are self-healed on next load: a drifted persisted cursor is clamped back to the latest persisted session end, so the next poll's overlap window catches the missed session(s) without requiring users to remove and re-add the integration.

### Internal

- Tighten pyright surface to zero errors. Removed lazy `# type: ignore` suppressions in `number.py`, `services.py`, and `test_history_coordinator.py` by adopting structural fixes (literal-kwarg `dataclasses.replace`, widened `ServiceResponse` value, assert-then-use pattern). Entity property overrides remain `@property` (matching `CoordinatorEntity`'s dynamic semantics) with documented per-site `# pyright: ignore[reportIncompatibleVariableOverride]` suppressions to bridge the HA `Entity`/`CoordinatorEntity` base-class inconsistency. Test files using HA TypedDicts (`ConfigFlowResult`, `StatisticData`) use a small `_r`/`_sd` cast helper to satisfy `reportTypedDictNotRequiredAccess`.

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
