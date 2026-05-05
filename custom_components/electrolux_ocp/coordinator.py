"""Coordinator for Electrolux API data."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any

from aiohttp import WSMsgType, WSServerHandshakeError
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    ElectroluxApiClient,
    ElectroluxApiError,
    ElectroluxAuthError,
    ElectroluxRateLimitError,
    ElectroluxRefreshThrottled,
    extract_appliance_id,
)
from .capabilities import (
    CapabilitiesProvider,
    CapabilityDict,
    build_default_provider,
)
from .const import (
    CONF_API_BASE_URL,
    CONF_WS_BASE_URL,
    DOMAIN,
    NEW_APPLIANCE_SIGNAL,
    POLL_INTERVAL,
)
from .models import ElectroluxConfigEntry, ElectroluxData

WS_BACKOFF_SECONDS = 30
WS_RECONNECT_DELAY = 5
WS_RATE_LIMIT_BACKOFF_SECONDS = 300  # 5 min when OCP says we're refreshing too fast
WS_NO_DATA_RETRY_SECONDS = 5
# Treat the access token as "fresh" if its remaining lifetime exceeds this.
# WS 401/403 while the token is fresh almost certainly is not an auth issue,
# so we must not refresh-bomb OCP into 429.
WS_TOKEN_FRESH_SECONDS = 1500  # 25 min
# Threshold for surfacing a repair issue. With a 30 min poll cadence this
# means the cloud has been unreachable for ~90 minutes before the user gets
# a notification — long enough to filter transient network blips, short
# enough that an actually broken integration doesn't go unnoticed.
POLLING_FAILURE_THRESHOLD = 3
ISSUE_POLLING_FAILING = "polling_failing"

LOGGER = logging.getLogger(__name__)


class ElectroluxDataUpdateCoordinator(DataUpdateCoordinator[ElectroluxData]):
    """Coordinate Electrolux API fetches and WebSocket updates."""

    config_entry: ElectroluxConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client: ElectroluxApiClient,
        entry: ElectroluxConfigEntry,
        capabilities_provider: CapabilitiesProvider | None = None,
    ) -> None:
        super().__init__(
            hass,
            logger=LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=POLL_INTERVAL,
            config_entry=entry,
            # WS pushes can be very chatty (one metric → one set_updated_data).
            # Skip listener fan-out when the new data object is == previous,
            # which is what happens when an OCP heartbeat ships unchanged values.
            always_update=False,
        )
        self._client = client
        self._ws_task: asyncio.Task[None] | None = None
        self._ws_active = False
        # Provider chain decides where capability data comes from for each
        # appliance: OCP first, hand-curated PURE A9 fallback second.
        self._capabilities_provider = capabilities_provider or build_default_provider(client)
        # Capabilities are static per appliance — cache forever once resolved.
        # An empty dict in the cache means "no provider claimed this appliance"
        # and is the signal for entity layers to use their built-in fallbacks.
        self._capabilities_cache: dict[str, CapabilityDict] = {}
        # Manually-tracked timestamp of the last successful refresh.
        # DataUpdateCoordinator only exposes a bool last_update_success.
        self.last_success_at: datetime | None = None
        # Appliance IDs we've already dispatched a NEW_APPLIANCE_SIGNAL for.
        # Seeded after the first successful refresh so the initial fleet
        # doesn't double-fire (platforms will pick those up directly from
        # coordinator.data in their async_setup_entry).
        self._dispatched_appliance_ids: set[str] = set()
        # Consecutive polling failures, used to gate the polling_failing
        # repair issue. Reset on every successful refresh.
        self._consecutive_failures: int = 0
        # Atomic persistence: every time the client applies a new token
        # response we immediately write it to the entry. Removes the class of
        # bugs where a refreshed token was lost in memory and the next call
        # used the (now-revoked) old refresh_token, producing cas_3412.
        client.set_on_token_update(self._persist_tokens_now)

    @property
    def client(self) -> ElectroluxApiClient:
        """Expose the API client (read-only) for diagnostic entities."""
        return self._client

    def get_capabilities(self, appliance_id: str) -> CapabilityDict | None:
        """Return cached capabilities for an appliance, or None if not fetched."""
        return self._capabilities_cache.get(appliance_id)

    async def _fetch_missing_capabilities(
        self, appliances: list[dict[str, Any]]
    ) -> None:
        """Populate the capabilities cache; remember failures to stop retrying."""
        for appliance in appliances:
            aid = extract_appliance_id(appliance)
            if not aid or aid in self._capabilities_cache:
                continue  # cache hit (success or sentinel-on-failure)
            caps = await self._capabilities_provider.async_fetch(appliance)
            # Cache the result regardless — empty dict means "no provider in
            # the chain claimed this appliance"; we won't retry next cycle
            # because capabilities are static per appliance.
            self._capabilities_cache[aid] = caps or {}
            if caps:
                LOGGER.debug("Loaded capabilities for %s (%d entries)", aid, len(caps))
            else:
                LOGGER.info(
                    "No capabilities source matched %s; entity layer will use built-in fallbacks",
                    aid,
                )

    def get_appliance(self, appliance_id: str) -> dict[str, Any] | None:
        """O(1) appliance lookup keyed by id, derived from coordinator data.

        The cache is keyed by the identity of `self.data`, so it stays in
        sync with both polling refreshes and WebSocket-driven
        async_set_updated_data calls without explicit invalidation.
        """
        if not self.data:
            return None
        cache = getattr(self, "_appliance_index_cache", None)
        if cache is None or cache[0] is not self.data:
            index = {
                extract_appliance_id(a): a
                for a in self.data.appliances
                if extract_appliance_id(a)
            }
            self._appliance_index_cache = (self.data, index)
            return index.get(appliance_id)
        result: dict[str, Any] | None = cache[1].get(appliance_id)
        return result

    def _persist_tokens_now(self) -> None:
        """Write current tokens to the config entry unconditionally."""
        new_data = dict(self.config_entry.data)
        new_data["access_token"] = self._client.access_token
        new_data["refresh_token"] = self._client.refresh_token
        new_data[CONF_API_BASE_URL] = self._client.api_base_url
        if self._client.ws_base_url:
            new_data[CONF_WS_BASE_URL] = self._client.ws_base_url
        self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

    def _persist_regional_config(self) -> None:
        """Write regional URLs back to the config entry (one-shot migration)."""
        new_data = dict(self.config_entry.data)
        changed = False
        if self._client.api_base_url and new_data.get(CONF_API_BASE_URL) != self._client.api_base_url:
            new_data[CONF_API_BASE_URL] = self._client.api_base_url
            changed = True
        if self._client.ws_base_url and new_data.get(CONF_WS_BASE_URL) != self._client.ws_base_url:
            new_data[CONF_WS_BASE_URL] = self._client.ws_base_url
            changed = True
        if changed:
            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)

    async def _async_update_data(self) -> ElectroluxData:
        """Fetch appliance data from Electrolux."""
        try:
            data = await self._do_update_data()
        except UpdateFailed:
            self._note_polling_failure()
            raise
        else:
            self._note_polling_success()
            return data

    async def _do_update_data(self) -> ElectroluxData:
        """The actual fetch; wrapped above for repair-issue book-keeping."""
        try:
            await self._client.async_ensure_valid_token()
        except ElectroluxAuthError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN,
                translation_key="invalid_auth",
                translation_placeholders={"reason": str(err)},
            ) from err

        # One-shot migration for entries created before regional URLs were cached.
        if not self._client.ws_base_url:
            try:
                if await self._client.async_ensure_regional_config():
                    self._persist_regional_config()
            except ElectroluxApiError:
                pass

        try:
            appliances = await self._client.async_get_appliances()
        except ElectroluxAuthError:
            # Expiry unknown or server rejected early — fall back to reactive refresh.
            try:
                await self._client.async_refresh_token()
                appliances = await self._client.async_get_appliances()
            except ElectroluxRefreshThrottled as err:
                # Cooldown blocked the refresh; back off one cycle.
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="refresh_throttled",
                    translation_placeholders={"reason": str(err)},
                ) from err
            except ElectroluxRateLimitError as err:
                # OCP throttled us; coordinator will retry next interval.
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="rate_limited",
                    translation_placeholders={"reason": str(err)},
                ) from err
            except ElectroluxAuthError as err:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN,
                    translation_key="invalid_auth",
                    translation_placeholders={"reason": str(err)},
                ) from err
            except ElectroluxApiError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="cannot_connect",
                    translation_placeholders={"reason": str(err)},
                ) from err
        except ElectroluxApiError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"reason": str(err)},
            ) from err

        try:
            user = await self._client.async_get_current_user()
        except ElectroluxApiError:
            user = None

        await self._fetch_missing_capabilities(list(appliances))

        # Drop device-registry entries for appliances no longer on the
        # account. Runs every successful poll so removals propagate within
        # one cycle, not just at setup time.
        self._async_cleanup_stale_devices(list(appliances))

        # Tell platforms about appliances that weren't there last time.
        # Skipped on the very first refresh because platforms haven't
        # subscribed yet — they read coordinator.data directly during their
        # async_setup_entry and seed entities from that.
        self._async_announce_new_appliances(list(appliances))

        # NOTE: WS task lifecycle is NOT tied to polling cadence — the task is
        # started once at setup time and self-heals via its own reconnect
        # loop. Polling here only refreshes REST state, token health, and
        # the appliance list.

        self.last_success_at = dt_util.utcnow()

        return ElectroluxData(
            appliances=list(appliances),
            user=dict(user) if isinstance(user, dict) else None,
            capabilities=dict(self._capabilities_cache),
        )

    def start_websocket(self) -> None:
        """Start the WebSocket background task (idempotent).

        The task runs its own reconnect/backoff loop, so a single call at
        setup is enough — no need for polling to re-arm it.
        """
        if self._ws_active or (self._ws_task and not self._ws_task.done()):
            return
        # Flip the flag BEFORE scheduling so a concurrent caller doesn't double-schedule.
        self._ws_active = True
        self._ws_task = self.hass.async_create_background_task(
            self._async_websocket_loop(), "electrolux_ocp_ws"
        )

    async def _async_websocket_loop(self) -> None:
        """Maintain a WebSocket connection for real-time updates.

        Single-cycle guarantees: at most ONE token refresh attempt per
        connection cycle, and never refresh while the access token is still
        fresh. This is what kept us out of the cas_3404 storm.

        Resync-on-reconnect: every successful (re)connect — except the first
        one right after setup, which the config-entry first refresh already
        covered — schedules a non-blocking REST refresh so any state that
        drifted during the silent gap is healed.
        """
        first_connect = True
        try:
            while self._ws_active:
                appliance_ids: list[str] = []
                if self.data is not None:
                    for app in self.data.appliances:
                        aid = extract_appliance_id(app)
                        if aid:
                            appliance_ids.append(aid)

                if not appliance_ids:
                    LOGGER.debug("WebSocket: no appliances yet, waiting %ss", WS_NO_DATA_RETRY_SECONDS)
                    await asyncio.sleep(WS_NO_DATA_RETRY_SECONDS)
                    continue

                refreshed_this_cycle = False
                LOGGER.info("Connecting to Electrolux WebSocket (%s)", self._client.ws_base_url)
                try:
                    async with self._client.ws_connect(appliance_ids) as ws:
                        LOGGER.info("Electrolux WebSocket connected")
                        if not first_connect:
                            # Non-blocking — must not delay reading WS msgs.
                            LOGGER.debug("WebSocket reconnected; scheduling REST resync")
                            self.hass.async_create_task(
                                self.async_request_refresh(),
                                name="electrolux_ocp_ws_resync",
                            )
                        first_connect = False
                        async for msg in ws:
                            if msg.type == WSMsgType.TEXT:
                                await self._handle_ws_message(msg.json())
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                                break
                except asyncio.CancelledError:
                    raise
                except WSServerHandshakeError as err:
                    backoff = await self._handle_ws_handshake_error(err, refreshed_this_cycle)
                    if backoff == 0:
                        refreshed_this_cycle = True
                        continue
                    await asyncio.sleep(backoff)
                except Exception as err:  # noqa: BLE001
                    LOGGER.warning("WebSocket error, retrying in %ss: %s", WS_BACKOFF_SECONDS, err)
                    await asyncio.sleep(WS_BACKOFF_SECONDS)
                else:
                    if self._ws_active:
                        LOGGER.info("WebSocket closed, reconnecting...")
                        await asyncio.sleep(WS_RECONNECT_DELAY)
        except asyncio.CancelledError:
            LOGGER.debug("Electrolux WebSocket task cancelled")
            raise
        finally:
            self._ws_active = False

    async def _handle_ws_handshake_error(
        self, err: WSServerHandshakeError, already_refreshed: bool
    ) -> float:
        """Decide what to do when WS handshake fails.

        Returns the number of seconds to back off before reconnecting, or 0
        if the caller should reconnect immediately (token was just refreshed).
        """
        if err.status not in (401, 403):
            LOGGER.warning(
                "WebSocket handshake error (%s), retrying in %ss",
                err.status, WS_BACKOFF_SECONDS,
            )
            return WS_BACKOFF_SECONDS

        if already_refreshed:
            LOGGER.warning(
                "WebSocket auth failed (%s) again after refresh — likely not a "
                "token issue; backing off %ss",
                err.status, WS_BACKOFF_SECONDS,
            )
            return WS_BACKOFF_SECONDS

        # Heuristic: if the token still has lots of life left, the WS rejection
        # is almost certainly NOT an auth issue. Refreshing now would be both
        # pointless and likely to hit OCP's refresh rate-limit (cas_3404).
        expires_at = self._client.access_token_expires_at
        if expires_at is not None and (expires_at - time.time()) > WS_TOKEN_FRESH_SECONDS:
            LOGGER.warning(
                "WebSocket auth failed (%s) but access token is fresh "
                "(%.0fs left); not refreshing, backing off %ss",
                err.status, expires_at - time.time(), WS_BACKOFF_SECONDS,
            )
            return WS_BACKOFF_SECONDS

        LOGGER.info("WebSocket auth failed (%s); refreshing access token", err.status)
        try:
            await self._client.async_refresh_token()
        except ElectroluxRefreshThrottled as refresh_err:
            LOGGER.warning(
                "WebSocket refresh skipped (%s); backing off %ss",
                refresh_err, WS_BACKOFF_SECONDS,
            )
            return WS_BACKOFF_SECONDS
        except ElectroluxRateLimitError as refresh_err:
            LOGGER.warning(
                "WebSocket refresh rate-limited by OCP (%s); backing off %ss",
                refresh_err, WS_RATE_LIMIT_BACKOFF_SECONDS,
            )
            return WS_RATE_LIMIT_BACKOFF_SECONDS
        except ElectroluxAuthError as refresh_err:
            LOGGER.error(
                "WebSocket token refresh failed; stopping WS until reauth: %s",
                refresh_err,
            )
            self._ws_active = False
            return WS_BACKOFF_SECONDS
        return 0  # immediate reconnect with the freshly refreshed token

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        """Apply incoming WebSocket state deltas to local appliance data.

        OCP WS message shape (mirrors py-electrolux-ocp):
            { "ConnectionId": ..., "Api": ..., "Version": ...,
              "Payload": { "Appliances": [
                  { "ApplianceId": "<id>",
                    "Metrics": [
                        { "Name": "PM2_5", "Value": 12, "Timestamp": "..." },
                        ...
                    ] }
              ]}}
        Each Metric maps directly to a key in `properties.reported`.
        """
        if not self.data:
            return
        payload = data.get("Payload") or {}
        updates = payload.get("Appliances") or []
        if not updates:
            LOGGER.debug("WebSocket message with no Appliances payload: %s", data)
            return

        existing_appliances = self.data.appliances
        updates_by_id: dict[str, dict[str, Any]] = {
            u["ApplianceId"]: u for u in updates if u.get("ApplianceId")
        }

        merged: list[dict[str, Any]] = []
        changed = False
        for appliance in existing_appliances:
            aid = extract_appliance_id(appliance)
            update = updates_by_id.get(aid) if aid else None
            if not update or not update.get("Metrics"):
                merged.append(appliance)
                continue

            props = dict(appliance.get("properties") or {})
            reported = dict(props.get("reported") or {})
            for metric in update["Metrics"]:
                name = metric.get("Name")
                if name is None:
                    continue
                reported[name] = metric.get("Value")
            props["reported"] = reported
            merged.append({**appliance, "properties": props})
            changed = True
            LOGGER.debug(
                "WebSocket update applied to %s: %d metric(s)",
                aid, len(update["Metrics"]),
            )

        if changed:
            self.async_set_updated_data(
                ElectroluxData(
                    appliances=merged,
                    user=self.data.user,
                    capabilities=self.data.capabilities,
                )
            )

    def _note_polling_failure(self) -> None:
        """Track failures and surface a repair issue once over the threshold."""
        self._consecutive_failures += 1
        if self._consecutive_failures < POLLING_FAILURE_THRESHOLD:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"{ISSUE_POLLING_FAILING}_{self.config_entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_POLLING_FAILING,
            translation_placeholders={"failures": str(self._consecutive_failures)},
        )

    def _note_polling_success(self) -> None:
        """Reset the failure counter and clear any active polling issue."""
        if self._consecutive_failures >= POLLING_FAILURE_THRESHOLD:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_POLLING_FAILING}_{self.config_entry.entry_id}",
            )
        self._consecutive_failures = 0

    def _async_announce_new_appliances(self, appliances: list[dict[str, Any]]) -> None:
        """Dispatch NEW_APPLIANCE_SIGNAL for each appliance ID never seen before."""
        current_ids: set[str] = {
            aid
            for a in appliances
            if (aid := extract_appliance_id(a)) is not None
        }
        first_refresh = not self._dispatched_appliance_ids and self.data is None
        new_ids = current_ids - self._dispatched_appliance_ids
        self._dispatched_appliance_ids = current_ids
        if first_refresh or not new_ids:
            return
        appliance_by_id = {extract_appliance_id(a): a for a in appliances}
        for aid in new_ids:
            appliance = appliance_by_id.get(aid)
            if appliance is None:
                continue
            async_dispatcher_send(
                self.hass,
                f"{NEW_APPLIANCE_SIGNAL}_{self.config_entry.entry_id}",
                appliance,
            )

    def _async_cleanup_stale_devices(self, appliances: list[dict[str, Any]]) -> None:
        """Remove device-registry entries for appliances no longer on the account."""
        known_ids = {
            extract_appliance_id(a)
            for a in appliances
            if extract_appliance_id(a)
        }
        device_reg = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(device_reg, self.config_entry.entry_id):
            owned_appliance_id: str | None = None
            for domain, identifier in device.identifiers:
                if domain == DOMAIN:
                    owned_appliance_id = identifier
                    break
            if owned_appliance_id is None:
                continue
            if owned_appliance_id not in known_ids:
                LOGGER.info(
                    "Removing stale device %s (%s) — not present on account anymore",
                    device.name or device.id,
                    owned_appliance_id,
                )
                device_reg.async_remove_device(device.id)

    async def async_stop_websocket(self) -> None:
        """Stop the WebSocket task and wait for it to finish."""
        self._ws_active = False
        task = self._ws_task
        self._ws_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
