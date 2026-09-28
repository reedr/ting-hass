"""Ting cloud REST API helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from typing import Any

from aiohttp import ClientError

from .auth import TingAuth
from .const import TING_API_BASE
from .exceptions import (
    TingAuthError,
    TingConnectionError,
    TingRateLimitError,
    TingResponseError,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TingDevice:
    """A Ting device discovered from the user profile."""

    serial_number: str
    name: str
    model: str | None = None
    firmware: str | None = None
    site_name: str | None = None


class TingApi:
    """Small wrapper around Ting's REST API."""

    def __init__(self, auth: TingAuth) -> None:
        self._auth = auth

    async def async_get_user(self) -> dict[str, Any]:
        """Fetch the Ting user profile and devices."""
        data = await self._async_get_json(f"/api/v1/Users/{self._auth.user_id}")
        if not isinstance(data, dict):
            raise TingResponseError("Ting user response was not an object")
        return data

    async def async_get_notifications(self) -> list[dict[str, Any]]:
        """Fetch the account's recent notifications (the Ting app's alerts)."""
        data = await self._async_get_json(
            f"/api/v1/Notifications/history/{self._auth.user_id}"
        )
        if not isinstance(data, list):
            raise TingResponseError("Ting notification response was not a list")
        return [item for item in data if isinstance(item, dict)]

    async def _async_get_json(self, path: str) -> Any:
        await self._auth.async_ensure_tokens()
        url = f"{TING_API_BASE}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._auth.id_token}",
            "x-wl-api-key": self._auth.api_key,
        }
        try:
            async with self._auth.session.get(url, headers=headers) as response:
                if response.status in (401, 403):
                    raise TingAuthError(f"Ting API returned HTTP {response.status}")
                if response.status == 429:
                    raise TingRateLimitError("Ting API rate limit exceeded")
                if response.status >= 500:
                    raise TingConnectionError(f"Ting API returned HTTP {response.status}")
                text = await response.text()
        except (ClientError, TimeoutError) as err:
            raise TingConnectionError("Could not connect to Ting API") from err

        if response.status >= 400:
            _LOGGER.debug("Ting API error %s: %s", response.status, text)
            raise TingResponseError(f"Ting API returned HTTP {response.status}")

        try:
            return json.loads(text)
        except json.JSONDecodeError as err:
            raise TingResponseError("Ting API returned a non-JSON response") from err


def extract_devices(user_data: Mapping[str, Any], default_serial: str | None = None) -> list[TingDevice]:
    """Extract devices from Ting's profile response.

    The mobile app has used several nested shapes over time. This intentionally
    walks the response and accepts any object carrying a serial number.
    """
    found: dict[str, TingDevice] = {}

    for item, parents in _walk_dicts(user_data):
        serial = _first_str(
            item,
            "serialNumber",
            "SerialNumber",
            "serial_number",
            "stationId",
            "StationId",
        )
        if not serial:
            continue

        name = (
            _first_str(item, "name", "Name", "displayName", "DisplayName", "nickname", "Nickname")
            or _site_name(parents)
            or f"Ting {serial}"
        )
        model = _first_str(item, "type", "Type", "deviceType", "DeviceType", "model", "Model")
        firmware = _first_str(item, "version", "Version", "firmware", "Firmware", "firmwareVersion")
        found[serial] = TingDevice(
            serial_number=serial,
            name=name,
            model=model,
            firmware=firmware,
            site_name=_site_name(parents),
        )

    if default_serial and default_serial not in found:
        found[default_serial] = TingDevice(
            serial_number=default_serial,
            name=f"Ting {default_serial}",
        )

    return list(found.values())


