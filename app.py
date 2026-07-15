import json
import os
import re
import secrets
from datetime import datetime, timezone
from math import isnan
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd

from analysis import SEVERITY_LEVELS, analyze_flight
import fr24_client

PARSED_DIR = Path("data/parsed")

# Pre-processed per-flight caches (trajectory + safety events). These are
# (re)built when data is ingested or the /process endpoint is called — never
# on a plain view request, which only reads what is already here.
PROCESSED_DIR = Path("data/processed")

# Max accepted upload size per request (bytes)
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _load_device_keys():
    """Per-device API keys from PILOTSENSE_DEVICE_KEYS="dev1:key1,dev2:key2"."""
    keys = {}
    for pair in os.environ.get("PILOTSENSE_DEVICE_KEYS", "").split(","):
        if ":" in pair:
            device_id, key = pair.split(":", 1)
            if device_id.strip() and key.strip():
                keys[device_id.strip()] = key.strip()
    return keys


DEVICE_KEYS = _load_device_keys()

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


def list_flights():
    return sorted([f.stem for f in PARSED_DIR.glob("*.json")])


def _nmea_to_decimal(raw_value, sign):
    """Convert DDMM.MMMM / DDDMM.MMMM to decimal degrees."""
    if raw_value is None:
        return None
    try:
        raw = float(raw_value)
    except (TypeError, ValueError):
        return None

    degrees = int(raw // 100)
    minutes = raw - (degrees * 100)
    decimal = degrees + (minutes / 60.0)

    if str(sign).upper() in {"S", "W"}:
        decimal *= -1
    return decimal


def _parse_gps_timestamp(gps_date, gps_time):
    """PilotSense date format is DD-MM-YY and time is HH:MM:SS."""
    if not gps_date or not gps_time:
        return pd.NaT
    try:
        dt = datetime.strptime(f"{gps_date} {gps_time}", "%d-%m-%y %H:%M:%S")
        return pd.Timestamp(dt.replace(tzinfo=timezone.utc))
    except ValueError:
        return pd.NaT


def _parse_log_line(line: str):
    """Parse one PilotSense NDJSON log line, tolerating unquoted Adsb_HexId.

    Returns (record_dict, normalized_line) or (None, None) if unparseable.
    """
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        # Some ADS-B log lines emit Adsb_HexId as an unquoted hex literal
        # (e.g. "Adsb_HexId": 505CE5), which isn't valid JSON.
        fixed = re.sub(
            r'("Adsb_HexId":\s*)([0-9A-Fa-f]+)\b',
            r'\1"\2"',
            line,
        )
        try:
            record = json.loads(fixed)
        except json.JSONDecodeError:
            return None, None
        line = fixed
    if not isinstance(record, dict):
        return None, None
    return record, line


def load_flight_data(flight_id: str) -> pd.DataFrame:
    file_path = PARSED_DIR / f"{flight_id}.json"
    if not file_path.is_file():
        return pd.DataFrame()

    rows = []
    last_adsb = {}

    def update_adsb_state(record: dict):
        updates = {}

        if "Adsb_HexId" in record:
            updates["hex_id"] = str(record["Adsb_HexId"]).strip()
        if "Adsb_Fs" in record:
            updates["aircraft_status"] = record["Adsb_Fs"]
        if "Adsb_squawk" in record:
            updates["squawk"] = record["Adsb_squawk"]
        if "Adsb_alt" in record:
            updates["altitude"] = record["Adsb_alt"]
        if "Adsb_alt_type" in record:
            updates["altitude_unit"] = record["Adsb_alt_type"]
        if "Adsb_msg" in record:
            updates["adsb_msg_type"] = record["Adsb_msg"]
        if "Adsb_gnd_speed" in record:
            updates["ground_speed"] = record["Adsb_gnd_speed"]
        # ADS-B reports indicated (IAS) and true (TAS) airspeed as separate
        # fields. Indicated airspeed is the pilot-facing "air speed"; keep
        # true airspeed alongside it. (Older logs used a single Adsb_air_speed.)
        if "Adsb_air_speed" in record:
            updates["air_speed"] = record["Adsb_air_speed"]
        if "Adsb_indic_air_speed" in record:
            updates["air_speed"] = record["Adsb_indic_air_speed"]
        if "Adsb_true_air_speed" in record:
            updates["true_air_speed"] = record["Adsb_true_air_speed"]
        if "Adsb_callsign" in record:
            callsign = str(record["Adsb_callsign"]).strip()
            if callsign:
                updates["callsign"] = callsign

        for k, v in updates.items():
            if v is not None:
                last_adsb[k] = v

    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            record, _ = _parse_log_line(line)
            if record is None:
                continue

            update_adsb_state(record)

            # Build one timeline row whenever a valid GPS sample is present.
            # Lines with Gps_data == "no_data" carry lat/lon 0.0 and no usable fix.
            if (
                "Gps_lat" in record
                and "Gps_lon" in record
                and record.get("Gps_data") != "no_data"
            ):
                lat = _nmea_to_decimal(record.get("Gps_lat"), record.get("Gps_latsign"))
                lon = _nmea_to_decimal(record.get("Gps_lon"), record.get("Gps_lonsign"))
                row = {
                    "timestamp": _parse_gps_timestamp(record.get("Gps_datum"), record.get("Gps_time")),
                    "latitude": lat,
                    "longitude": lon,
                    "gps_speed": record.get("Gps_speed"),
                    "gps_altitude": record.get("Gps_alt"),
                    "track": record.get("Gps_tr"),
                    "gps_status": record.get("Gps_data"),
                    "gps_lat_raw": record.get("Gps_lat"),
                    "gps_lon_raw": record.get("Gps_lon"),
                }
                row.update(last_adsb)
                rows.append(row)

    df = pd.DataFrame(rows)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)

    return df


