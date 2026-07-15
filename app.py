import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd

from analysis import SEVERITY_LEVELS, analyze_flight
import fr24_client

PARSED_DIR = Path("data/parsed")

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
        if "Adsb_air_speed" in record:
            updates["air_speed"] = record["Adsb_air_speed"]
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
    "gps_status": "GPS Status",
    "gps_lat_raw": "Raw GPS Latitude (DDMM.MMMM)",
    "gps_lon_raw": "Raw GPS Longitude (DDDMM.MMMM)",
    "air_speed": "Air Speed",
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

    if accepted_lines:
        PARSED_DIR.mkdir(parents=True, exist_ok=True)
        with open(PARSED_DIR / f"{safe_id}.json", "a") as f:
            f.write("\n".join(accepted_lines) + "\n")

    return {
        "flight_id": safe_id,
        "device_id": x_device_id,
        "accepted": len(accepted_lines),
        "rejected": rejected,
    }


# ------------------ API ENDPOINTS ------------------

@app.get("/flight/{flight_id}/trajectory")
async def flight_trajectory(flight_id: str):
    df = load_flight_data(flight_id)
    if not df.empty:
        df = df.sort_values("timestamp")

    if df.empty:
        return {
            "trajectory": [],
            "fields": [],
            "labels": FRIENDLY_NAMES,
            "groups": {
                "accel": GROUP_ACCEL,
                "att": GROUP_ATTITUDE,
                "adsb_position": GROUP_ADSB_POSITION,
                "adsb_movement": GROUP_ADSB_MOVEMENT
            }
        }

    # All available fields
    all_fields = set(df.columns)

    # SPECIAL latitude/longitude used by the map
    required = {"latitude", "longitude", "timestamp"}

    # Hidden internal IMU fields
    HIDDEN_FIELDS = {"gyroX", "gyroY", "gyroZ", "magX", "magY", "magZ"}

    # Value fields = everything except timestamp, lat/lon, hidden
    value_fields = sorted([
        f for f in all_fields
        if f not in required and f not in HIDDEN_FIELDS
    ])

    from math import isnan

    def clean(val):
        """Convert NaN / inf → None SAFE for JSON."""
        try:
            if val is None:
                return None
            if isinstance(val, float) and (isnan(val) or val in (float("inf"), float("-inf"))):
                return None
            return val
        except:
            return None


    trajectory = []
    for _, row in df.iterrows():
        # Skip invalid coordinates
        if pd.isna(row.get("latitude")) or pd.isna(row.get("longitude")):
            continue

        point = {
            "lat": float(row["latitude"]),
            "lon": float(row["longitude"]),
            "timestamp": row["timestamp"].isoformat()
        }

        # Add all user-visible fields
        for field in value_fields:
            raw = row.get(field)

            if raw is None or pd.isna(raw):
                point[field] = None
            else:
                # Round floats only
                if isinstance(raw, (int, float)):
                    cleaned = clean(raw)
                    point[field] = round(cleaned, 2) if cleaned is not None else None
                else:
                    point[field] = raw

        trajectory.append(point)


    return {
        "trajectory": trajectory,
        "fields": value_fields,
        "labels": FRIENDLY_NAMES,
        "groups": {
            "accel": GROUP_ACCEL,
            "att": GROUP_ATTITUDE,
            "adsb_position": GROUP_ADSB_POSITION,
            "adsb_movement": GROUP_ADSB_MOVEMENT
        }
    }


@app.get("/flight/{flight_id}/events")
async def flight_events(flight_id: str):
    """Safety events detected in the flight (see analysis.py)."""
    df = load_flight_data(flight_id)
    if df.empty:
        return {"events": [], "levels": SEVERITY_LEVELS}
    return analyze_flight(df.sort_values("timestamp"))


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
    flights = list_flights()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "flights": flights
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
