"""Tests for Ting notifications and the alerts event entity."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import Event, callback
from homeassistant.helpers.event import async_track_state_change_event
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ting.api import extract_notifications
from custom_components.ting.const import CONF_REFRESH_TOKEN, DOMAIN
from custom_components.ting.exceptions import TingConnectionError

PROFILE = {
    "devices": [
        {"serialNumber": "TEST-001", "siteId": 10, "isFire": False},
        {"serialNumber": "TEST-002", "siteId": 20, "isFire": False},
    ]
}
ALERTS = "event.ting_test_001_alerts"


def _note(note_id, event_type="FireHazard", serial="TEST-001", sent="2030-01-01T00:00:00Z", **extra):
    return {
        "id": note_id,
        "eventType": event_type,
        "eventCategory": "Safety",
        "title": f"Title {note_id}",
        "subtitle": None,
        "message": f"Message {note_id}",
        "eventTimestampLocal": "2029-12-31T17:00:00-07:00",
        "sentUtc": sent,
        "serialNumber": serial,
        "isAcknowledged": False,
        "isCleared": False,
        **extra,
    }


def test_extract_notifications_groups_and_orders() -> None:
    """Notifications land on their device, site-level ones on the site's devices."""
    notes = [
        _note("b", sent="2030-01-02T00:00:00Z"),
        _note("a", sent="2030-01-01T00:00:00Z"),
        _note("site", "WeatherAlert", serial=None, siteId=20),
        _note("account", "WeatherAlert", serial=None, sent="2030-01-01T06:00:00Z"),
        _note("other", serial="NOT-MINE"),
        {"eventType": "FireHazard", "serialNumber": "TEST-001"},  # no id
    ]

    grouped = extract_notifications(notes, PROFILE)

    assert [n["id"] for n in grouped["TEST-001"]] == ["a", "account", "b"]
    assert {n["id"] for n in grouped["TEST-002"]} == {"site", "account"}
    first = next(n for n in grouped["TEST-001"] if n["id"] == "a")
    assert first["event_type"] == "FireHazard"
    assert first["message"] == "Message a"
    assert first["sent"].isoformat() == "2030-01-01T00:00:00+00:00"
    assert first["acknowledged"] is False


async def _setup(hass, initial):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={CONF_REFRESH_TOKEN: "test-refresh-token", "username": "test@example.invalid"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ting.TingAuth.async_ensure_tokens"),
        patch("custom_components.ting.TingApi.async_get_user", return_value=PROFILE),
        patch("custom_components.ting.TingApi.async_get_notifications", **initial),
        patch("custom_components.ting.coordinator.TingRealtimeCoordinator.async_start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id].profile_coordinator


async def _poll(hass, coordinator, notifications):
    with (
        patch.object(coordinator._api, "async_get_user", return_value=PROFILE),
        patch.object(coordinator._api, "async_get_notifications", **notifications),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()


def _capture(hass):
    fired: list[dict] = []

    @callback
    def _listener(event: Event) -> None:
        fired.append(dict(event.data["new_state"].attributes))

    async_track_state_change_event(hass, ALERTS, _listener)
    return fired


async def test_backlog_is_not_replayed_and_new_alerts_fire(hass):
    """History at setup is seeded; each later new notification fires once, in order."""
    coordinator = await _setup(hass, {"return_value": [_note("old")]})
    assert hass.states.get(ALERTS).state == STATE_UNKNOWN
    fired = _capture(hass)

    await _poll(
        hass,
        coordinator,
        {
            "return_value": [
                _note("old"),
                _note("n2", "FrozenPipe", sent="2030-01-02T00:00:00Z"),
                _note("n1", "Sag", sent="2030-01-01T12:00:00Z"),
                _note("n3", "SomethingNew", sent="2030-01-03T00:00:00Z"),
            ]
        },
    )

    assert [(a["event_type"], a["notification_id"]) for a in fired] == [
        ("Sag", "n1"),
        ("FrozenPipe", "n2"),
        ("unknown", "n3"),
    ]
    assert fired[2]["raw_event_type"] == "SomethingNew"
    assert fired[1]["message"] == "Message n2"

    # The same history on the next poll fires nothing.
    await _poll(hass, coordinator, {"return_value": [_note("old"), _note("n1"), _note("n2")]})
    assert len(fired) == 3


async def test_failed_setup_fetch_seeds_on_first_poll(hass):
    """Without a setup fetch, the first successful poll seeds rather than fires."""
    coordinator = await _setup(hass, {"side_effect": TingConnectionError("down")})
    fired = _capture(hass)

    await _poll(hass, coordinator, {"return_value": [_note("old")]})
    assert fired == []

    await _poll(hass, coordinator, {"side_effect": TingConnectionError("down")})
    assert fired == []

    await _poll(hass, coordinator, {"return_value": [_note("old"), _note("new")]})
    assert [a["notification_id"] for a in fired] == ["new"]