# ------------------ FRIENDLY LABELS ------------------

FRIENDLY_NAMES = {
    # Sensor
    "accX": "Acceleration X (m/s²)",
    "accY": "Acceleration Y (m/s²)",
    "accZ": "Acceleration Z (m/s²)",
    "pitch": "Pitch (°)",
    "roll": "Roll (°)",
    "yaw": "Yaw (°)",
    "Pressure": "Pressure",
    "Altitude": "Altitude (Sensor)",

    # ADS-B expanded fields
    "altitude": "Altitude ADS-B",
    "altitude_baro": "Barometric Altitude (ADS-B)",
    "altitude_geom": "Geometric Altitude (ADS-B)",
    "ground_speed": "Ground Speed (kt)",
    "heading": "Heading (°)",
    "track": "Track Angle (°)",
    "gps_speed": "GPS Speed",
    "gps_altitude": "GPS Altitude (m)",
    "gps_status": "GPS Status",
    "gps_lat_raw": "Raw GPS Latitude (DDMM.MMMM)",
    "gps_lon_raw": "Raw GPS Longitude (DDDMM.MMMM)",
    "air_speed": "Indicated Air Speed (kt)",
    "true_air_speed": "True Air Speed (kt)",
    "aircraft_status": "Aircraft Status",
    "altitude_unit": "Altitude Unit",
    "adsb_msg_type": "ADS-B Message Type",
    "hex_id": "Hex ID",
    "latitude": "Latitude (ADS-B)",
    "longitude": "Longitude (ADS-B)",
    "rate_climb": "Vertical Rate (ft/min)",
    "roll_rate": "Roll Rate",
    "squawk": "Squawk",
    "callsign": "Callsign",
}

# ------------------ GROUPED GRAPHS ------------------

GROUP_ACCEL = ["accX", "accY", "accZ"]
GROUP_ATTITUDE = ["pitch", "roll", "yaw"]

# ADS-B XYZ-style groups (if present)
GROUP_ADSB_POSITION = ["latitude", "longitude"]
GROUP_ADSB_MOVEMENT = ["ground_speed", "track", "heading"]

# Raw IMU channels never sent to the UI.
HIDDEN_FIELDS = {"gyroX", "gyroY", "gyroZ", "magX", "magY", "magZ"}


def _trajectory_groups() -> dict:
    """Static field-grouping config shared by the map and graphs pages."""
    return {
        "accel": GROUP_ACCEL,
        "att": GROUP_ATTITUDE,
        "adsb_position": GROUP_ADSB_POSITION,
        "adsb_movement": GROUP_ADSB_MOVEMENT,
    }


# ------------------ PRE-PROCESSING / CACHE ------------------
#
# The expensive work (parsing raw NDJSON, merging, rounding every point, and
# running the safety detectors) happens once here and is cached to disk. View
# requests read the cache; they never rebuild it. Static config (friendly
# labels, group definitions, severity colors) is injected at serve time so
# tweaking it doesn't require reprocessing.

