# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PilotSense is a FastAPI web app for visualizing flight telemetry (GPS, IMU sensor, and ADS-B data). It serves an index of flights, a Leaflet map of each flight's trajectory, and Plotly time-series graphs.

## Commands

```bash
# Activate the virtualenv (lives in ./venv)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run the dev server
uvicorn app:app --reload

# Split a raw multi-flight NDJSON dump into per-flight files
python tools/parse.py   # reads data/flights.json, writes data/parsed/<FLIGHT_ID>.json

# Generate a synthetic flight that triggers safety events at every severity
python tools/make_demo_flight.py   # writes data/parsed/DEMO-EVENTS.json
```

There are no tests or linters configured.

## Architecture

Server-side code is [app.py](app.py) (routes, data loading) and [analysis.py](analysis.py) (safety-event detection).

**Safety analysis:** `analysis.py` runs detectors (`DETECTORS` list) over the merged flight DataFrame and emits events with severity 1–3 (Notice/Caution/Danger, colors in `SEVERITY_LEVELS`). Each detector groups consecutive exceedances into one event with a peak value and a human-readable `summary`. `GET /flight/{flight_id}/events` serves them; the map shows severity-colored markers plus a clickable events panel, the graphs page shades event time bands on every chart and lists explanations in a summary card. Thresholds are calibrated for light aircraft — tune them in the detector docstrings/code together.

**Data flow:** raw NDJSON logs → `tools/parse.py` splits them by the `f` (flight/callsign) field into `data/parsed/<FLIGHT_ID>.json` → `app.py` reads those files on every request (no database, no caching). The flight list on the index page is simply the filenames in `data/parsed/`.

**Data format:** each per-flight file is NDJSON, one JSON object per line. Lines are heterogeneous — a line may carry GPS fields (`Gps_lat`, `Gps_lon`, `Gps_datum`, `Gps_time`, ...), ADS-B fields (`Adsb_HexId`, `Adsb_alt`, `Adsb_callsign`, ...), and/or IMU fields (`accX`, `pitch`, ...). `load_flight_data()` in app.py merges these into one timeline: it accumulates the latest ADS-B state across lines and emits a DataFrame row only when a line contains a GPS fix, attaching the last-known ADS-B values to it.

**Data quirks handled in `load_flight_data()`:**
- Some log lines contain `"Adsb_HexId": 505CE5` (unquoted hex), which is invalid JSON — a regex repair quotes it before retrying the parse.
- GPS coordinates are in NMEA `DDMM.MMMM` format with separate sign fields (`Gps_latsign`/`Gps_lonsign`), converted via `_nmea_to_decimal()`.
- GPS timestamps use a two-digit-year format `DD-MM-YY HH:MM:SS` (UTC).
- Lines with `Gps_data: "no_data"` carry lat/lon `0.0` and are skipped.

**Data ingestion:** `POST /api/flights/{flight_id}/data` accepts a batch of raw NDJSON log lines from authorized devices and appends them to `data/parsed/<flight_id>.json`. Auth is per-device via `X-Device-Id` + `X-Api-Key` headers, checked against the `PILOTSENSE_DEVICE_KEYS` env var (`"device1:key1,device2:key2"`); the endpoint returns 503 when no keys are configured. Lines are validated with the same lenient parser (`_parse_log_line`) the loader uses; the response reports accepted/rejected counts.

**Single JSON API:** `GET /flight/{flight_id}/trajectory` returns `{trajectory, fields, labels, groups}`. Both frontends (map and graphs) consume this same endpoint:
- `trajectory` is a list of points (lat/lon/timestamp plus every value field, floats rounded to 2 decimals, NaN/inf → null).
- `labels` comes from the `FRIENDLY_NAMES` dict in app.py — add an entry there when introducing a new field so the UI shows a human-readable name.
- `groups` (`accel`, `att`, `adsb_position`, `adsb_movement`) drives which fields are plotted together on the graphs page; ungrouped fields each get their own chart.
- Fields in `HIDDEN_FIELDS` (raw gyro/magnetometer) are excluded from the API output.

**FlightRadar comparison:** [fr24_client.py](fr24_client.py) is a thin client for the official Flightradar24 API (`fr24api.flightradar24.com`), used to verify our recorded ADS-B against an external reference. `GET /flight/{flight_id}/fr24` resolves the flight by ICAO hex (`hex_id`, most frequent value) + flight date, fetches the track, and normalizes it to our trajectory schema (shared field names: `ground_speed`, `altitude`, `track`, `rate_climb`). Auth is a bearer token from the `FR24_API_TOKEN` env var; the endpoint returns 503 (config), 404 (no match), or 502 (upstream) as JSON `{error}` so the UI can show it inline. The two FR24-specific calls (`_resolve_fr24_id`, `_fetch_track`) are isolated so endpoint/field specifics can be tuned per plan tier without live testing. Note FR24 publishes **ground speed**, not airspeed — there is no direct `air_speed` reference.

**Frontend:** plain HTML templates in `templates/` with inline CSS/JS, no build step. `flight.html` uses Leaflet (CDN) for the map with a hover legend; `graphs.html` uses Plotly (CDN). Both fetch the trajectory endpoint client-side. The map page right-side sidebar stacks: flight-data legend, an **ADS-B data-quality** card (computed client-side from the trajectory — coverage/volume, frozen-run & zero checks, plausible-range, and air_speed-vs-ground_speed agreement, each scored OK/Check/Bad), a **FlightRadar comparison** card (hidden until the header "Compare with FlightRadar" button loads `/fr24`; overlays the FR24 track dashed-orange and diffs shared fields against time-aligned local points), and the safety-events panel.

## Data directory notes

`data/` contains large raw dumps and `data/parsed_old/` legacy files — don't load these into context wholesale. `data/parsed/` is the only directory the app reads.
