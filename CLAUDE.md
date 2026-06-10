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
```

There are no tests or linters configured.

## Architecture

Everything server-side lives in [app.py](app.py); there are no other Python modules in the app itself.

**Data flow:** raw NDJSON logs → `tools/parse.py` splits them by the `f` (flight/callsign) field into `data/parsed/<FLIGHT_ID>.json` → `app.py` reads those files on every request (no database, no caching). The flight list on the index page is simply the filenames in `data/parsed/`.

**Data format:** each per-flight file is NDJSON, one JSON object per line. Lines are heterogeneous — a line may carry GPS fields (`Gps_lat`, `Gps_lon`, `Gps_datum`, `Gps_time`, ...), ADS-B fields (`Adsb_HexId`, `Adsb_alt`, `Adsb_callsign`, ...), and/or IMU fields (`accX`, `pitch`, ...). `load_flight_data()` in app.py merges these into one timeline: it accumulates the latest ADS-B state across lines and emits a DataFrame row only when a line contains a GPS fix, attaching the last-known ADS-B values to it.

**Data quirks handled in `load_flight_data()`:**
- Some log lines contain `"Adsb_HexId": 505CE5` (unquoted hex), which is invalid JSON — a regex repair quotes it before retrying the parse.
- GPS coordinates are in NMEA `DDMM.MMMM` format with separate sign fields (`Gps_latsign`/`Gps_lonsign`), converted via `_nmea_to_decimal()`.
- GPS timestamps use a two-digit-year format `DD-MM-YY HH:MM:SS` (UTC).
- Lines with `Gps_data: "no_data"` carry lat/lon `0.0` and are skipped.

**Single JSON API:** `GET /flight/{flight_id}/trajectory` returns `{trajectory, fields, labels, groups}`. Both frontends (map and graphs) consume this same endpoint:
- `trajectory` is a list of points (lat/lon/timestamp plus every value field, floats rounded to 2 decimals, NaN/inf → null).
- `labels` comes from the `FRIENDLY_NAMES` dict in app.py — add an entry there when introducing a new field so the UI shows a human-readable name.
- `groups` (`accel`, `att`, `adsb_position`, `adsb_movement`) drives which fields are plotted together on the graphs page; ungrouped fields each get their own chart.
- Fields in `HIDDEN_FIELDS` (raw gyro/magnetometer) are excluded from the API output.

**Frontend:** plain HTML templates in `templates/` with inline CSS/JS, no build step. `flight.html` uses Leaflet (CDN) for the map with a hover legend; `graphs.html` uses Plotly (CDN). Both fetch the trajectory endpoint client-side.

## Data directory notes

`data/` contains large raw dumps and `data/parsed_old/` legacy files — don't load these into context wholesale. `data/parsed/` is the only directory the app reads.