def build_trajectory_payload(df: pd.DataFrame) -> dict:
    """Turn a merged flight DataFrame into the data-dependent trajectory payload.

    Returns {"trajectory": [...points...], "fields": [...value field names...]}.
    Labels/groups are added by the endpoint, not stored, so they stay live.
    """
    if df.empty:
        return {"trajectory": [], "fields": []}

    df = df.sort_values("timestamp")

    all_fields = set(df.columns)
    required = {"latitude", "longitude", "timestamp"}
    value_fields = sorted(
        f for f in all_fields if f not in required and f not in HIDDEN_FIELDS
    )

    def clean(val):
        """NaN / inf → None, JSON-safe."""
        try:
            if val is None:
                return None
            if isinstance(val, float) and (isnan(val) or val in (float("inf"), float("-inf"))):
                return None
            return val
        except Exception:
            return None

    trajectory = []
    for _, row in df.iterrows():
        if pd.isna(row.get("latitude")) or pd.isna(row.get("longitude")):
            continue

        point = {
            "lat": float(row["latitude"]),
            "lon": float(row["longitude"]),
            "timestamp": row["timestamp"].isoformat(),
        }
        for field in value_fields:
            raw = row.get(field)
            if raw is None or pd.isna(raw):
                point[field] = None
            elif isinstance(raw, (int, float)):
                cleaned = clean(raw)
                point[field] = round(cleaned, 2) if cleaned is not None else None
            else:
                point[field] = raw
        trajectory.append(point)

    return {"trajectory": trajectory, "fields": value_fields}


def process_flight(flight_id: str) -> dict:
    """Run the full pipeline for one flight and cache the result to disk.

    Writes data/processed/<id>.json with the trajectory, field list and
    detected safety events, plus the source file's mtime so staleness can be
    detected. Called on ingest and by the manual /process endpoint only.
    """
    df = load_flight_data(flight_id)

    payload = build_trajectory_payload(df)
    events = [] if df.empty else analyze_flight(df.sort_values("timestamp"))["events"]

    raw_path = PARSED_DIR / f"{flight_id}.json"
    cache = {
        "flight_id": flight_id,
        "trajectory": payload["trajectory"],
        "fields": payload["fields"],
        "events": events,
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "source_mtime": raw_path.stat().st_mtime if raw_path.is_file() else None,
    }

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROCESSED_DIR / f"{flight_id}.json.tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f)
    tmp.replace(PROCESSED_DIR / f"{flight_id}.json")  # atomic: never serve a half-written cache
    return cache


def load_processed(flight_id: str) -> dict | None:
    """Read a flight's cached payload, or None if it hasn't been processed."""
    path = PROCESSED_DIR / f"{flight_id}.json"
    if not path.is_file():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _is_stale(flight_id: str, cache: dict) -> bool:
    """True if the raw file changed after the cache was written."""
    raw_path = PARSED_DIR / f"{flight_id}.json"
    if not raw_path.is_file():
        return False
    cached = cache.get("source_mtime")
    if cached is None:
        return True
    return raw_path.stat().st_mtime > cached + 1e-6


def flight_status() -> list:
    """Flight ids with their processing state, for the index page."""
    out = []
    for fid in list_flights():
        cache = load_processed(fid)
        out.append({
            "id": fid,
            "processed": cache is not None,
            "stale": cache is not None and _is_stale(fid, cache),
        })
    return out


# ------------------ DATA INGESTION ------------------

def _sanitize_flight_id(flight_id: str) -> str:
    return "".join(c for c in flight_id if c.isalnum() or c in ("_", "-", "."))


def _authenticate_device(device_id: str, api_key: str):
    if not DEVICE_KEYS:
        raise HTTPException(
            status_code=503,
            detail="Ingestion disabled: PILOTSENSE_DEVICE_KEYS is not configured",
        )
    expected = DEVICE_KEYS.get(device_id)
    if expected is None or not secrets.compare_digest(expected, api_key):
        raise HTTPException(status_code=401, detail="Invalid device credentials")


@app.post("/api/flights/{flight_id}/data")
async def ingest_flight_data(
    flight_id: str,
    request: Request,
    x_device_id: str = Header(...),
    x_api_key: str = Header(...),
):
    """Accept a batch of NDJSON log lines from an authorized device.

    Valid lines are appended to the flight's file; the response reports
    how many lines were accepted vs rejected so devices can detect problems.
    """
    _authenticate_device(x_device_id, x_api_key)

    safe_id = _sanitize_flight_id(flight_id)
    if not safe_id or safe_id != flight_id:
        raise HTTPException(status_code=400, detail="Invalid flight id")

    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Upload too large")
    if not body:
        raise HTTPException(status_code=400, detail="Empty body")

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="Body must be UTF-8 NDJSON")

    accepted_lines = []
    rejected = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        record, normalized = _parse_log_line(line)
        if record is None:
            rejected += 1
            continue
        accepted_lines.append(normalized)

    processed = None
    if accepted_lines:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        with open(PARSED_DIR / f"{safe_id}.json", "a") as f:
            f.write("\n".join(accepted_lines) + "\n")
        # New data loaded → regenerate the cached trajectory + safety events so
        # view pages serve the update without recomputing on every request.
        cache = process_flight(safe_id)
        processed = {
            "points": len(cache["trajectory"]),
            "events": len(cache["events"]),
            "processed_at": cache["processed_at"],
        }

    return {
        "flight_id": safe_id,
        "device_id": x_device_id,
        "accepted": len(accepted_lines),
        "rejected": rejected,
        "processed": processed,
    }


