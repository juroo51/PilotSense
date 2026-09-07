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

# Import a Garmin G1000 data card (one CSV per power-on cycle in data_log/)
python tools/parse_garmin.py /path/to/data_log         # converts + processes
python tools/parse_garmin.py /path/to/data_log --dry-run
python tools/parse_garmin.py /path/to/data_log --min-points 600   # flights only

# Generate a synthetic flight that triggers safety events at every severity
python tools/make_demo_flight.py   # writes data/parsed/DEMO-EVENTS.json

# Pre-process flights into the view cache (trajectory + safety events).
# Run once to bootstrap existing flights, or any time to rebuild caches.
python tools/process.py            # all flights; or pass flight ids to limit
```

There are no tests or linters configured.

## Architecture

Server-side code is [app.py](app.py) (routes, data loading) and [analysis.py](analysis.py) (safety-event detection).

**Safety analysis:** `analysis.py` runs detectors (`DETECTORS` list) over the merged flight DataFrame and emits events with severity 1–3 (Notice/Caution/Danger, colors in `SEVERITY_LEVELS`). Each detector groups consecutive exceedances into one event with a peak value and a human-readable `summary`. `GET /flight/{flight_id}/events` serves them; the map shows severity-colored markers plus a clickable events panel, the graphs page shades event time bands on every chart and lists explanations in a summary card. Thresholds are calibrated for light aircraft — tune them in the detector docstrings/code together.

**Data flow:** raw NDJSON logs → `tools/parse.py` splits them by the `f` (flight/callsign) field into `data/parsed/<FLIGHT_ID>.json` → **pre-processing** (`process_flight()` in app.py) merges + rounds the trajectory and runs the safety detectors once, caching the result to `data/parsed`'s sibling `data/processed/<FLIGHT_ID>.json`. The flight list on the index page is the filenames in `data/parsed/`, each shown with its processing state.

**Pre-processing vs. viewing (important):** the expensive work happens at *load/trigger time*, not view time. `process_flight()` runs (a) automatically when a device ingests new data and (b) on demand via `POST /api/flights/{id}/process` (the index "Process/Reprocess" buttons and the map/graphs "Reprocess" button). The view endpoints (`/trajectory`, `/events`) **only read the cache and never recompute** — if a flight has no cache they return `409` with `needs_processing: true`, and the UI offers a Process button instead of rendering. Static config (friendly labels, groups, severity levels) is injected at serve time, so tweaking it doesn't require reprocessing; a cache is flagged `stale` when the raw file's mtime is newer than the cache. `tools/process.py` bootstraps/rebuilds caches from the CLI. (`GET /flight/{id}/fr24` still loads the raw file directly, since it's a manual, on-click action that needs the source DataFrame.)

**Two data sources.** A flight's `data/parsed/<id>.json` is NDJSON either way, but it comes from one of two kinds of log, and `load_flight_data()` dispatches between them:

- **PilotSense device** (`pilotsense_device`) — the original heterogeneous GPS/IMU/ADS-B format described below. No `_meta` line.
- **Garmin G1000 / NXi** (`garmin_g1000`) — an avionics data card, converted by `tools/parse_garmin.py`. The file starts with a `{"_meta": {...}}` record (source, airframe, system id, origin ident, source filename, units) and every following line is one already-normalized 1 Hz sample: `{"t", "lat", "lon", ...fields}`.

`read_flight_meta()` / `flight_source()` read that first line (cheap — one `readline`) and are what the index, the label overlay and the cache all key off. Garmin channels reuse the canonical field names where the meaning matches (`altitude`, `ground_speed`, `air_speed`, `true_air_speed`, `track`, `heading`, `pitch`, `roll`, `rate_climb`), so the safety detectors, the index summary and the map's metric colouring work on both sources unchanged; `GARMIN_LABELS` overrides `FRIENDLY_NAMES` at serve time for the handful whose meaning differs (Garmin `altitude` is baro-corrected MSL, not ADS-B baro altitude). Garmin logs have no ADS-B stream, so `adsb_messages` is empty, the map's **ADS-B data-quality** card is hidden for them, and `GET /fr24` returns 404. Radio frequencies are stored as strings so 8.33 kHz spacing survives the payload's 2-decimal rounding. On the index, Garmin cards are violet with a `G1000` badge and a dial icon; device cards stay blue with an `ADS-B` badge — the accent is one set of CSS custom properties per `.card[data-source]`.

**Data format:** each PilotSense-device per-flight file is NDJSON, one JSON object per line. Lines are heterogeneous — a line may carry GPS fields (`Gps_lat`, `Gps_lon`, `Gps_datum`, `Gps_time`, ...), ADS-B fields (`Adsb_HexId`, `Adsb_alt`, `Adsb_callsign`, ...), and/or IMU fields (`accX`, `pitch`, ...). `load_flight_data()` in app.py merges these into one timeline: it accumulates the latest ADS-B state across lines and emits a DataFrame row only when a line contains a GPS fix, attaching the last-known ADS-B values to it.

**Data quirks handled in `load_flight_data()`:**
- Some log lines contain `"Adsb_HexId": 505CE5` (unquoted hex), which is invalid JSON — a regex repair quotes it before retrying the parse.
- GPS coordinates are in NMEA `DDMM.MMMM` format with separate sign fields (`Gps_latsign`/`Gps_lonsign`), converted via `_nmea_to_decimal()`.
- GPS timestamps use a two-digit-year format `DD-MM-YY HH:MM:SS` (UTC).
- Lines with `Gps_data: "no_data"` carry lat/lon `0.0` and are skipped.

**Data ingestion:** `POST /api/flights/{flight_id}/data` accepts a batch of raw NDJSON log lines from authorized devices and appends them to `data/parsed/<flight_id>.json`. Auth is per-device via `X-Device-Id` + `X-Api-Key` headers, checked against the `PILOTSENSE_DEVICE_KEYS` env var (`"device1:key1,device2:key2"`); the endpoint returns 503 when no keys are configured. Lines are validated with the same lenient parser (`_parse_log_line`) the loader uses; the response reports accepted/rejected counts. After a successful append the endpoint **reprocesses the flight** (rebuilding its `data/processed/` cache) and reports the resulting point/event counts under `processed`.

**Message points & coverage outages:** the trajectory payload also carries `adsb_messages` (one interpolated point per ADS-B message — ADS-B log lines have no position/time of their own, so each is placed by interpolating between the GPS fixes that bracket it in the raw stream; see `extract_adsb_message_points()`) and `outages` (`{gps, adsb}`, gaps where a source went silent longer than `max(8s, 6× median interval)`; see `compute_coverage_outages()`). The map legend exposes these as extra rows: **GPS/ADSB messages** (◇) replaces the track with per-message receipt dots (hover for time), and **GPS/ADSB outages** (⚠) draws a faint reference track with the silent stretches in red — ADS-B outages along the flown GPS path (shows *where* coverage was missing), GPS outages as the straight jump between the last fix and reacquisition. These, metric-coloring, and the default line are mutually exclusive view modes.

**Single JSON API:** `GET /flight/{flight_id}/trajectory` returns `{trajectory, fields, labels, groups, adsb_messages, outages}`. Both frontends (map and graphs) consume this same endpoint:
- `trajectory` is a list of points (lat/lon/timestamp plus every value field, floats rounded to 2 decimals, NaN/inf → null).
- `labels` comes from the `FRIENDLY_NAMES` dict in app.py — add an entry there when introducing a new field so the UI shows a human-readable name.
- `groups` (`accel`, `att`, `adsb_position`, `adsb_movement`) drives which fields are plotted together on the graphs page; ungrouped fields each get their own chart.
- Fields in `HIDDEN_FIELDS` (raw gyro/magnetometer) are excluded from the API output.

**FlightRadar comparison:** [fr24_client.py](fr24_client.py) is a thin client for the official Flightradar24 API (`fr24api.flightradar24.com`), used to verify our recorded ADS-B against an external reference. `GET /flight/{flight_id}/fr24` resolves the flight by ICAO hex (`hex_id`, most frequent value) + flight date, fetches the track, and normalizes it to our trajectory schema (shared field names: `ground_speed`, `altitude`, `track`, `rate_climb`). Auth is a bearer token from the `FR24_API_TOKEN` env var; the endpoint returns 503 (config), 404 (no match), or 502 (upstream) as JSON `{error}` so the UI can show it inline. The two FR24-specific calls (`_resolve_fr24_id`, `_fetch_track`) are isolated so endpoint/field specifics can be tuned per plan tier without live testing. Note FR24 publishes **ground speed**, not airspeed — there is no direct `air_speed` reference.

**Frontend:** plain HTML templates in `templates/` with inline CSS/JS, no build step. `flight.html` uses Leaflet (CDN) for the map with a hover legend; `graphs.html` uses Plotly (CDN). Both fetch the trajectory endpoint client-side. The map page right-side sidebar stacks: flight-data legend, an **ADS-B data-quality** card (computed client-side from the trajectory — coverage/volume, frozen-run & zero checks, plausible-range, and air_speed-vs-ground_speed agreement, each scored OK/Check/Bad), a **FlightRadar comparison** card (hidden until the header "Compare with FlightRadar" button loads `/fr24`; overlays the FR24 track dashed-orange and diffs shared fields against time-aligned local points), and the safety-events panel.

## Data directory notes

`data/` contains large raw dumps and `data/parsed_old/` legacy files — don't load these into context wholesale. `data/parsed/` is the only directory the app reads.
