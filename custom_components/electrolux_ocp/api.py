"""Async client for the Electrolux OCP API."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import random
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from .const import DEFAULT_REQUEST_TIMEOUT, DEFAULT_WEBSOCKET_BASE_URL, USER_AGENT

_LOGGER = logging.getLogger(__name__)

# OCP rejects refresh requests that arrive within ~30s of the previous one
# (cas_3404 "Too frequent refresh token request"). Add headroom on top.
MIN_REFRESH_INTERVAL_SECONDS = 60


class ElectroluxApiError(Exception):
    """Base API error."""


class ElectroluxAuthError(ElectroluxApiError):
    """Authentication or authorization failed."""


class ElectroluxRefreshThrottled(ElectroluxApiError):
    """Refresh request was skipped locally to respect OCP rate limits."""


class ElectroluxRateLimitError(ElectroluxApiError):
    """OCP returned 429 / cas_3404; back off before retrying."""


@dataclass(slots=True)
class IdentityProviderConfig:
    """Regional identity provider settings returned by One Account."""

    http_regional_base_url: str | None
    web_socket_regional_base_url: str | None


def _normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def _get_path_value(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def _first_value(payload: Mapping[str, Any], paths: Sequence[str | Sequence[str]]) -> Any:
    for path in paths:
        value = _get_path_value(payload, (path,)) if isinstance(path, str) else _get_path_value(payload, path)
        if value not in (None, ""):
            return value
    return None


def _looks_like_appliance(item: Any) -> bool:
    return isinstance(item, Mapping) and any(
        key in item
        for key in (
            "applianceId",
            "applianceName",
            "applianceType",
            "pnc",
            "serialNumber",
            "sn",
        )
    )


def _find_appliance_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, Mapping):
        return []

    direct = payload.get("appliances")
    if isinstance(direct, list):
        return [item for item in direct if isinstance(item, dict)]

    for value in payload.values():
        if isinstance(value, list) and any(_looks_like_appliance(item) for item in value):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, Mapping):
            nested = _find_appliance_list(value)
            if nested:
                return nested

    return []


def _find_metadata_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, Mapping):
        return []

    metadata = payload.get("metaDataResult")
    if isinstance(metadata, list):
        return [item for item in metadata if isinstance(item, dict)]

    for value in payload.values():
        if isinstance(value, Mapping):
            nested = _find_metadata_list(value)
            if nested:
                return nested

    return []


def _attach_appliance_metadata(
    payload: Any, appliances: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    metadata_by_pnc = {
        str(item["pnc"]): item
        for item in _find_metadata_list(payload)
        if item.get("pnc") not in (None, "")
    }
    if not metadata_by_pnc:
        return appliances

    merged: list[dict[str, Any]] = []
    for appliance in appliances:
        pnc = _first_value(appliance, ("pnc", ("applianceInfo", "pnc")))
        metadata = metadata_by_pnc.get(str(pnc)) if pnc not in (None, "") else None
        if metadata is None:
            merged.append(appliance)
            continue

        enriched = dict(appliance)
        if isinstance(metadata.get("applianceInfoResult"), Mapping):
            enriched["applianceInfo"] = dict(metadata["applianceInfoResult"])
        if isinstance(metadata.get("productCardResult"), Mapping):
            enriched["productCard"] = dict(metadata["productCardResult"])
        merged.append(enriched)

    return merged


def extract_appliance_id(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "applianceId",
            "id",
            "cloudId",
            ("applianceData", "applianceId"),
            ("applianceInfo", "applianceId"),
            ("metadata", "applianceId"),
        ),
    )
    return str(value) if value is not None else None


def extract_appliance_name(appliance: Mapping[str, Any]) -> str:
    value = _first_value(
        appliance,
        (
            "applianceName",
            "name",
            ("applianceData", "applianceName"),
            ("applianceInfo", "name"),
            ("metadata", "name"),
        ),
    )
    appliance_id = extract_appliance_id(appliance)
    if value is None:
        return appliance_id or "Electrolux appliance"
    return str(value)


def extract_appliance_type(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "applianceType",
            "type",
            "deviceType",
            ("applianceData", "applianceType"),
            ("applianceInfo", "deviceType"),
            ("metadata", "type"),
            ("product", "type"),
        ),
    )
    return str(value) if value is not None else None


def extract_appliance_manufacturer(appliance: Mapping[str, Any]) -> str:
    value = _first_value(
        appliance,
        (
            "manufacturer",
            "brand",
            ("applianceInfo", "brand"),
            ("product", "brand"),
            ("metadata", "brand"),
        ),
    )
    if value is None:
        return "Electrolux"
    brand = str(value)
    return brand if brand.isupper() and len(brand) <= 4 else brand.title()


def extract_appliance_model(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "model",
            "modelName",
            ("applianceData", "modelName"),
            ("applianceInfo", "model"),
            ("product", "model"),
            ("metadata", "model"),
            "pnc",
        ),
    )
    return str(value) if value is not None else None


def extract_appliance_model_id(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "pnc",
            "PNC",
            ("applianceInfo", "pnc"),
            ("product", "pnc"),
            ("metadata", "pnc"),
        ),
    )
    if value is not None:
        return str(value)

    appliance_id = extract_appliance_id(appliance)
    if appliance_id and len(appliance_id) >= 17 and appliance_id[:17].isdigit():
        return appliance_id[:9]

    return None


def extract_appliance_serial(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "serialNumber",
            "sn",
            ("applianceData", "serialNumber"),
            ("applianceData", "sn"),
            ("product", "serialNumber"),
            ("product", "sn"),
            ("metadata", "serialNumber"),
            ("metadata", "sn"),
        ),
    )
    if value is not None:
        return str(value)

    # OneApp's product-info screen derives the 8-digit serial from the product
    # identifier: <PNC><serial><suffix>. Example:
    # 111222333444455556667777 -> PNC 111222333, serial 44445555.
    pnc = extract_appliance_model_id(appliance)
    appliance_id = extract_appliance_id(appliance)
    if pnc and appliance_id and appliance_id.startswith(pnc):
        serial = appliance_id[len(pnc) : len(pnc) + 8]
        if len(serial) == 8 and serial.isdigit():
            return serial

    return None


def extract_firmware_version(appliance: Mapping[str, Any]) -> str | None:
    value = _first_value(
        appliance,
        (
            "firmwareVersion",
            "swVersion",
            ("metadata", "firmwareVersion"),
            ("applianceInfo", "firmwareVersion"),
        ),
    )
    return str(value) if value is not None else None


def extract_state_value(appliance: Mapping[str, Any]) -> str:
    online = _first_value(
        appliance,
        (
            "online",
            "connected",
            ("metadata", "online"),
            ("metadata", "connected"),
            "connectionState",
        ),
    )
    if isinstance(online, bool):
        return "online" if online else "offline"
    if str(online).lower() == "connected":
        return "online"
    if str(online).lower() == "disconnected":
        return "offline"

    value = _first_value(
        appliance,
        (
            "state",
            "status",
            ("metadata", "state"),
            ("metadata", "status"),
            "applianceType",
            "type",
        ),
    )
    if value is None:
        return "unknown"
    return str(value)


def summarize_appliance(appliance: Mapping[str, Any]) -> dict[str, Any]:
    """Return a small, stable summary for entities and diagnostics."""
    return {
        "appliance_id": extract_appliance_id(appliance),
        "name": extract_appliance_name(appliance),
        "type": extract_appliance_type(appliance),
        "manufacturer": extract_appliance_manufacturer(appliance),
        "model": extract_appliance_model(appliance),
        "model_id": extract_appliance_model_id(appliance),
        "serial_number": extract_appliance_serial(appliance),
        "firmware_version": extract_firmware_version(appliance),
        "state": extract_state_value(appliance),
        "keys": sorted(appliance.keys()),
    }


def _url_encode(value: Any) -> str:
    if isinstance(value, int):
        str_value = str(value)
    elif isinstance(value, (dict, list)):
        str_value = json.dumps(value, separators=(',', ':'))
    else:
        str_value = str(value)
    return urllib.parse.quote_plus(str_value.encode("utf-8")).replace("+", "%20").replace("%7E", "~")

def _get_oauth1_signature(secret_key: str, http_method: str, url: str, request_params: dict[str, Any]) -> str:
    u = urllib.parse.urlparse(url)
    hostname = (u.hostname or "").lower()
    normalized_url = f"https://{hostname}{u.path}"
    query_string = "&".join(f"{k}={_url_encode(request_params[k])}" for k in sorted(request_params.keys()) if request_params[k] is not None)
    base_string = f"{http_method.upper()}&{_url_encode(normalized_url)}&{_url_encode(query_string)}"
    raw_hmac = hmac.new(base64.b64decode(secret_key.encode("utf-8")), base_string.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(raw_hmac).decode("utf-8")


# Upper bound on how far into the future a computed expiry timestamp is
# allowed to sit. Guards against both a malformed/adversarial `exp` or
# `expiresIn` (Python's `json.loads` accepts the non-standard `Infinity`/
# `NaN` literals, and `float("1e400")` silently overflows to `inf` -- no
# exception raised) and a plausible unit mixup (e.g. a millisecond-epoch
# value mistaken for seconds, landing ~1000x too far out). Anything outside
# this range must fail safe to None: an unguarded `inf`/`nan` reaching
# datetime.fromtimestamp() in the token-expiry sensor's native_value raises
# OverflowError/ValueError, and that exception escapes
# DataUpdateCoordinator.async_update_listeners() uncaught, silently
# dropping every entity update scheduled after the sensor's in that cycle.
_MAX_TOKEN_LIFETIME_SECONDS = 10 * 365 * 24 * 3600  # 10 years


def _sanitize_expires_at(value: float | None) -> float | None:
    """Clamp a computed `_access_token_expires_at` candidate to a sane range.

    Applied at every site that assigns `_access_token_expires_at` (both the
    JWT-`exp` path and the `expiresIn` path), not just inside
    `_decode_jwt_exp`, so a future direct assignment can't reintroduce the
    hazard by skipping the JWT decoder.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        return None
    if value <= 0 or value > time.time() + _MAX_TOKEN_LIFETIME_SECONDS:
        return None
    return value


