"""Interval statistics for Ting's realtime voltage stream.

Ting streams about four samples per second. Publishing every sample (or the
latest sample every few seconds) either floods the recorder or hides short
events: a two-second sag between publishes never reaches Home Assistant. This
aggregator instead summarises each window as mean / min / max, publishes on
wall-clock-aligned boundaries, and switches to a faster cadence while the
voltage is outside a configured band.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import math
import time
from typing import Any

# Samples that carry more than the voltage itself; the latest of these is
# passed through unchanged so the existing sensors keep working.
PASSTHROUGH_KEYS = ("voltage_high", "voltage_low", "hifi", "last_update", "raw")


@dataclass(frozen=True)
class AggregatorConfig:
    """Tunable behaviour, normally taken from the config entry options."""

    interval: float
    fast_interval: float
    band_low: float
    band_high: float
    hold: float
    hysteresis: float = 1.0
    decimals: int = 1

    def __post_init__(self) -> None:
        if self.interval <= 0 or self.fast_interval <= 0:
            raise ValueError("intervals must be positive")
        if self.fast_interval > self.interval:
            raise ValueError("fast_interval must not exceed interval")
        if self.band_low >= self.band_high:
            raise ValueError("band_low must be below band_high")
        if self.hysteresis < 0 or 2 * self.hysteresis >= self.band_high - self.band_low:
            raise ValueError("hysteresis must be non-negative and narrower than the band")
        if self.hold < 0:
            raise ValueError("hold must not be negative")


class VoltageWindowAggregator:
    """Summarise realtime samples into windows, with a fast excursion mode.

    Normal mode publishes one summary per ``interval`` seconds, aligned to the
    wall clock so several devices line up in history. The first sample outside
    ``band_low``..``band_high`` publishes immediately (so automations see an
    excursion within one sample) and switches to ``fast_interval`` windows.
    Fast mode ends once the voltage has stayed inside the band, narrowed by
    ``hysteresis`` on each side, for ``hold`` seconds; the excursion is then
    reported once through ``excursion_callback``.

    A window with no samples publishes nothing - the coordinator's stale
    handling marks entities unavailable if the stream itself has stopped.
    """

    def __init__(
        self,
        config: AggregatorConfig,
        callback: Callable[[dict[str, Any]], None],
        excursion_callback: Callable[[dict[str, Any]], None] | None = None,
        *,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._callback = callback
        self._excursion_callback = excursion_callback
        self._wall = wall_clock
        self._handle: asyncio.TimerHandle | None = None
        self._passthrough: dict[str, Any] = {}
        self._last_published: dict[str, Any] | None = None
        self._reset_window()
        self._reset_excursion()
        self._fast = False

    # --- public -----------------------------------------------------------

    @property
    def fast(self) -> bool:
        """True while in excursion (fast) mode."""
        return self._fast

    def submit(self, sample: dict[str, Any]) -> None:
        """Add one realtime sample."""
        voltage = sample.get("voltage")
        for key in PASSTHROUGH_KEYS:
            if key in sample:
                self._passthrough[key] = sample[key]
        if not isinstance(voltage, (int, float)) or math.isnan(voltage):
            self._ensure_timer()
            return

        now = self._wall()
        self._count += 1
        self._sum += voltage
        self._min = voltage if self._min is None else min(self._min, voltage)
        self._max = voltage if self._max is None else max(self._max, voltage)
        self._last = voltage

        if self._outside_band(voltage):
            self._last_out = now
            self._recovered_since = None
            self._exc_min = voltage if self._exc_min is None else min(self._exc_min, voltage)
            self._exc_max = voltage if self._exc_max is None else max(self._exc_max, voltage)
            if not self._fast:
                # Enter fast mode and publish right away rather than waiting
                # up to a full interval for the window to close.
                self._fast = True
                self._exc_start = now
                self._flush()
                self._reschedule()
                return
        elif self._fast and self._recovered_since is None and self._inside_hysteresis(voltage):
            self._recovered_since = now
        elif self._fast and not self._inside_hysteresis(voltage):
            # Back in band but still within the hysteresis margin: not yet
            # recovered.  Restart the hold clock.
            self._recovered_since = None

        self._ensure_timer()

    def cancel(self) -> None:
        """Stop publishing and forget any partial window or excursion."""
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._reset_window()
        self._reset_excursion()
        self._passthrough = {}
        self._last_published = None
        self._fast = False

    # --- internals --------------------------------------------------------

    def _reset_window(self) -> None:
        self._count = 0
        self._sum = 0.0
        self._min: float | None = None
        self._max: float | None = None
        self._last: float | None = None

    def _reset_excursion(self) -> None:
        self._exc_start: float | None = None
        self._last_out: float | None = None
        self._recovered_since: float | None = None
        self._exc_min: float | None = None
        self._exc_max: float | None = None

    def _outside_band(self, voltage: float) -> bool:
        return voltage < self._config.band_low or voltage > self._config.band_high

    def _inside_hysteresis(self, voltage: float) -> bool:
        h = self._config.hysteresis
        return self._config.band_low + h <= voltage <= self._config.band_high - h

    def _current_interval(self) -> float:
        return self._config.fast_interval if self._fast else self._config.interval

    def _delay_to_boundary(self) -> float:
        interval = self._current_interval()
        now = self._wall()
        boundary = math.floor(now / interval) * interval + interval
        return max(boundary - now, 0.0)

    def _ensure_timer(self) -> None:
        if self._handle is None:
            self._schedule()

    def _reschedule(self) -> None:
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        self._schedule()

    def _schedule(self) -> None:
        loop = asyncio.get_running_loop()
        self._handle = loop.call_later(self._delay_to_boundary(), self._on_boundary)

    def _on_boundary(self) -> None:
        self._handle = None
        self._flush()
        if self._fast and self._recovered_since is not None:
            if self._wall() - self._recovered_since >= self._config.hold:
                self._end_excursion()
                # Publish the state change (out_of_range -> False) at once.
                self._publish_state_only()
        self._schedule()

    def _end_excursion(self) -> None:
        # note: "end" is the last OUT-OF-BAND sample, so duration measures the
        #       excursion itself, not the hold time that followed it.  A
        #       single-sample dip reports a duration of 0.
        start = self._exc_start
        last_out = self._last_out
        summary = {
            "start": start,
            "end": last_out,
            "duration": round(last_out - start, 1) if start is not None and last_out is not None else None,
            "min": self._round(self._exc_min),
            "max": self._round(self._exc_max),
            "sag": self._exc_min is not None and self._exc_min < self._config.band_low,
            "swell": self._exc_max is not None and self._exc_max > self._config.band_high,
            "band_low": self._config.band_low,
            "band_high": self._config.band_high,
        }
        self._fast = False
        self._reset_excursion()
        if self._excursion_callback is not None:
            self._excursion_callback(summary)

    def _round(self, value: float | None) -> float | None:
        return None if value is None else round(value, self._config.decimals)

    def _flush(self) -> None:
        if self._count == 0:
            return
        data = {
            **self._passthrough,
            "voltage": self._round(self._sum / self._count),
            "voltage_min": self._round(self._min),
            "voltage_max": self._round(self._max),
            "voltage_last": self._round(self._last),
            "samples": self._count,
            "window": self._current_interval(),
            "out_of_range": self._fast,
        }
        self._last_published = data
        self._reset_window()
        self._callback(data)

    def _publish_state_only(self) -> None:
        """Re-publish the last summary with the updated out_of_range flag."""
        if self._last_published is None:
            return
        data = {
            **self._last_published,
            "out_of_range": self._fast,
            "window": self._current_interval(),
        }
        self._last_published = data
        self._callback(data)
