"""Token-lifecycle tests: JWT `exp` fallback and refresh-request auth headers.

Background: after a restart / entry reload, `_access_token_expires_at` is an
in-memory-only value. If it is never seeded, `async_ensure_valid_token`
never proactively refreshes and the integration only recovers via a
reactive 401 refresh — which can itself be rejected by the server. Since
the access token is a JWT, its own `exp` claim can be used to recover an
expiry timestamp without any new dependency (no signature verification;
this is the client reading its own token, not a trust boundary).

Separately, `async_refresh_token` must not send the (possibly already
expired) old access token as a Bearer credential on the refresh call
itself — the refresh endpoint authenticates via the refresh token in the
body, not the access token.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.electrolux_ocp.api import (
    ElectroluxApiClient,
    ElectroluxAuthError,
    _decode_jwt_exp,
)


def _b64url(segment: bytes) -> str:
    return base64.urlsafe_b64encode(segment).decode("ascii").rstrip("=")


def _make_jwt(payload: dict, *, header: dict | None = None) -> str:
    """Build an unsigned JWT-shaped string carrying `payload` as its body."""
    header_segment = _b64url(json.dumps(header or {"alg": "none", "typ": "JWT"}).encode("utf-8"))
    payload_segment = _b64url(json.dumps(payload).encode("utf-8"))
    return f"{header_segment}.{payload_segment}.fake-signature"


def _client(**kwargs) -> ElectroluxApiClient:
    kwargs.setdefault("session", MagicMock())
    kwargs.setdefault("api_base_url", "https://test.local")
    return ElectroluxApiClient(**kwargs)


class TestDecodeJwtExp:
    """`_decode_jwt_exp` must fail safe (return None) on anything malformed."""

    def test_valid_jwt_returns_exp_as_float(self):
        exp = time.time() + 3600
        token = _make_jwt({"exp": exp, "sub": "user-1"})
        assert _decode_jwt_exp(token) == pytest.approx(exp)

    def test_exp_as_int_is_coerced_to_float(self):
        exp_int = int(time.time()) + 3600
        token = _make_jwt({"exp": exp_int})
        result = _decode_jwt_exp(token)
        assert isinstance(result, float)
        assert result == pytest.approx(float(exp_int))

    @pytest.mark.parametrize(
        "bad_token",
        [
            "not-a-jwt-at-all",
            "only.two-segments",
            "a.b.c.d",
            "",
            "!!!not-base64!!!.!!!not-base64!!!.sig",
        ],
        ids=[
            "no-dots",
            "two-segments",
            "four-segments",
            "empty-string",
            "invalid-base64",
        ],
    )
    def test_malformed_shape_returns_none(self, bad_token: str):
        assert _decode_jwt_exp(bad_token) is None

    def test_payload_not_json_returns_none(self):
        header_segment = _b64url(b'{"alg":"none"}')
        payload_segment = _b64url(b"not-json-at-all")
        token = f"{header_segment}.{payload_segment}.sig"
        assert _decode_jwt_exp(token) is None

    def test_payload_missing_exp_returns_none(self):
        token = _make_jwt({"sub": "user-1"})
        assert _decode_jwt_exp(token) is None

    def test_exp_as_string_returns_none(self):
        token = _make_jwt({"exp": "not-a-number"})
        assert _decode_jwt_exp(token) is None

    def test_exp_as_bool_returns_none(self):
        # bool is a subclass of int in Python; guard against True/False
        # masquerading as a valid exp timestamp.
        token = _make_jwt({"exp": True})
        assert _decode_jwt_exp(token) is None

    def test_decode_jwt_exp_handles_base64url_specific_encoding(self):
        """Regression test for a base64/base64url mutant that survives every
        other test in this module: swapping `base64.urlsafe_b64decode` for
        `base64.b64decode` in `_decode_jwt_payload` only breaks decoding when
        the payload segment's encoding actually differs between the two
        alphabets -- i.e. contains a '-' or '_' where standard base64 would
        have emitted '+' or '/'. No other payload built in this file happens
        to trigger that difference, so that swap previously passed the full
        suite unnoticed.

        We search a small, deterministic space of payloads (a non-ASCII
        character shifted across the three possible byte alignments via
        padding) for one whose base64url segment contains '-'/'_', so the
        test doesn't depend on the current wall-clock `exp` value's digit
        width.
        """
        exp = time.time() + 3600
        payload_segment = None
        for pad_len in range(0, 6):
            candidate_payload = {"exp": exp, "note": "a" * pad_len + "¾"}
            candidate_json = json.dumps(
                candidate_payload, separators=(",", ":"), ensure_ascii=False
            )
            candidate_segment = _b64url(candidate_json.encode("utf-8"))
            if "-" in candidate_segment or "_" in candidate_segment:
                payload_segment = candidate_segment
                break

        assert payload_segment is not None, (
            "could not find a payload whose base64url segment contains "
            "'-' or '_' within the searched padding range"
        )
        # The whole point of this test: confirm the segment we're about to
        # feed in actually exercises a base64url-specific character. Without
        # this assertion the test would silently be a no-op against the
        # `b64decode` mutant.
        assert "-" in payload_segment or "_" in payload_segment

        header_segment = _b64url(json.dumps({"alg": "none"}).encode("utf-8"))
        token = f"{header_segment}.{payload_segment}.fake-signature"

        assert _decode_jwt_exp(token) == pytest.approx(exp)

    def test_none_token_returns_none(self):
        # The `str | None` signature is not just decorative: the constructor
        # and _apply_token_response's JWT-fallback path both pass whatever
        # self._access_token currently holds, which type-checks as
        # `str | None`.
        assert _decode_jwt_exp(None) is None

    @pytest.mark.parametrize(
        "bad_token",
        [
            # 2 segments, but the payload segment alone is valid base64 JSON
            # with a valid `exp` -- only the length check can reject this.
            "{header}.{payload}",
            # 4 segments, same trick.
            "{header}.{payload}.sig.extra",
        ],
    )
    def test_wrong_segment_count_with_otherwise_valid_payload_returns_none(
        self, bad_token: str
    ):
        payload_segment = _b64url(
            json.dumps({"exp": time.time() + 3600}).encode("utf-8")
        )
        token = bad_token.format(header="header", payload=payload_segment)
        assert _decode_jwt_exp(token) is None


class TestConstructorSeedsExpiry:
    def test_valid_jwt_access_token_seeds_expires_at(self):
        exp = time.time() + 3600
        token = _make_jwt({"exp": exp})
        client = _client(access_token=token)
        assert client.access_token_expires_at == pytest.approx(exp)

    def test_malformed_access_token_leaves_expires_at_none(self):
        client = _client(access_token="not-a-jwt")
        assert client.access_token_expires_at is None

    def test_no_access_token_leaves_expires_at_none(self):
        client = _client()
        assert client.access_token_expires_at is None

    def test_constructor_never_raises_on_garbage_token(self):
        # This path runs during integration setup; raising here would break
        # config entry setup entirely.
        try:
            _client(access_token="\x00\x01garbage.\xffmore-garbage.sig")
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"constructor raised on malformed token: {exc!r}")


class TestApplyTokenResponseFallback:
    def test_expires_in_present_takes_precedence_over_jwt_exp(self):
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 999999  # deliberately different from expiresIn
        token = _make_jwt({"exp": jwt_exp})
        before = time.time()
        client._apply_token_response(
            {"accessToken": token, "refreshToken": "ref-1", "expiresIn": 3600}
        )
        after = time.time()
        # Should be seeded from expiresIn (~now+3600), not the JWT's exp.
        assert before + 3600 <= client.access_token_expires_at <= after + 3600

    def test_missing_expires_in_falls_back_to_jwt_exp(self):
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 4321
        token = _make_jwt({"exp": jwt_exp})
        client._apply_token_response({"accessToken": token, "refreshToken": "ref-1"})
        assert client.access_token_expires_at == pytest.approx(jwt_exp)

    def test_invalid_expires_in_falls_back_to_jwt_exp(self):
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 4321
        token = _make_jwt({"exp": jwt_exp})
        client._apply_token_response(
            {"accessToken": token, "refreshToken": "ref-1", "expiresIn": "garbage"}
        )
        assert client.access_token_expires_at == pytest.approx(jwt_exp)

    def test_missing_expires_in_and_non_jwt_access_token_gives_none(self):
        client = _client(refresh_token="ref-0")
        client._apply_token_response({"accessToken": "opaque-token", "refreshToken": "ref-1"})
        assert client.access_token_expires_at is None


class TestExpiryRangeGuard:
    """Regression tests for the sensor.py native_value crash hazard.

    `sensor.py`'s `native_value` feeds `access_token_expires_at` straight
    into `datetime.fromtimestamp()`. If that value is non-finite (`inf`/
    `nan`) or absurdly out of range (e.g. a millisecond-epoch value treated
    as seconds), `fromtimestamp()` raises -- and since that runs inside a
    coordinator listener callback with no surrounding try/except, the
    exception escapes `async_update_listeners()` and silently drops every
    other entity's update for that refresh cycle. Both computation paths
    (JWT `exp` and the `expiresIn` response field) must be guarded.
    """

    def test_native_value_hazard_documented(self):
        # Motivation record, not a test of our own code: demonstrates the
        # crash this range guard exists to prevent from ever reaching
        # sensor.py's native_value.
        with pytest.raises((OverflowError, OSError, ValueError)):
            datetime.fromtimestamp(float("inf"), tz=timezone.utc)
        with pytest.raises((OverflowError, OSError, ValueError)):
            datetime.fromtimestamp(float("nan"), tz=timezone.utc)

    @pytest.mark.parametrize(
        "bad_exp",
        [float("inf"), float("-inf"), float("nan")],
        ids=["inf", "neg-inf", "nan"],
    )
    def test_decode_jwt_exp_rejects_non_finite(self, bad_exp: float):
        token = _make_jwt({"exp": bad_exp})
        assert _decode_jwt_exp(token) is None

    def test_decode_jwt_exp_rejects_millisecond_scale_exp(self):
        # A plausible unit mixup: `exp` given in milliseconds, ~1000x the
        # sane range regardless of current wall-clock time.
        ms_exp = time.time() * 1000
        token = _make_jwt({"exp": ms_exp})
        assert _decode_jwt_exp(token) is None

    def test_decode_jwt_exp_accepts_far_future_but_sane_exp(self):
        # Guard against an overly aggressive range check: a legitimately
        # long-lived token a few years out must still pass.
        exp = time.time() + 3 * 365 * 24 * 3600  # 3 years
        token = _make_jwt({"exp": exp})
        assert _decode_jwt_exp(token) == pytest.approx(exp)

    def test_apply_token_response_infinite_expires_in_falls_back_to_jwt_exp(self):
        # expiresIn path: float("1e400") overflows to inf with no exception,
        # so it must be caught by the range guard and treated the same as a
        # missing/invalid expiresIn -- falling back to the JWT's own exp.
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 4321
        token = _make_jwt({"exp": jwt_exp})
        client._apply_token_response(
            {"accessToken": token, "refreshToken": "ref-1", "expiresIn": float("1e400")}
        )
        assert client.access_token_expires_at == pytest.approx(jwt_exp)

    def test_apply_token_response_infinite_expires_in_and_no_jwt_exp_gives_none(self):
        # Both paths poisoned/unavailable: must fail safe to None, never inf.
        client = _client(refresh_token="ref-0")
        client._apply_token_response(
            {
                "accessToken": "opaque-token",
                "refreshToken": "ref-1",
                "expiresIn": float("1e400"),
            }
        )
        assert client.access_token_expires_at is None

    def test_apply_token_response_finite_but_absurd_expires_in_falls_back_to_jwt_exp(self):
        # Realistic real-world trigger for this guard, distinct from the
        # inf/nan cases above: a token lifetime accidentally reported in
        # milliseconds instead of seconds. A 30-day lifetime
        # (2_592_000_000 ms) misread as seconds is ~82 years out -- finite
        # (so `math.isfinite` alone would not catch it), but comfortably
        # over `_MAX_TOKEN_LIFETIME_SECONDS` (10 years), so
        # `_sanitize_expires_at`'s upper-bound check must reject it.
        #
        # Note this guard only fires once the misread value is actually
        # implausible: a *short-lived* token (e.g. a 12h/43200s lifetime
        # misread as 43_200_000) lands at ~500 days out, which is still
        # under the 10-year cap and is accepted as-is -- this test
        # intentionally uses a long-lived-token example so the mixup
        # crosses that threshold.
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 4321
        token = _make_jwt({"exp": jwt_exp})
        client._apply_token_response(
            {"accessToken": token, "refreshToken": "ref-1", "expiresIn": 2_592_000_000}
        )
        assert client.access_token_expires_at == pytest.approx(jwt_exp)


class TestExpiresInNonPositiveShortCircuitsBeforeRangeGuard:
    """Covers `expiresIn` values that never reach `_sanitize_expires_at`'s
    range guard at all: `_apply_token_response` gates the whole `expiresIn`
    path with `if expires_in_f and expires_in_f > 0`, and `nan > 0` is
    `False` in Python, so `nan` is filtered out by that short circuit, not
    by the range guard. The end result (fall back to the JWT `exp`) looks
    identical to `TestExpiryRangeGuard`'s cases, but for a different reason
    -- kept in its own class so a reader doesn't mistake this for a
    range-guard regression test.
    """

    def test_apply_token_response_nan_expires_in_falls_back_to_jwt_exp(self):
        client = _client(refresh_token="ref-0")
        jwt_exp = time.time() + 4321
        token = _make_jwt({"exp": jwt_exp})
        client._apply_token_response(
            {"accessToken": token, "refreshToken": "ref-1", "expiresIn": float("nan")}
        )
        assert client.access_token_expires_at == pytest.approx(jwt_exp)


class TestAsyncLoginIdTokenDecode:
    """`async_login`'s id_token decode must reuse `_decode_jwt_payload` and
    surface `ElectroluxAuthError` on malformed input rather than leaking a
    raw binascii/JSONDecodeError/IndexError (low-severity finding: this
    decode used to be hand-rolled separately from `_decode_jwt_exp`)."""

    @staticmethod
    def _queue_login_responses(client: ElectroluxApiClient, id_token: str, *, final: dict | None = None) -> None:
        session_secret = base64.b64encode(b"a-session-secret").decode("ascii")
        responses = [
            {"accessToken": "client-token"},
            [{"domain": "example.com", "apiKey": "apikey"}],
            {"gmid": "g-1", "ucid": "u-1"},
            {
                "sessionInfo": {
                    "sessionToken": "session-token",
                    "sessionSecret": session_secret,
                }
            },
            {"id_token": id_token},
        ]
        if final is not None:
            responses.append(final)
        client._request = AsyncMock(side_effect=responses)  # type: ignore[method-assign]

    @pytest.mark.asyncio
    async def test_malformed_id_token_raises_auth_error_not_raw_exception(self):
        client = _client(email="user@example.com", password="pw", country_code="TW")
        self._queue_login_responses(client, "not-a-valid-jwt-shape")

        with pytest.raises(ElectroluxAuthError):
            await client.async_login()

    @pytest.mark.asyncio
    async def test_valid_id_token_login_still_succeeds(self):
        # Behaviour-preservation check for reusing _decode_jwt_payload:
        # a well-formed id_token must still let login complete normally.
        id_token = _make_jwt({"country": "TW"})
        final_token = _make_jwt({"exp": time.time() + 3600})
        client = _client(email="user@example.com", password="pw", country_code="TW")
        self._queue_login_responses(
            client,
            id_token,
            final={
                "accessToken": final_token,
                "refreshToken": "final-refresh",
                "expiresIn": 3600,
            },
        )

        await client.async_login()

        assert client.access_token == final_token
        assert client.refresh_token == "final-refresh"


class TestEnsureValidTokenUsesRecoveredExpiry:
    @pytest.mark.asyncio
    async def test_soon_to_expire_jwt_triggers_proactive_refresh(self):
        # Within the 300s default buffer -> must refresh proactively, purely
        # from the JWT-derived expiry (never persisted, never set explicitly).
        token = _make_jwt({"exp": time.time() + 100})
        client = _client(access_token=token, refresh_token="ref-0")

        refreshed = False

        async def fake_refresh():
            nonlocal refreshed
            refreshed = True
            client._access_token = _make_jwt({"exp": time.time() + 3600})
            client._access_token_expires_at = time.time() + 3600

        client.async_refresh_token = fake_refresh  # type: ignore[method-assign]

        result = await client.async_ensure_valid_token()

        assert refreshed is True
        assert result is True

    @pytest.mark.asyncio
    async def test_fresh_jwt_does_not_trigger_refresh(self):
        token = _make_jwt({"exp": time.time() + 999999})
        client = _client(access_token=token, refresh_token="ref-0")

        async def fake_refresh():
            pytest.fail("refresh should not have been called for a fresh token")

        client.async_refresh_token = fake_refresh  # type: ignore[method-assign]

        result = await client.async_ensure_valid_token()

        assert result is False


class _FakeResponse:
    def __init__(self, status: int, text: str = "", content_type: str = "application/json"):
        self.status = status
        self._text = text
        self.headers = {"Content-Type": content_type}

    async def text(self) -> str:
        return self._text


class _FakeRequestCM:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *args) -> bool:
        return False


class _RecordingSession:
    """Fake aiohttp session that records the headers of the last request."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_headers: dict | None = None
        self.call_count = 0

    def request(self, method, url, **kwargs):
        self.call_count += 1
        self.last_headers = kwargs.get("headers")
        return _FakeRequestCM(self._response)


class TestRefreshOmitsAuthorizationHeader:
    @pytest.mark.asyncio
    async def test_refresh_request_has_no_authorization_header(self):
        new_token = _make_jwt({"exp": time.time() + 3600})
        response = _FakeResponse(
            200,
            text=json.dumps(
                {"accessToken": new_token, "refreshToken": "ref-1", "expiresIn": 3600}
            ),
        )
        session = _RecordingSession(response)
        client = _client(
            session=session,
            access_token="stale-possibly-expired-access-token",
            refresh_token="ref-0",
            api_key="key",
        )

        await client.async_refresh_token()

        assert session.call_count == 1
        assert session.last_headers is not None
        assert "Authorization" not in session.last_headers

    @pytest.mark.asyncio
    async def test_ordinary_request_still_has_authorization_header(self):
        # Guard the other direction: skip_auth must be opt-in, not the new
        # default for every call.
        response = _FakeResponse(200, text=json.dumps({"ok": True}))
        session = _RecordingSession(response)
        client = _client(session=session, access_token="valid-access-token")

        await client._request("GET", "/some/path")

        assert session.last_headers is not None
        assert session.last_headers.get("Authorization") == "Bearer valid-access-token"