def _decode_jwt_payload(token: str | None) -> dict[str, Any] | None:
    """Best-effort base64/JSON decode of a JWT's payload segment.

    No signature verification is performed: callers use this to read claims
    out of a token the client itself already holds (its own access token or
    the id_token it just received from the identity provider during login),
    not to validate an externally supplied credential, so there is no trust
    boundary to enforce here.

    Fails safe by returning None on anything malformed rather than raising,
    so callers on setup-critical paths (client construction, login) don't
    have to guard against a handful of different decode exceptions.
    """
    try:
        parts = token.split(".") if token else []
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        return payload if isinstance(payload, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _decode_jwt_exp(token: str | None) -> float | None:
    """Best-effort extraction of the `exp` claim from a JWT access token.

    The access token is a JWT whose `exp` claim survives a restart even
    though `_access_token_expires_at` (in-memory only) does not. This lets a
    freshly constructed client recover a proactive-refresh deadline without
    persisting anything new.

    Observed shape (real OCP access token, checked 2026-07-22, not persisted
    here -- only its shape/magnitude): a standard 3-segment JWT whose `exp`
    (like `iat`) is a second-resolution Unix timestamp, with an observed
    token lifetime of 12 hours.

    Runs on the integration setup path (client construction) and after every
    token apply, so any malformed input -- including `token=None` -- must
    fail safe by returning None rather than raising -- an exception here
    would prevent the integration from starting.
    """
    payload = _decode_jwt_payload(token)
    if payload is None:
        return None
    exp = payload.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return _sanitize_expires_at(float(exp))


class ElectroluxApiClient:
    """Minimal Electrolux client based on app reverse-engineering."""

    def __init__(
        self,
        *,
        session: ClientSession,
        api_base_url: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        api_key: str | None = None,
        country_code: str | None = None,
        email: str | None = None,
        password: str | None = None,
        ws_base_url: str | None = None,
    ) -> None:
        self._session = session
        self._api_base_url = _normalize_base_url(api_base_url)
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._api_key = api_key or "2AMqwEV5MqVhTKrRCyYfVF8gmKrd2rAmp7cUsfky"
        self._country_code = country_code
        self._email = email
        self._password = password
        self._client_id = "ElxOneApp"
        self._client_secret = "8UKrsKD7jH9zvTV7rz5HeCLkit67Mmj68FvRVTlYygwJYy4dW6KF2cVLPKeWzUQUd6KJMtTifFf4NkDnjI7ZLdfnwcPtTSNtYvbP7OzEkmQD9IjhMOf5e1zeAQYtt2yN"
        # Seed the proactive-refresh deadline from the JWT itself so a fresh
        # client (post-restart / entry reload) doesn't have to wait for a
        # reactive 401 before it knows the token is about to expire.
        self._access_token_expires_at: float | None = (
            _decode_jwt_exp(access_token) if access_token else None
        )
        self._ws_base_url = _normalize_base_url(ws_base_url) if ws_base_url else None
        self._last_refresh_at: float | None = None
        self._last_refresh_status: str | None = None
        self._on_token_update: Callable[[], None] | None = None
        # Serialises token refreshes. Without it a REST poll and a WS handshake
        # recovery can both pass the cooldown check and fire two HTTP refreshes
        # with the same (possibly single-use) refresh token; the loser gets
        # invalid_grant → 401 → a spurious reauth.
        self._refresh_lock = asyncio.Lock()


    @property
    def api_base_url(self) -> str:
        return self._api_base_url

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        return self._refresh_token

    @property
    def access_token_expires_at(self) -> float | None:
        """Unix timestamp when the current access token expires, if known."""
        return self._access_token_expires_at

    @property
    def ws_base_url(self) -> str | None:
        """Regional WebSocket base URL (populated after login)."""
        return self._ws_base_url

    @property
    def session(self) -> ClientSession:
        """Expose underlying aiohttp session (for WebSocket use)."""
        return self._session

    @property
    def last_refresh_at(self) -> float | None:
        """Unix timestamp of the most recent token refresh, if any."""
        return self._last_refresh_at

    @property
    def last_refresh_status(self) -> str | None:
        """Last refresh result string ('ok' / 'rate_limited' / 'error: ...')."""
        return self._last_refresh_status

    def set_on_token_update(self, callback: Callable[[], None] | None) -> None:
        """Register a synchronous callback fired after every token apply.

        The callback should persist tokens immediately. Storing this hook on
        the client guarantees a fresh refresh_token is never lost between a
        successful refresh and the next API call (which is what produces
        cas_3412 'Invalid grant' down the line).
        """
        self._on_token_update = callback

    async def _request(
        self,
        method: str,
        path: str,
        *,
        base_url: str | None = None,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        skip_auth: bool = False,
    ) -> Any:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if self._access_token and not skip_auth:
            request_headers["Authorization"] = f"Bearer {self._access_token}"
        if self._api_key:
            request_headers["x-api-key"] = self._api_key
        if headers:
            request_headers.update(headers)

        url = f"{_normalize_base_url(base_url or self._api_base_url)}/{path.lstrip('/')}"

        try:
            async with self._session.request(
                method,
                url,
                params=params,
                json=json_body,
                data=data,
                headers=request_headers,
                timeout=ClientTimeout(total=DEFAULT_REQUEST_TIMEOUT.total_seconds()),
            ) as response:
                text = await response.text()

                # Map 429 (and OCP's cas_3404) uniformly to RateLimitError so
                # callers (config_flow setup, coordinator, WS loop) can map to
                # ConfigEntryNotReady / backoff instead of generic failure.
                # Checked before 401/403: OCP can wrap cas_3404 in an auth
                # status, and treating a throttle as credential failure would
                # escalate it into a spurious reauth prompt.
                if response.status == 429 or "cas_3404" in (text or ""):
                    raise ElectroluxRateLimitError(
                        f"{response.status}: {text or 'Rate limited by Electrolux'}"
                    )

                if response.status in (401, 403):
                    raise ElectroluxAuthError(f"{response.status}: {text or 'Authentication failed'}")

                if response.status >= 400:
                    raise ElectroluxApiError(f"{response.status}: {text or 'Electrolux API request failed'}")

                if not text:
                    return None

                content_type = response.headers.get("Content-Type", "")
                if "application/json" not in content_type and not text.lstrip().startswith(("{", "[")):
                    return text

                try:
                    return json.loads(text)
                except (TypeError, ValueError) as err:
                    raise ElectroluxApiError(f"Invalid JSON response from {url}") from err

        except ElectroluxApiError:
            raise
        except ClientError as err:
            raise ElectroluxApiError(f"Connection error while calling {url}") from err

    async def async_login(self) -> None:
        """Perform full authentication flow to obtain an access_token."""
        if not self._email or not self._password or not self._country_code:
            raise ElectroluxAuthError("Missing credentials for login")

        # 1. Fetch client token
        client_token_resp = await self._request(
            "POST",
            "/one-account-authorization/api/v1/token",
            json_body={
                "grantType": "client_credentials",
                "clientId": self._client_id,
                "clientSecret": self._client_secret,
                "idToken": None,
                "refreshToken": None,
                "scope": "",
            },
        )
        if not client_token_resp or "accessToken" not in client_token_resp:
            raise ElectroluxAuthError("Failed to get client credentials")
        
        client_token = client_token_resp["accessToken"]

        # 2. Fetch identity providers
        idp_data = await self._request(
            "GET",
            "/one-account-user/api/v1/identity-providers",
            params={"brand": "electrolux", "countryCode": self._country_code},
            headers={"Authorization": f"Bearer {client_token}", "Context-Brand": "electrolux"}
        )
        if not isinstance(idp_data, list) or not idp_data:
            raise ElectroluxAuthError("No identity providers found")
        
        provider = idp_data[0]
        gigya_domain = provider.get("domain")
        gigya_api_key = provider.get("apiKey")

        if provider.get("httpRegionalBaseUrl"):
            self._api_base_url = _normalize_base_url(provider["httpRegionalBaseUrl"])
        if provider.get("webSocketRegionalBaseUrl"):
            self._ws_base_url = _normalize_base_url(provider["webSocketRegionalBaseUrl"])

        if not gigya_domain or not gigya_api_key:
            raise ElectroluxAuthError("Identity provider missing domain or apiKey")

        # 3. Gigya getIDs
        nonce = f"{int(time.time() * 1000)}_{random.randrange(1000000000, 10000000000)}"
        ids_data = await self._request(
            "POST",
            "socialize.getIDs",
            base_url=f"https://socialize.{gigya_domain}",
            data={
                "apiKey": gigya_api_key,
                "format": "json",
                "httpStatusCodes": "true",
                "nonce": nonce,
                "sdk": "Android_6.2.1",
                "targetEnv": "mobile",
            }
        )
        if not ids_data:
            raise ElectroluxAuthError(
                "Gigya socialize.getIDs returned empty response (network/captcha?)"
            )
        if "gmid" not in ids_data:
            raise ElectroluxAuthError(f"Gigya socialize.getIDs missing gmid: {ids_data}")
        gmid = ids_data["gmid"]
        ucid = ids_data["ucid"]

        # 4. Gigya login
        login_data = await self._request(
            "POST",
            "accounts.login",
            base_url=f"https://accounts.{gigya_domain}",
            data={
                "apiKey": gigya_api_key,
                "format": "json",
                "gmid": gmid,
                "httpStatusCodes": "true",
                "loginID": self._email,
                "nonce": f"{int(time.time() * 1000)}_{random.randrange(1000000000, 10000000000)}",
                "password": self._password,
                "sdk": "Android_6.2.1",
                "targetEnv": "mobile",
                "ucid": ucid,
            }
        )
        if not login_data:
            raise ElectroluxAuthError(
                "Gigya accounts.login returned empty response (network/captcha?)"
            )
        if "sessionInfo" not in login_data:
            # Common shape on bad credentials: {"errorCode": 403042, "errorMessage": "Invalid credentials"}
            err_msg = login_data.get("errorMessage") or login_data.get("errorDetails") or str(login_data)
            raise ElectroluxAuthError(f"Gigya login failed: {err_msg}")
        session_token = login_data["sessionInfo"]["sessionToken"]
        session_secret = login_data["sessionInfo"]["sessionSecret"]

        # 5. Gigya getJWT
        url = f"https://accounts.{gigya_domain}/accounts.getJWT"
        jwt_params = {
            "apiKey": gigya_api_key,
            "fields": "country",
            "format": "json",
            "gmid": gmid,
            "httpStatusCodes": "true",
            "nonce": f"{int(time.time() * 1000)}_{random.randrange(1000000000, 10000000000)}",
            "oauth_token": session_token,
            "sdk": "Android_6.2.1",
            "targetEnv": "mobile",
            "timestamp": int(time.time()),
            "ucid": ucid,
        }
        jwt_params["sig"] = _get_oauth1_signature(session_secret, "POST", url, jwt_params)

        jwt_data = await self._request(
            "POST",
            "accounts.getJWT",
            base_url=f"https://accounts.{gigya_domain}",
            data=jwt_params
        )
        if not jwt_data:
            raise ElectroluxAuthError(
                "Gigya accounts.getJWT returned empty response (network/captcha?)"
            )
        if "id_token" not in jwt_data:
            err_msg = jwt_data.get("errorMessage") or jwt_data.get("errorDetails") or str(jwt_data)
            raise ElectroluxAuthError(f"Gigya getJWT failed: {err_msg}")
        id_token = jwt_data["id_token"]

        # 6. One Account token exchange
        decoded_jwt = _decode_jwt_payload(id_token)
        if decoded_jwt is None:
            # A malformed id_token here would otherwise leak a raw
            # binascii/JSONDecodeError/IndexError out of async_login instead
            # of the ElectroluxAuthError callers expect on login failure.
            raise ElectroluxAuthError("Gigya getJWT returned a malformed id_token")
        origin_country = decoded_jwt.get("country", self._country_code)

        token_data = await self._request(
            "POST",
            "/one-account-authorization/api/v1/token",
            base_url=self._api_base_url,
            json_body={
                "grantType": "urn:ietf:params:oauth:grant-type:token-exchange",
                "clientId": self._client_id,
                "idToken": id_token,
                "scope": "",
            },
            headers={"Origin-Country-Code": origin_country, "Authorization": "Bearer ", "x-api-key": self._api_key}
        )
        if not token_data or "accessToken" not in token_data:
            raise ElectroluxAuthError("Failed to exchange token")

        self._apply_token_response(token_data)


    async def async_refresh_token(self) -> None:
        """Refresh the access token using the refresh token.

        Raises ElectroluxRefreshThrottled if a refresh happened within the
        local cooldown window, ElectroluxRateLimitError on cas_3404, or
        ElectroluxAuthError on a real auth failure.
        """
        if not self._refresh_token:
            raise ElectroluxAuthError("No refresh token available")

        # Snapshot before contending for the lock so we can tell a concurrent
        # refresh (which completed while we waited) apart from a genuinely
        # too-frequent sequential call.
        refresh_at_on_entry = self._last_refresh_at

        async with self._refresh_lock:
            # Double-check under the lock: if another caller refreshed while we
            # were queued, adopt its result instead of firing a second HTTP
            # request with a refresh token that may already be rotated.
            if (
                self._last_refresh_at is not None
                and self._last_refresh_at != refresh_at_on_entry
                and self._last_refresh_status == "ok"
            ):
                return

            if self._last_refresh_at is not None:
                elapsed = time.time() - self._last_refresh_at
                if elapsed < MIN_REFRESH_INTERVAL_SECONDS:
                    self._last_refresh_status = "throttled"
                    raise ElectroluxRefreshThrottled(
                        f"Refresh skipped: last refresh was {elapsed:.0f}s ago "
                        f"(cooldown {MIN_REFRESH_INTERVAL_SECONDS}s)"
                    )

            try:
                token_data = await self._request(
                    "POST",
                    "/one-account-authorization/api/v1/token",
                    base_url=self._api_base_url,
                    json_body={
                        "grantType": "refresh_token",
                        "clientId": self._client_id,
                        "clientSecret": self._client_secret,
                        "refreshToken": self._refresh_token,
                        "scope": "",
                    },
                    headers={"x-api-key": self._api_key},
                    # The token endpoint rejects a request that carries an
                    # expired Bearer credential, even when the refreshToken in
                    # the body is still perfectly valid. Measured against
                    # api.ap.ocp.electrolux.one on 2026-07-30 with one
                    # 8-day-old refresh token, identical bodies, 5s apart:
                    # with the expired Authorization header -> HTTP 401 (empty
                    # body); without it -> HTTP 200 and a fresh token pair.
                    # Since _request() otherwise attaches Authorization
                    # unconditionally, and the access token being refreshed is
                    # usually expired (that's why we're here), leaving it on
                    # turned every post-expiry refresh into a hard auth
                    # failure -- the cause of the 2026-07-22 spurious reauth.
                    # pyelectroluxgroup, the reference implementation, likewise
                    # sends this request with skip_auth_headers=True.
                    skip_auth=True,
                )
            except ElectroluxApiError as err:
                text = str(err)
                if text.startswith("429") or "cas_3404" in text:
                    self._last_refresh_status = "rate_limited"
                    raise ElectroluxRateLimitError(text) from err
                if "invalid_grant" in text:
                    self._last_refresh_status = "error: invalid_grant"
                    raise ElectroluxAuthError(text) from err
                self._last_refresh_status = f"error: {text[:120]}"
                raise

            if not token_data or "accessToken" not in token_data:
                self._last_refresh_status = "error: malformed response"
                raise ElectroluxAuthError("Failed to refresh token")

            self._apply_token_response(token_data)

    def _apply_token_response(self, token_data: Mapping[str, Any]) -> None:
        """Store access/refresh tokens and compute expiry from the response."""
        self._access_token = token_data["accessToken"]

        # OCP rotates refresh tokens. Strict policy: if the response contains
        # a non-empty refreshToken, adopt it; otherwise keep the previous one
        # but log loudly so any drift is visible.
        if "refreshToken" in token_data:
            new_refresh = token_data.get("refreshToken")
            if new_refresh:
                if new_refresh != self._refresh_token:
                    _LOGGER.info("Electrolux refresh token rotated")
                self._refresh_token = new_refresh
            else:
                _LOGGER.warning(
                    "Token response had refreshToken=%r; keeping previous", new_refresh
                )
        # If no refreshToken key at all, OCP did not rotate; keep the existing.

        expires_in = token_data.get("expiresIn")
        try:
            expires_in_f = float(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            expires_in_f = None

        expires_at_from_expires_in: float | None = None
        if expires_in_f and expires_in_f > 0:
            expires_at_from_expires_in = _sanitize_expires_at(time.time() + expires_in_f)

        if expires_at_from_expires_in is not None:
            self._access_token_expires_at = expires_at_from_expires_in
        else:
            # expiresIn absent/invalid/out-of-range (see _sanitize_expires_at):
            # fall back to the new access token's own JWT `exp` claim rather
            # than giving up on proactive refresh entirely (see
            # _decode_jwt_exp docstring for why no signature check is needed
            # here). _decode_jwt_exp already applies the same range guard.
            self._access_token_expires_at = _decode_jwt_exp(self._access_token)

        self._last_refresh_at = time.time()
        self._last_refresh_status = "ok"
        if self._access_token_expires_at is None:
            _LOGGER.info("Electrolux token applied (expires_at=unknown)")
        else:
            _LOGGER.info(
                "Electrolux token applied (expires_at=%d, source=%s)",
                self._access_token_expires_at,
                "expiresIn" if expires_at_from_expires_in is not None else "jwt_exp",
            )

        if self._on_token_update is not None:
            try:
                self._on_token_update()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("on_token_update callback failed")

    async def async_ensure_valid_token(self, buffer_seconds: int = 300) -> bool:
        """Refresh the access token if missing or within buffer of expiry.

        Returns True if a refresh happened, False otherwise. Raises
        ElectroluxAuthError if no valid token can be produced.
        """
        if not self._access_token:
            raise ElectroluxAuthError("No access token available")

        expires_at = self._access_token_expires_at
        if expires_at is None:
            return False  # unknown expiry; rely on reactive refresh path

        if expires_at - time.time() > buffer_seconds:
            return False

        if not self._refresh_token:
            raise ElectroluxAuthError("Access token near expiry and no refresh token")

        try:
            await self.async_refresh_token()
        except ElectroluxRefreshThrottled:
            return False  # refreshed very recently; existing token still serves
        except ElectroluxRateLimitError:
            return False  # let caller retry on next poll cycle
        return True

    async def async_ensure_regional_config(self) -> bool:
        """Populate api_base_url / ws_base_url from identity-providers if missing.

        Returns True if any value changed.
        """
        if self._ws_base_url and self._api_base_url:
            return False
        if not self._country_code:
            return False
        try:
            providers = await self.async_get_identity_providers(self._country_code)
        except ElectroluxApiError:
            return False
        if not providers:
            return False
        p = providers[0]
        changed = False
        if not self._ws_base_url and p.web_socket_regional_base_url:
            self._ws_base_url = _normalize_base_url(p.web_socket_regional_base_url)
            changed = True
        if p.http_regional_base_url:
            new_api = _normalize_base_url(p.http_regional_base_url)
            if new_api != self._api_base_url:
                self._api_base_url = new_api
                changed = True
        return changed

    def ws_connect(
        self,
        appliance_ids: list[str],
        *,
        heartbeat: int = 300,
    ) -> Any:
        """Open an authenticated WebSocket connection.

        OCP's gateway is strict: the URL is the regional base URL **as-is**
        (no extra path) and only three headers are accepted. Sending extras
        like x-api-key, Sec-WebSocket-Protocol, or Origin makes it 403.
        Mirrors py-electrolux-ocp exactly.
        """
        url = (self._ws_base_url or DEFAULT_WEBSOCKET_BASE_URL).rstrip("/")
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "appliances": json.dumps(
                [{"applianceId": aid} for aid in appliance_ids]
            ),
            "version": "2",
        }
        return self._session.ws_connect(url, headers=headers, heartbeat=heartbeat)

    async def async_get_identity_providers(self, country_code: str) -> list[IdentityProviderConfig]:
        """Fetch regional identity provider settings."""
        # We need a client token first
        client_token_resp = await self._request(
            "POST",
            "/one-account-authorization/api/v1/token",
            json_body={
                "grantType": "client_credentials",
                "clientId": self._client_id,
                "clientSecret": self._client_secret,
                "idToken": None,
                "refreshToken": None,
                "scope": "",
            },
        )
        client_token = client_token_resp.get("accessToken")
        
        data = await self._request(
            "GET",
            "/one-account-user/api/v1/identity-providers",
            params={"brand": "electrolux", "countryCode": country_code},
            headers={"Authorization": f"Bearer {client_token}", "Context-Brand": "electrolux"}
        )
        
        providers = []
        if isinstance(data, list):
            for item in data:
                providers.append(IdentityProviderConfig(
                    http_regional_base_url=item.get("httpRegionalBaseUrl"),
                    web_socket_regional_base_url=item.get("webSocketRegionalBaseUrl"),
                ))
        return providers

    async def async_get_current_user(self) -> Mapping[str, Any] | None:
        """Return the current authenticated user."""
        data = await self._request("GET", "/one-account-user/api/v1/users/current")
        return data if isinstance(data, Mapping) else None

    async def async_get_appliances(self) -> list[dict[str, Any]]:
        """Return appliances using the v2 endpoint observed in the app."""
        data = await self._request(
            "GET",
            "/appliance/api/v2/appliances",
            params={"includeMetadata": "true"},
        )
        appliances = _find_appliance_list(data)
        return _attach_appliance_metadata(data, appliances)

    async def async_get_capabilities(self, appliance_id: str) -> Mapping[str, Any] | list[Any] | None:
        """Return capabilities for an appliance."""
        result = await self._request(
            "GET",
            f"/appliance/api/v2/appliances/{appliance_id}/capabilities",
        )
        if result is None or isinstance(result, (Mapping, list)):
            return result
        return None

    async def async_send_command(self, appliance_id: str, payload: Mapping[str, Any]) -> Any:
        """Send a raw capability command payload."""
        return await self._request(
            "PUT",
            f"/appliance/api/v2/appliances/{appliance_id}/command",
            json_body=payload,
            headers={"Content-Type": "application/json"},
        )
