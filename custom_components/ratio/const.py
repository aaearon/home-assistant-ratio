"""Constants for the Ratio EV Charging integration."""
from __future__ import annotations

DOMAIN = "ratio"
DEFAULT_SCAN_INTERVAL = 60  # seconds

CONF_EMAIL = "email"
CONF_PASSWORD = "password"

SERVICE_START_CHARGE = "start_charge"
SERVICE_STOP_CHARGE = "stop_charge"
SERVICE_SET_SCHEDULE = "set_schedule"

ATTR_CHARGER_ID = "charger_id"
ATTR_VEHICLE_ID = "vehicle_id"
ATTR_SLOTS = "slots"

PLATFORMS: list[str] = ["sensor", "binary_sensor", "switch", "select"]
