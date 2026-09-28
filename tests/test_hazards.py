"""Home Assistant-level tests for the hazard detector entities."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ting.const import CONF_REFRESH_TOKEN, DOMAIN

PROFILE = {
    "devices": [
        {
            "serialNumber": "TEST-001",
            "isFire": False,
            "hasFrozenPipe": False,
            "fireHazardStatus": {
                "learningMode": False,
                "message": "No Hazards Detected",
                "efhStatus": {
                    "level": 3,
                    "status": "ReviewedNotFire",
                    "message": "Synthetic electrical hazard",
                },
                "ufhStatus": {"level": None, "status": None},
            },
        }
    ]
}


async def _setup(hass, profile):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={CONF_REFRESH_TOKEN: "test-refresh-token", "username": "test@example.invalid"},
    )
    entry.add_to_hass(hass)
    with (
        patch("custom_components.ting.TingAuth.async_ensure_tokens"),
        patch("custom_components.ting.TingApi.async_get_user", return_value=profile),
        patch("custom_components.ting.coordinator.TingRealtimeCoordinator.async_start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_hazard_detector_entities(hass):
    """Detector levels drive the hazard binary sensors independently of isFire."""
    await _setup(hass, PROFILE)

    assert hass.states.get("binary_sensor.ting_test_001_fire_hazard").state == STATE_OFF
    assert hass.states.get("binary_sensor.ting_test_001_electrical_fire_hazard").state == STATE_ON
    assert hass.states.get("binary_sensor.ting_test_001_utility_fire_hazard").state == STATE_OFF
    assert hass.states.get("binary_sensor.ting_test_001_frozen_pipe_risk").state == STATE_OFF

    assert hass.states.get("sensor.ting_test_001_electrical_fire_hazard_level").state == "3"
    status = hass.states.get("sensor.ting_test_001_electrical_fire_hazard_status")
    assert status.state == "ReviewedNotFire"
    assert status.attributes["message"] == "Synthetic electrical hazard"
    assert hass.states.get("sensor.ting_test_001_utility_fire_hazard_level").state == "0"
    assert hass.states.get("sensor.ting_test_001_utility_fire_hazard_status").state == "none"


async def test_missing_detector_block_is_unavailable(hass):
    """Without efhStatus / ufhStatus / hasFrozenPipe the entities never read safe."""
    await _setup(
        hass,
        {"devices": [{"serialNumber": "TEST-001", "isFire": False, "fireHazardStatus": {}}]},
    )

    for entity_id in (
        "binary_sensor.ting_test_001_electrical_fire_hazard",
        "binary_sensor.ting_test_001_utility_fire_hazard",
        "binary_sensor.ting_test_001_frozen_pipe_risk",
        "sensor.ting_test_001_electrical_fire_hazard_level",
        "sensor.ting_test_001_utility_fire_hazard_status",
    ):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE, entity_id


async def test_detector_entities_survive_deprecated_cleanup(hass):
    """The level/status unique IDs are no longer purged at setup."""
    entry = await _setup(hass, PROFILE)
    registry = er.async_get(hass)
    unique_ids = {
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert "TEST-001_electrical_fire_hazard_level" in unique_ids
    assert "TEST-001_utility_fire_hazard_status" in unique_ids

    await hass.config_entries.async_unload(entry.entry_id)
    with (
        patch("custom_components.ting.TingAuth.async_ensure_tokens"),
        patch("custom_components.ting.TingApi.async_get_user", return_value=PROFILE),
        patch("custom_components.ting.coordinator.TingRealtimeCoordinator.async_start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get_entity_id(
        "sensor", DOMAIN, "TEST-001_electrical_fire_hazard_level"
    ) == "sensor.ting_test_001_electrical_fire_hazard_level"
