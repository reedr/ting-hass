"""Constants for the Ting integration."""

from __future__ import annotations

DOMAIN = "ting"

CONF_REFRESH_TOKEN = "refresh_token"

PLATFORMS = ["binary_sensor", "sensor"]

COGNITO_REGION = "us-east-1"
COGNITO_USER_POOL_ID = "us-east-1_trW4gH661"
COGNITO_CLIENT_ID = "4akjeqt9gtl8rgg1cksunipk9u"
COGNITO_ENDPOINT = f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"

TING_API_BASE = "https://api.wskr.io"
TING_SIGNALR_WS_URL = "wss://signalr.api.wskr.io/dataHub"
TING_SIGNALR_HUB = "dataHub"
TING_COMBO_BINARY_DATA = "ComboBinaryData"

ATTR_VOLTAGE_HI = "voltage_high"
ATTR_VOLTAGE_LO = "voltage_low"
ATTR_HIFI = "hifi"
ATTR_LAST_UPDATE = "last_update"

# --- interval statistics options (config entry options) ---------------------
# Ting sends about four realtime samples per second. Home Assistant's state
# machine and recorder do not benefit from that display-oriented frequency, but
# publishing only the latest sample every few seconds hides short sags and
# swells.  Each window is summarised as mean / min / max instead (aggregator.py).
CONF_INTERVAL = "interval"
CONF_FAST_INTERVAL = "fast_interval"
CONF_BAND_LOW = "band_low"
CONF_BAND_HIGH = "band_high"
CONF_HOLD = "hold"

# Normal publish interval, seconds, aligned to the wall clock.
DEFAULT_INTERVAL = 60
# Publish interval while the voltage is out of band, seconds.
DEFAULT_FAST_INTERVAL = 5
# ANSI C84.1 Range A service voltage for a 120 V nominal system (+/-5 %).
DEFAULT_BAND_LOW = 114.0
DEFAULT_BAND_HIGH = 126.0
# Seconds back inside the band (less hysteresis) before fast mode ends.
DEFAULT_HOLD = 60

# Recovery must be this far inside the band edge, so a voltage hovering right
# at a limit does not flap between modes.
BAND_HYSTERESIS = 1.0

EVENT_VOLTAGE_EXCURSION = "ting_voltage_excursion"
