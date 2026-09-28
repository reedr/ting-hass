"""Tests for interval voltage statistics and excursion mode."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.ting.aggregator import AggregatorConfig, VoltageWindowAggregator

# Short real-time intervals keep these tests fast while exercising the real
# asyncio scheduling.
CONFIG = AggregatorConfig(
    interval=0.2, fast_interval=0.05, band_low=114.0, band_high=126.0, hold=0.1, hysteresis=1.0
)


def _collect() -> tuple[list[dict], list[dict], VoltageWindowAggregator]:
    published: list[dict] = []
    excursions: list[dict] = []
    agg = VoltageWindowAggregator(CONFIG, published.append, excursions.append)
    return published, excursions, agg


def test_window_publishes_mean_min_max() -> None:
    """A normal window is summarised once, at the boundary."""

    async def run() -> None:
        published, excursions, agg = _collect()
        for v in (118.0, 120.0, 122.0):
            agg.submit({"voltage": v, "voltage_high": 123.4, "hifi": 0.5})
        assert published == []
        await asyncio.sleep(0.25)
        agg.cancel()
        assert len(published) == 1
        data = published[0]
        assert data["voltage"] == 120.0
        assert data["voltage_min"] == 118.0
        assert data["voltage_max"] == 122.0
        assert data["voltage_last"] == 122.0
        assert data["samples"] == 3
        assert data["out_of_range"] is False
        # Non-voltage fields from the latest sample pass through unchanged.
        assert data["voltage_high"] == 123.4
        assert data["hifi"] == 0.5
        assert excursions == []

    asyncio.run(run())


def test_values_are_rounded() -> None:
    """Rounding to 0.1 V lets Home Assistant skip unchanged states."""

    async def run() -> None:
        published, _, agg = _collect()
        for v in (120.04, 120.06, 120.11):
            agg.submit({"voltage": v})
        await asyncio.sleep(0.25)
        agg.cancel()
        assert published[0]["voltage"] == 120.1
        assert published[0]["voltage_min"] == 120.0

    asyncio.run(run())


def test_empty_window_publishes_nothing() -> None:
    """Without samples there is nothing to report."""

    async def run() -> None:
        published, _, agg = _collect()
        agg.submit({"voltage": 120.0})
        await asyncio.sleep(0.25)
        count = len(published)
        await asyncio.sleep(0.25)
        agg.cancel()
        assert len(published) == count

    asyncio.run(run())


def test_samples_without_voltage_are_ignored() -> None:
    """A sample missing its voltage does not skew the statistics."""

    async def run() -> None:
        published, _, agg = _collect()
        agg.submit({"voltage": 120.0})
        agg.submit({"voltage": None, "hifi": 0.7})
        agg.submit({"voltage": 122.0})
        await asyncio.sleep(0.25)
        agg.cancel()
        assert published[0]["samples"] == 2
        assert published[0]["voltage"] == 121.0
        assert published[0]["hifi"] == 0.7

    asyncio.run(run())


def test_out_of_band_sample_publishes_immediately_and_goes_fast() -> None:
    """The first out-of-band sample is reported at once, not at the boundary."""

    async def run() -> None:
        published, _, agg = _collect()
        agg.submit({"voltage": 120.0})
        agg.submit({"voltage": 104.2})
        assert len(published) == 1
        assert published[0]["out_of_range"] is True
        assert published[0]["voltage_min"] == 104.2
        assert published[0]["window"] == CONFIG.fast_interval
        assert agg.fast
        agg.cancel()

    asyncio.run(run())


def test_excursion_ends_after_hold_and_reports_once() -> None:
    """Fast mode ends after the hold time back in band, with one summary."""

    async def run() -> None:
        published, excursions, agg = _collect()
        agg.submit({"voltage": 104.2})
        agg.submit({"voltage": 108.0})
        assert agg.fast
        # Stay back in band (inside the hysteresis margin) for longer than hold.
        for _ in range(12):
            agg.submit({"voltage": 120.0})
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.1)
        agg.cancel()

        assert len(excursions) == 1
        exc = excursions[0]
        assert exc["min"] == 104.2
        assert exc["max"] == 108.0
        assert exc["sag"] is True
        assert exc["swell"] is False
        assert exc["duration"] is not None and exc["duration"] >= 0
        # The flag clears in the published data once the excursion ends.
        assert published[-1]["out_of_range"] is False
        assert any(p["out_of_range"] for p in published)

    asyncio.run(run())


def test_hysteresis_margin_does_not_count_as_recovered() -> None:
    """Hovering just inside the band edge keeps fast mode on."""

    async def run() -> None:
        published, excursions, agg = _collect()
        agg.submit({"voltage": 112.0})
        # 114.5 is inside the band but within the 1.0 V hysteresis margin.
        for _ in range(12):
            agg.submit({"voltage": 114.5})
            await asyncio.sleep(0.03)
        assert agg.fast
        assert excursions == []
        agg.cancel()

    asyncio.run(run())


def test_new_excursion_restarts_hold_clock() -> None:
    """Dipping out again before the hold expires keeps one continuous excursion."""

    async def run() -> None:
        published, excursions, agg = _collect()
        agg.submit({"voltage": 110.0})
        agg.submit({"voltage": 120.0})
        await asyncio.sleep(0.06)
        agg.submit({"voltage": 128.5})  # swell before the hold expired
        for _ in range(12):
            agg.submit({"voltage": 120.0})
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.1)
        agg.cancel()
        assert len(excursions) == 1
        assert excursions[0]["sag"] is True
        assert excursions[0]["swell"] is True
        assert excursions[0]["max"] == 128.5

    asyncio.run(run())


def test_cancel_discards_partial_window_and_excursion() -> None:
    """Cancelling (stream went stale) forgets pending data."""

    async def run() -> None:
        published, excursions, agg = _collect()
        agg.submit({"voltage": 104.0})
        count = len(published)
        agg.cancel()
        assert not agg.fast
        await asyncio.sleep(0.25)
        assert len(published) == count
        assert excursions == []

    asyncio.run(run())


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"interval": 0}, "positive"),
        ({"fast_interval": 90}, "exceed"),
        ({"band_low": 130}, "below"),
        ({"hysteresis": 7}, "hysteresis"),
        ({"hold": -1}, "hold"),
    ],
)
def test_config_validation(kwargs, match) -> None:
    """Bad option combinations fail early."""
    base = {"interval": 60, "fast_interval": 5, "band_low": 114, "band_high": 126, "hold": 60}
    with pytest.raises(ValueError, match=match):
        AggregatorConfig(**{**base, **kwargs})
