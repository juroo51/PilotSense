# Changelog

All notable changes to PilotSense are documented here.
This project adheres to [Semantic Versioning](https://semver.org/). While on
`0.x` the app is a prototype and anything may change between releases.

## [0.1.0] - 2026-06-11

First prototype — flight telemetry ingestion, visualization, and safety analysis.

### Added
- **Device data ingestion**: `POST /api/flights/{flight_id}/data` accepts batches
  of raw NDJSON log lines from authorized devices and appends them to the flight
  file. Per-device auth via `X-Device-Id`/`X-Api-Key` headers checked against the
  `PILOTSENSE_DEVICE_KEYS` env var; returns 503 when ingestion is unconfigured.
- **Gradient map**: clickable legend that recolors the trajectory as a blue→red
  gradient by any numeric metric, with a color-scale bar.
- **Safety analysis** (`analysis.py`): detectors for GPS gaps, rapid climb/descent,
  sharp turns, rapid deceleration, and low stall margin, each emitting events at
  three severities (Notice / Caution / Danger) with a plain-language explanation.
  Served at `GET /flight/{flight_id}/events`.
- **Event visualization**: severity-colored markers and a collapsible events panel
  on the map; shaded event time bands and an explanation card on the graphs page.
- `tools/make_demo_flight.py` to generate a synthetic flight that triggers every
  severity level.
- Sample flight `data/parsed/20-04-2026.json` for demos.
- Deployment scaffolding: `Procfile` and `.env.example`.

### Changed
- Reworked the data pipeline: NMEA coordinate conversion, corrected `DD-MM-YY`
  GPS timestamps, forward-filled ADS-B state merged onto GPS rows, and skipping
  of `Gps_data: "no_data"` fixes.
- Trimmed `requirements.txt` to direct dependencies.

### Known limitations
- Analysis runs per request (no caching); thresholds are calibrated for light
  aircraft and the stall-margin detector assumes a fixed stall speed and treats
  GPS ground speed as airspeed — needs per-aircraft calibration before relying on
  its Danger classifications.
- Ingestion has no rate limiting or cumulative size cap.
- No automated tests yet.

[0.1.0]: https://github.com/juroo51/PilotSense/releases/tag/v0.1.0-prototype