@app.post("/api/flights/{flight_id}/process")
async def process_flight_endpoint(flight_id: str):
    """Manually (re)build a flight's cached trajectory + safety events.

    This is the on-demand trigger: it recomputes from the flight's stored data
    and refreshes the cache the view pages read. No device auth — it only
    reprocesses local data, it doesn't accept new input.
    """
    safe_id = _sanitize_flight_id(flight_id)
    if not safe_id or safe_id != flight_id:
        raise HTTPException(status_code=400, detail="Invalid flight id")
    if not (PARSED_DIR / f"{safe_id}.json").is_file():
        raise HTTPException(status_code=404, detail="No data for this flight")

    cache = process_flight(safe_id)
    return {
        "flight_id": safe_id,
        "points": len(cache["trajectory"]),
        "events": len(cache["events"]),
        "processed_at": cache["processed_at"],
    }


# ------------------ API ENDPOINTS ------------------

@app.get("/flight/{flight_id}/trajectory")
async def flight_trajectory(flight_id: str):
    """Serve the pre-processed trajectory. Does not compute anything: if the
    flight hasn't been processed yet, returns 409 so the UI can offer to
    process it (see POST /api/flights/{id}/process)."""
    static = {"labels": FRIENDLY_NAMES, "groups": _trajectory_groups()}

    cache = load_processed(flight_id)
    if cache is None:
        return JSONResponse(status_code=409, content={
            "error": "Flight not processed yet. Process it before visualizing.",
            "needs_processing": True,
            "trajectory": [],
            "fields": [],
            **static,
        })

    return {
        "trajectory": cache["trajectory"],
        "fields": cache["fields"],
        "stale": _is_stale(flight_id, cache),
        **static,
    }


@app.get("/flight/{flight_id}/events")
async def flight_events(flight_id: str):
    """Serve the pre-processed safety events (see analysis.py). Like the
    trajectory endpoint, this only reads the cache — it never re-runs the
    detectors."""
    cache = load_processed(flight_id)
    if cache is None:
        return JSONResponse(status_code=409, content={
            "error": "Flight not processed yet.",
            "needs_processing": True,
            "events": [],
            "levels": SEVERITY_LEVELS,
        })

    return {"events": cache.get("events", []), "levels": SEVERITY_LEVELS}


@app.get("/flight/{flight_id}/fr24")
async def flight_fr24(flight_id: str, fr24_id: str | None = None):
    """Fetch the same flight from FlightRadar (matched by ICAO hex + date).

    Returns a normalized trajectory (same field names as our own) so the map
    can overlay both tracks and diff shared fields. Errors are returned as
    JSON with the appropriate status so the UI can show them inline.
    """
    df = load_flight_data(flight_id)
    if df.empty:
        return JSONResponse(status_code=404, content={"error": "Flight has no data."})

    df = df.sort_values("timestamp")

    # ICAO hex: most frequent non-null value across the flight.
    hex_id = ""
    if "hex_id" in df.columns:
        hexes = df["hex_id"].dropna()
        if not hexes.empty:
            hex_id = str(hexes.mode().iloc[0])

    # Flight date: first valid GPS timestamp (UTC).
    ts = df["timestamp"].dropna()
    if ts.empty:
        return JSONResponse(
            status_code=422, content={"error": "Flight has no usable timestamps."}
        )
    date_utc = ts.iloc[0].to_pydatetime()

    try:
        result = fr24_client.fetch_flight_track(hex_id, date_utc, fr24_id=fr24_id)
    except fr24_client.Fr24Error as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    result["labels"] = FRIENDLY_NAMES
    result["matched_on"] = {"hex": hex_id, "date": date_utc.date().isoformat()}
    return result


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "flights": flight_status(),
    })


@app.get("/flight/{flight_id}", response_class=HTMLResponse)
async def show_flight(request: Request, flight_id: str):
    return templates.TemplateResponse("flight.html", {
        "request": request,
        "flight_id": flight_id
    })


@app.get("/flight/{flight_id}/graphs", response_class=HTMLResponse)
async def show_graphs(request: Request, flight_id: str):
    return templates.TemplateResponse("graphs.html", {
        "request": request,
        "flight_id": flight_id
    })
