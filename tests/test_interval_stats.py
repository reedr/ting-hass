"""Home Assistant-level tests for interval voltage statistics."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.ting.const import (
    CONF_BAND_HIGH,
    CONF_BAND_LOW,
    CONF_FAST_INTERVAL,
    CONF_HOLD,
    CONF_INTERVAL,
    CONF_REFRESH_TOKEN,
    DOMAIN,
    EVENT_VOLTAGE_EXCURSION,
)

PROFILE = {"devices": [{"serialNumber": "TEST-001", "isFire": False}]}

# Sub-second options so the tests run on real timers quickly.  The options
# schema enforces whole seconds for users; the coordinator accepts any float.
FAST_OPTIONS = {
    CONF_INTERVAL: 0.2,
    CONF_FAST_INTERVAL: 0.05,
    CONF_BAND_LOW: 114.0,
    CONF_BAND_HIGH: 126.0,
    CONF_HOLD: 0.1,
}

VOLTAGE = "sensor.ting_test_001_voltage"
VOLTAGE_MIN = "sensor.ting_test_001_voltage_min_interval"
VOLTAGE_MAX = "sensor.ting_test_001_voltage_max_interval"
OUT_OF_RANGE = "binary_sensor.ting_test_001_voltage_out_of_range"


def _entry(hass, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="test-user",
        data={CONF_REFRESH_TOKEN: "test-refresh-token", "username": "test@example.invalid"},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry):
    with (
        patch("custom_components.ting.TingAuth.async_ensure_tokens"),
        patch("custom_components.ting.TingApi.async_get_user", return_value=PROFILE),
        patch("custom_components.ting.coordinator.TingRealtimeCoordinator.async_start"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id].coordinators["TEST-001"]


async def _feed(coordinator, *voltages):
    for v in voltages:
        await coordinator._async_handle_update({"voltage": v, "voltage_high": 125.0})


async def test_window_statistics_reach_entities(hass):
    """Mean, min and max of a window land on the sensors."""
    coordinator = await _setup(hass, _entry(hass, FAST_OPTIONS))
    assert hass.states.get(VOLTAGE).state == STATE_UNAVAILABLE

    await _feed(coordinator, 118.0, 120.0, 122.0)
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()

    assert float(hass.states.get(VOLTAGE).state) == 120.0
    assert float(hass.states.get(VOLTAGE_MIN).state) == 118.0
    assert float(hass.states.get(VOLTAGE_MAX).state) == 122.0
    assert hass.states.get(OUT_OF_RANGE).state == STATE_OFF
    attrs = hass.states.get(VOLTAGE).attributes
    assert attrs["window_seconds"] == 0.2
    assert "data_time_utc" not in attrs
    await coordinator.async_stop()


async def test_out_of_range_turns_on_immediately_and_fires_event(hass):
    """A sag flips the problem sensor at once and reports one event on recovery."""
    events = async_capture_events(hass, EVENT_VOLTAGE_EXCURSION)
    coordinator = await _setup(hass, _entry(hass, FAST_OPTIONS))

    await _feed(coordinator, 120.0, 104.2)
    await hass.async_block_till_done()
    assert hass.states.get(OUT_OF_RANGE).state == STATE_ON
    assert float(hass.states.get(VOLTAGE_MIN).state) == 104.2

    for _ in range(12):
        await _feed(coordinator, 120.0)
        await asyncio.sleep(0.03)
    await asyncio.sleep(0.1)
    await hass.async_block_till_done()

    assert hass.states.get(OUT_OF_RANGE).state == STATE_OFF
    assert len(events) == 1
    data = events[0].data
    assert data["min"] == 104.2
    assert data["sag"] is True
    assert data["swell"] is False
    assert data["serial_number"] == "TEST-001"
    assert data["start"].endswith("+00:00")
    await coordinator.async_stop()


async def test_stale_stream_marks_new_entities_unavailable(hass):
    """The new entities follow the stream's availability."""
    coordinator = await _setup(hass, _entry(hass, FAST_OPTIONS))
    await _feed(coordinator, 120.0)
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()
    assert hass.states.get(OUT_OF_RANGE).state == STATE_OFF

    await coordinator._async_handle_stale(RuntimeError("stream silent"))
    await hass.async_block_till_done()
    for entity_id in (VOLTAGE, VOLTAGE_MIN, VOLTAGE_MAX, OUT_OF_RANGE):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    await coordinator.async_stop()


async def test_default_options_apply_without_configuration(hass):
    """An entry created before this feature gets the documented defaults."""
    coordinator = await _setup(hass, _entry(hass))
    cfg = coordinator.aggregator_config
    assert (cfg.interval, cfg.fast_interval, cfg.band_low, cfg.band_high, cfg.hold) == (
        60.0,
        5.0,
        114.0,
        126.0,
        60.0,
    )
    await coordinator.async_stop()


async def test_options_flow_validates_and_saves(hass):
    """Invalid combinations are rejected; valid ones are stored."""
    entry = _entry(hass)
    await _setup(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    bad = {
        CONF_INTERVAL: 10,
        CONF_FAST_INTERVAL: 30,
        CONF_BAND_LOW: 114,
        CONF_BAND_HIGH: 126,
        CONF_HOLD: 60,
    }
    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_options"}

    good = {**bad, CONF_INTERVAL: 30, CONF_FAST_INTERVAL: 2}
    with (
        patch("custom_components.ting.TingAuth.async_ensure_tokens"),
        patch("custom_components.ting.TingApi.async_get_user", return_value=PROFILE),
        patch("custom_components.ting.coordinator.TingRealtimeCoordinator.async_start"),
    ):
        result = await hass.config_entries.options.async_configure(result["flow_id"], good)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_INTERVAL] == 30
    # The reload rebuilt the coordinator with the new settings.
    coordinator = hass.data[DOMAIN][entry.entry_id].coordinators["TEST-001"]
    assert coordinator.aggregator_config.interval == 30.0
    await coordinator.async_stop()


@pytest.mark.parametrize("voltage", [None, float("nan")])
async def test_non_numeric_voltage_does_not_publish(hass, voltage):
    """A sample without a usable voltage never creates a window by itself."""
    coordinator = await _setup(hass, _entry(hass, FAST_OPTIONS))
    await coordinator._async_handle_update({"voltage": voltage})
    await asyncio.sleep(0.25)
    await hass.async_block_till_done()
    assert hass.states.get(VOLTAGE).state == STATE_UNAVAILABLE
    await coordinator.async_stop()