def extract_device_diagnostics(user_data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return normalized, non-personal diagnostics keyed by device serial."""
    sites: dict[str, bool] = {}
    for item, _parents in _walk_dicts(user_data):
        site_id = _identifier(item.get("id"))
        power_quality_hazard = item.get("isPowerQualityHazard")
        if site_id is not None and isinstance(power_quality_hazard, bool):
            sites[site_id] = power_quality_hazard

    diagnostics: dict[str, dict[str, Any]] = {}
    for item, _parents in _walk_dicts(user_data):
        serial = _first_str(
            item,
            "serialNumber",
            "SerialNumber",
            "serial_number",
            "stationId",
            "StationId",
        )
        if not serial:
            continue

        device_diagnostics = diagnostics.setdefault(serial, {})

        fire_hazard = item.get("isFire")
        if isinstance(fire_hazard, bool):
            device_diagnostics["fire_hazard"] = fire_hazard

        fire_hazard_status = item.get("fireHazardStatus")
        if isinstance(fire_hazard_status, Mapping):
            learning_mode = fire_hazard_status.get("learningMode")
            if isinstance(learning_mode, bool):
                device_diagnostics["learning_mode"] = learning_mode

            hazard_message = fire_hazard_status.get("message")
            if isinstance(hazard_message, str):
                device_diagnostics["hazard_message"] = hazard_message

            for source, prefix in HAZARD_STATUS_SOURCES:
                device_diagnostics.update(
                    _hazard_status_diagnostics(fire_hazard_status.get(source), prefix)
                )

        frozen_pipe = item.get("hasFrozenPipe")
        if isinstance(frozen_pipe, bool):
            device_diagnostics["frozen_pipe"] = frozen_pipe

        site_id = _identifier(item.get("siteId"))
        if site_id is not None and site_id in sites:
            device_diagnostics["power_quality_hazard"] = sites[site_id]

    return diagnostics


def extract_notifications(
    notifications: Iterable[Mapping[str, Any]],
    user_data: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Normalize Ting notifications and group them by device serial, oldest first.

    A notification names its device by serialNumber.  One without a serial
    (a site-level alert) goes to every device at its siteId, or to every
    device when it names neither.  Notifications without an id are dropped,
    since the id is what tells a new alert from one already seen.
    """
    device_sites: dict[str, str | None] = {}
    for item, _parents in _walk_dicts(user_data):
        serial = _first_str(item, "serialNumber", "SerialNumber", "serial_number")
        if serial:
            device_sites.setdefault(serial, _identifier(item.get("siteId")))

    grouped: dict[str, list[dict[str, Any]]] = {serial: [] for serial in device_sites}
    for item in notifications:
        note_id = _identifier(item.get("id"))
        if note_id is None:
            continue
        note = {
            "id": note_id,
            "event_type": _first_str(item, "eventType") or "unknown",
            "category": _first_str(item, "eventCategory"),
            "title": _first_str(item, "title"),
            "subtitle": _first_str(item, "subtitle"),
            "message": _first_str(item, "message"),
            "timestamp": _parse_iso(item.get("eventTimestampLocal")),
            "sent": _parse_iso(item.get("sentUtc")),
            "acknowledged": item.get("isAcknowledged") is True,
            "cleared": item.get("isCleared") is True,
        }
        serial = _first_str(item, "serialNumber")
        site_id = _identifier(item.get("siteId"))
        if serial is not None:
            targets = [serial] if serial in grouped else []
        elif site_id is not None:
            targets = [s for s, site in device_sites.items() if site == site_id]
        else:
            targets = list(grouped)
        for target in targets:
            grouped[target].append(note)

    for notes in grouped.values():
        notes.sort(key=_notification_sort_key)
    return grouped


def _notification_sort_key(note: Mapping[str, Any]) -> datetime:
    return note["sent"] or note["timestamp"] or datetime.min.replace(tzinfo=timezone.utc)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


# Ting's two hazard detectors, each reported under fireHazardStatus with a
# level, a status and a message:
#   efhStatus - electrical fire hazard (arcing inside the home's wiring)
#   ufhStatus - utility fire hazard (a fault on the utility side of the meter)
HAZARD_STATUS_SOURCES = (
    ("efhStatus", "electrical_fire_hazard"),
    ("ufhStatus", "utility_fire_hazard"),
)


def _hazard_status_diagnostics(value: Any, prefix: str) -> dict[str, Any]:
    """Normalize one efhStatus / ufhStatus block.

    note: Ting reports a null level and status when there is no hazard, so a
          null inside a present block means "no hazard" (level 0, status
          "none") rather than unknown.  A missing block, or a value of the
          wrong type, is left out so the entity reads unavailable.
    """
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}

    level = value.get("level")
    if level is None:
        level = 0
    if isinstance(level, int) and not isinstance(level, bool):
        result[f"{prefix}_level"] = level
        result[prefix] = level > 0

    status = value.get("status")
    if status is None:
        status = "none"
    if isinstance(status, str):
        result[f"{prefix}_status"] = status

    message = value.get("message")
    if isinstance(message, str):
        result[f"{prefix}_message"] = message

    return result


def _walk_dicts(value: Any, parents: tuple[Mapping[str, Any], ...] = ()) -> Iterable[tuple[Mapping[str, Any], tuple[Mapping[str, Any], ...]]]:
    if isinstance(value, Mapping):
        yield value, parents
        next_parents = (*parents, value)
        for child in value.values():
            yield from _walk_dicts(child, next_parents)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child, parents)


def _first_str(item: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return None


def _identifier(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _site_name(parents: tuple[Mapping[str, Any], ...]) -> str | None:
    for parent in reversed(parents):
        value = _first_str(parent, "siteName", "SiteName", "locationName", "LocationName", "address1")
        if value:
            return value
    return None
