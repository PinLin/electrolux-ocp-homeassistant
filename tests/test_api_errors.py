"""Error class hierarchy tests.

The catch order in `__init__.py async_setup_entry` and `coordinator.py
_async_update_data` relies on subclass relationships between the API
error types — if those break, exceptions get silently miscategorised
(e.g. a rate-limited login becomes ConfigEntryAuthFailed and triggers
spurious reauth flows).
"""

from __future__ import annotations

from custom_components.electrolux_ocp.api import (
    ElectroluxApiError,
    ElectroluxAuthError,
    ElectroluxRateLimitError,
    ElectroluxRefreshThrottled,
)


class TestErrorHierarchy:
    """Catch-order assumptions encoded as tests."""

    def test_auth_error_extends_api_error(self):
        # async_setup_entry catches ElectroluxAuthError before ElectroluxApiError;
        # if AuthError is no longer a subclass, the second except is unreachable.
        assert issubclass(ElectroluxAuthError, ElectroluxApiError)

    def test_rate_limit_extends_api_error_but_not_auth(self):
        # Rate-limit errors must NOT be caught by ElectroluxAuthError handlers
        # (they would trigger ConfigEntryAuthFailed → reauth flow). They must
        # be caught by ElectroluxApiError handlers → ConfigEntryNotReady.
        assert issubclass(ElectroluxRateLimitError, ElectroluxApiError)
        assert not issubclass(ElectroluxRateLimitError, ElectroluxAuthError)

    def test_refresh_throttled_extends_api_error_but_not_auth(self):
        # Same constraint as RateLimit — local cooldown is not an auth issue.
        assert issubclass(ElectroluxRefreshThrottled, ElectroluxApiError)
        assert not issubclass(ElectroluxRefreshThrottled, ElectroluxAuthError)

    def test_rate_limit_and_refresh_throttled_are_distinct(self):
        # WS handshake handler treats them differently — RateLimit gets a
        # 5-min OCP backoff, RefreshThrottled gets a normal backoff.
        assert ElectroluxRateLimitError is not ElectroluxRefreshThrottled
        assert not issubclass(ElectroluxRateLimitError, ElectroluxRefreshThrottled)
        assert not issubclass(ElectroluxRefreshThrottled, ElectroluxRateLimitError)
