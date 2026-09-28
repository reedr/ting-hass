"""Realtime coordinator for Ting."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .aggregator import AggregatorConfig, VoltageWindowAggregator
from .api import TingApi, TingDevice, extract_device_diagnostics, extract_notifications
from .auth import TingAuth
from .const import (
    BAND_HYSTERESIS,
    CONF_BAND_HIGH,
    CONF_BAND_LOW,
    CONF_FAST_INTERVAL,
    CONF_HOLD,
    CONF_INTERVAL,
    DEFAULT_BAND_HIGH,
    DEFAULT_BAND_LOW,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HOLD,
    DEFAULT_INTERVAL,
    EVENT_VOLTAGE_EXCURSION,
)
from .exceptions import TingAuthError, TingConnectionError, TingResponseError
from .signalr import TingSignalRClient

_LOGGER = logging.getLogger(__name__)


def aggregator_config(options: dict[str, Any]) -> AggregatorConfig:
    """Build the interval statistics settings from config entry options."""
    return AggregatorConfig(
        interval=float(options.get(CONF_INTERVAL, DEFAULT_INTERVAL)),
        fast_interval=float(options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)),
        band_low=float(options.get(CONF_BAND_LOW, DEFAULT_BAND_LOW)),
        band_high=float(options.get(CONF_BAND_HIGH, DEFAULT_BAND_HIGH)),
        hold=float(options.get(CONF_HOLD, DEFAULT_HOLD)),
        hysteresis=BAND_HYSTERESIS,
    )


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class TingRealtimeCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Push coordinator for one Ting device."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, auth: TingAuth, device: TingDevice
    ) -> None:
        super().__init__(hass, _LOGGER, name=f"Ting {device.serial_number}")
        self._entry = entry
        self.device = device
        self._client = TingSignalRClient(
            auth,
            station_id=device.serial_number,
            callback=self._async_handle_update,
            stale_callback=self._async_handle_stale,
        )
        self.aggregator_config = aggregator_config(dict(entry.options))
        self._aggregator = VoltageWindowAggregator(
            self.aggregator_config,
            self.async_set_updated_data,
            self._async_handle_excursion,
        )
        # No data yet: leave entities unavailable until the stream delivers.
        self.last_update_success = False

    async def async_start(self) -> None:
        """Start the realtime stream."""
        try:
            await self._client.async_run()
        except TingAuthError as err:
            await self._async_handle_stale(err)
            self._entry.async_start_reauth(self.hass)

    async def async_stop(self) -> None:
        """Stop the realtime stream."""
        try:
            await self._client.async_stop()
        finally:
            self._aggregator.cancel()

    async def _async_handle_update(self, data: dict[str, Any]) -> None:
        self._aggregator.submit(data)

    def _async_handle_excursion(self, summary: dict[str, Any]) -> None:
        """Report a finished out-of-band excursion as one event."""
        self.hass.bus.async_fire(
            EVENT_VOLTAGE_EXCURSION,
            {
                **summary,
                "start": _iso(summary.get("start")),
                "end": _iso(summary.get("end")),
                "serial_number": self.device.serial_number,
                "device_name": self.device.name,
            },
        )

    async def _async_handle_stale(self, err: Exception) -> None:
        """Mark entities unavailable while the stream is down.

        Without this, entities hold their last value indefinitely during an
        outage and history renders a fake flat line instead of a gap.  Any
        partial window or open excursion is discarded rather than reported
        from before the gap.
        """
        self._aggregator.cancel()
        self.async_set_update_error(UpdateFailed(str(err)))


class TingProfileCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polling coordinator for low-rate Ting profile diagnostics and alerts.

    Each poll fetches the user profile and, best-effort, the notification
    history.  ``new_alerts`` holds, per device, the notifications first seen
    on the latest poll; the alerts event entities fire those.  The first
    successful notification fetch only seeds what has been seen, so a restart
    does not replay the backlog.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: TingApi,
        initial_user_data: dict[str, Any],
        initial_notifications: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Ting profile",
            update_interval=timedelta(minutes=5),
        )
        self._api = api
        self._seen_notification_ids: set[str] | None = None
        self.new_alerts: dict[str, list[dict[str, Any]]] = {}
        if initial_notifications is not None:
            self._process_notifications(initial_notifications, initial_user_data)
        self.async_set_updated_data(extract_device_diagnostics(initial_user_data))

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        try:
            user_data = await self._api.async_get_user()
        except TingAuthError as err:
            raise ConfigEntryAuthFailed("Ting authentication failed") from err
        except (TingConnectionError, TingResponseError) as err:
            raise UpdateFailed(f"Could not update Ting profile: {err}") from err

        # note: alerts are best-effort.  A failed fetch leaves the profile
        #       entities updating; anything missed is still in the history
        #       next poll and fires then.
        try:
            notifications = await self._api.async_get_notifications()
        except TingAuthError as err:
            raise ConfigEntryAuthFailed("Ting authentication failed") from err
        except (TingConnectionError, TingResponseError) as err:
            _LOGGER.debug("Could not fetch Ting notifications: %s", err)
            self.new_alerts = {}
        else:
            self._process_notifications(notifications, user_data)

        return extract_device_diagnostics(user_data)

    def _process_notifications(
        self, notifications: list[dict[str, Any]], user_data: dict[str, Any]
    ) -> None:
        grouped = extract_notifications(notifications, user_data)
        all_ids = {note["id"] for notes in grouped.values() for note in notes}
        if self._seen_notification_ids is None:
            self._seen_notification_ids = all_ids
            self.new_alerts = {}
            return
        seen = self._seen_notification_ids
        self.new_alerts = {
            serial: new
            for serial, notes in grouped.items()
            if (new := [note for note in notes if note["id"] not in seen])
        }
        seen |= all_ids
