"""Constants for the Ratio EV Charging integration."""

from __future__ import annotations

DOMAIN = "ratio"
DEFAULT_SCAN_INTERVAL = 60  # seconds

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

SERVICE_START_CHARGE = "start_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_SCHEDULE = "set_schedule"
SERVICE_ADD_VEHICLE = "add_vehicle"
SERVICE_REMOVE_VEHICLE = "remove_vehicle"
SERVICE_IMPORT_SESSION_HISTORY = "import_session_history"

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
