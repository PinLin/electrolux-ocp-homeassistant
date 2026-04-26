"""Constants for the Electrolux integration."""

from __future__ import annotations

from datetime import timedelta
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD

DOMAIN = "electrolux"

CONF_ACCESS_TOKEN = "access_token"
CONF_API_BASE_URL = "api_base_url"
CONF_API_KEY = "api_key"
CONF_COUNTRY_CODE = "country_code"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_WS_BASE_URL = "ws_base_url"

DEFAULT_API_BASE_URL = "https://api.ocp.electrolux.one"
DEFAULT_WEBSOCKET_BASE_URL = "wss://ws.eu.ocp.electrolux.one"
DEFAULT_REQUEST_TIMEOUT = timedelta(seconds=30)

# Polling cadence is fixed (not user-configurable). WebSocket pushes realtime
# state updates, so the scheduled poll is a housekeeping watchdog: it refreshes
# the access token (12 h TTL) before expiry, fetches capabilities once,
# resyncs the appliance list, and recovers WS if it died silently. 30 minutes
# is slow enough not to burden OCP and fast enough to refresh tokens well
# before the 5-minute pre-expiry buffer kicks in.
POLL_INTERVAL = timedelta(minutes=30)

# OCP rejects requests from unknown clients; we mimic the official Android
# app's Ktor user agent so the gateway treats us like the OneApp.
USER_AGENT = "Ktor client"

# Sensitive fields stripped from diagnostic dumps. Covers tokens / auth
# material plus account and appliance identifiers that could be used to
# correlate the user or their hardware if a diagnostic file is shared.
# Matching is exact and case-sensitive (HA's async_redact_data behaviour),
# so common case variants from the OCP payloads are listed explicitly.
REDACT_KEYS: set[str] = {
    # tokens / credentials
    CONF_ACCESS_TOKEN,
    CONF_API_KEY,
    CONF_REFRESH_TOKEN,
    CONF_PASSWORD,
    CONF_USERNAME,
    "access_token",
    "apiKey",
    "authorization",
    "Authorization",
    "originalCookie",
    "signedCookieBase64",
    "certificateRawDataBase64",
    "appliancePasswordHash",
    "refresh_token",
    "password",
    # account PII
    "email",
    "Email",
    "userName",
    "username",
    "user_name",
    "loginID",
    # appliance / hardware identifiers
    "applianceId",
    "appliance_id",
    "applianceName",
    "macAddress",
    "MacAddress",
    "macaddress",
    "MAC",
    "serialNumber",
    "sn",
    "SN",
    "DeviceId",
    "deviceId",
    "PNC",
    "pnc",
}
