# Ting Home Assistant Integration

Experimental HACS custom integration for Whisker Labs Ting monitors.

## What it exposes

The integration signs in with the same Cognito user pool used by the Ting mobile app, discovers Ting devices from the account profile, and subscribes to Ting's realtime SignalR websocket stream.
Realtime SignalR transport is handled with `pysignalr`.

### Interval statistics

Ting streams about four voltage samples per second. Rather than forwarding the
latest sample every few seconds (which would miss a short sag between
publishes), the integration summarises each publish window as **mean, min and
max** and publishes once per window, aligned to the clock so several Ting
devices line up in history. Values are rounded to 0.1 V, so Home Assistant
skips writing a new state when the voltage has not changed.

While the voltage is outside a configurable band, windows shorten to a fast
interval. The first out-of-band sample is published immediately, so
automations can react within a sample, and **Voltage out of range** turns on.
Fast mode ends, and the sensor turns off, once the voltage has stayed at least
1 V inside the band for the hold time. A `ting_voltage_excursion` event then
reports the excursion once:

| Field | Meaning |
|---|---|
| `start`, `end` | First and last out-of-band sample (UTC, ISO 8601) |
| `duration` | Seconds between them; 0 for a single-sample dip |
| `min`, `max` | Extreme voltages during the excursion |
| `sag`, `swell` | Whether it went below the band, above it, or both |
| `band_low`, `band_high` | The band in force |
| `serial_number`, `device_name` | Which Ting |

Options (**Settings > Devices & services > Ting > Configure**):

| Option | Default | |
|---|---|---|
| Publish interval | 60 s | Normal window length |
| Fast interval | 5 s | Window length while out of band |
| Band low / high | 114 / 126 V | ANSI C84.1 Range A for 120 V nominal |
| Hold time | 60 s | Time back in band before fast mode ends |

Saving the options reloads the integration. A window with no samples publishes
nothing; if the stream itself stops, the realtime entities become unavailable.

Ting cannot report a power outage: it is powered by the circuit it measures,
so during an outage its entities simply become unavailable, the same as a
cloud or internet outage.

Realtime sensors:

- Voltage (window mean)
- Voltage min (interval) and Voltage max (interval)
- Voltage out of range (binary sensor, problem)
- Hi-Fi, from Ting's `AveragePeaksMax` datapoint
- Voltage high and Voltage low, Ting's own `VoltageHi` / `VoltageLo` datapoints
- Last update

REST profile safety entities, refreshed every 5 minutes:

- Fire hazard (binary sensor)
- Power quality hazard (binary sensor)

REST profile diagnostic entities, refreshed every 5 minutes:

- Learning mode (binary sensor)
- Hazard message (sensor)

Profile responses are normalized to these four values. Full device, site, and
account profile payloads are not stored as coordinator data. If Ting omits a
value or returns it with an unexpected type, the corresponding entity is
unavailable rather than reporting a misleading safe state.

## Install with HACS

1. Add this repository as a custom HACS integration repository.
2. Install **Ting** from HACS.
3. Restart Home Assistant.
4. Add the integration from **Settings > Devices & services > Add integration > Ting**.
5. Sign in with your Ting username and password.

The password is only used during setup. Home Assistant stores the Cognito refresh token returned by Ting and uses that for future token refreshes.

## Notes

This integration is based on observed Ting app behavior as of June 30, 2026. Ting does not appear to publish this API, so endpoints, auth details, and realtime payloads may change.

The HACS brand icon is included at `custom_components/ting/brand/icon.png`.

## Tests

Run the unit and Home Assistant lifecycle tests with Python 3.14:

```bash
uv run --no-project --with-requirements requirements-test.txt python -m pytest -q
```

Cloud responses are mocked; tests do not require a Ting account.

## Local auth probe

Run the standalone Cognito SRP auth probe from the repository root:

```bash
python3 scripts/ting_auth_probe.py -u you@example.com
```

The script prompts for the password, does not write credentials or tokens to disk, and prints only redacted token details on success.
