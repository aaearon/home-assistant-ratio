"""Constants for the Ratio EV Charging integration."""

from __future__ import annotations

import math
from typing import Any

DOMAIN = "ratio"
DEFAULT_SCAN_INTERVAL = 60  # seconds

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_BLE_ENABLED_SERIALS = "ble_enabled_serials"
CONF_BLE_ADDRESSES = "ble_addresses"
CONF_BLE_POLL_PERIODS = "ble_poll_periods"
"""Options key for per-charger BLE poll periods. Value: ``dict[str, float]``
mapping charger serial → poll period in seconds."""

DEFAULT_BLE_POLL_PERIOD_S = 3.0
BLE_POLL_PERIOD_MIN_S = 1.0
BLE_POLL_PERIOD_MAX_S = 60.0


def valid_poll_period(value: Any) -> float:
    """Return ``value`` as a float if it is a finite number within the allowed
    range, otherwise fall back to ``DEFAULT_BLE_POLL_PERIOD_S``.

    Guards against corrupt or hand-edited stored values (None, non-numeric,
    zero, negative, or out of range) that would otherwise busy-loop or crash
    the BLE poller.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_BLE_POLL_PERIOD_S
    number = float(value)
    if not math.isfinite(number):
        return DEFAULT_BLE_POLL_PERIOD_S
    if not BLE_POLL_PERIOD_MIN_S <= number <= BLE_POLL_PERIOD_MAX_S:
        return DEFAULT_BLE_POLL_PERIOD_S
    return number


SERVICE_START_CHARGE = "start_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_ADD_VEHICLE = "add_vehicle"
SERVICE_REMOVE_VEHICLE = "remove_vehicle"
SERVICE_IMPORT_SESSION_HISTORY = "import_session_history"
SERVICE_RECONFIGURE_WIFI = "reconfigure_wifi"
SERVICE_BLE_PROBE = "ble_probe"

ATTR_CHARGER_ID = "charger_id"
ATTR_VEHICLE_ID = "vehicle_id"
ATTR_VEHICLE_NAME = "vehicle_name"
ATTR_LICENSE_PLATE = "license_plate"
ATTR_BEGIN_TIME = "begin_time"
ATTR_END_TIME = "end_time"
ATTR_SLOTS = "slots"

PLATFORMS: list[str] = [
    "sensor",
    "binary_sensor",
    "button",
    "switch",
    "select",
    "number",
    "text",
]

# Persistent storage for coordinator-level user preferences (preferred vehicle).
STORAGE_VERSION = 1
STORAGE_KEY_PREFERENCES = "preferences"

# ChargingState values that represent an *active charge request* — the user
# wants the car to be charging and the EVSE is honoring it (even if momentarily
# paused by the EVSE itself, e.g. for load-balancing). Used by `switch.charging`
# (start/stop control) and `binary_sensor.charging` (live "is charging" truth).
# Distinct from the raw `isChargeSessionActive` flag, which stays True through
# the post-stop VehicleDetected window while the cable remains plugged in.
ACTIVE_CHARGING_STATES = frozenset(
    {"Charging", "ChargingWithVentilation", "PausedByEVSE"}
)
