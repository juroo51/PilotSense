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
import numpy as np
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


# Flights can come from two kinds of log. The PilotSense device writes the
# heterogeneous GPS/IMU/ADS-B NDJSON this app grew up on; a Garmin G1000 data
# card is converted by tools/parse_garmin.py into the same NDJSON container,
# with a leading {"_meta": {...}} record marking the source. Everything
# downstream (pre-processing, safety analysis, map, graphs) is shared.
GARMIN_SOURCE = "garmin_g1000"
DEVICE_SOURCE = "pilotsense_device"


def read_flight_meta(flight_id: str) -> dict:
    """The `_meta` record a converted log starts with, or {} for device logs.

    Reads only the first line, so it is cheap enough for the index to call on
    every flight.
    """
    file_path = PARSED_DIR / f"{flight_id}.json"
    if not file_path.is_file():
        return {}
    try:
        with open(file_path, "r") as f:
            first = f.readline().strip()
    except OSError:
        return {}
    if not first:
        return {}
    record, _ = _parse_log_line(first)
    if not record:
        return {}
    meta = record.get("_meta")
    return meta if isinstance(meta, dict) else {}


def flight_source(flight_id: str) -> str:
    """Which kind of log a flight came from — drives labels and index styling."""
    return read_flight_meta(flight_id).get("source") or DEVICE_SOURCE


def _load_garmin_flight(file_path: Path) -> pd.DataFrame:
    """Read a converted Garmin log (see tools/parse_garmin.py) into a DataFrame.

    Samples are already normalized at conversion time, so this is a straight
    read: rename t/lat/lon to the canonical timestamp/latitude/longitude the
    rest of the app expects and leave every other field as written.
    """
    rows = []
    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record, _ = _parse_log_line(line)
            if record is None or "_meta" in record:
                continue
            row = dict(record)
            row["timestamp"] = row.pop("t", None)
            if "lat" in row and "lon" in row:
                row["latitude"] = row.pop("lat")
                row["longitude"] = row.pop("lon")
            else:
                # No fix on this sample — the detectors and trajectory both
                # need a position, so there is nothing to place it on.
                row.pop("lat", None)
                row.pop("lon", None)
                continue
            rows.append(row)

    df = pd.DataFrame(rows)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce", utc=True)
    return df


def load_flight_data(flight_id: str) -> pd.DataFrame:
    file_path = PARSED_DIR / f"{flight_id}.json"
    if not file_path.is_file():
        return pd.DataFrame()

    if read_flight_meta(flight_id).get("source") == GARMIN_SOURCE:
        return _load_garmin_flight(file_path)

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

# Garmin G1000 channels. These sit alongside FRIENDLY_NAMES rather than in it
# because a few names are shared with the device format but mean something
# slightly different there (e.g. `altitude` is ADS-B baro altitude on a device
# log, baro-corrected MSL on a Garmin one). The overlay is applied at serve
# time for Garmin flights only — see _labels_for().
GARMIN_LABELS = {
    "altitude": "Altitude MSL (ft)",
    "alt_baro": "Indicated Altitude (ft)",
    "alt_gps": "GPS Altitude (ft WGS84)",
    "baro_setting": "Altimeter Setting (inHg)",
    "oat": "Outside Air Temp (°C)",
    "rate_climb": "Vertical Speed (ft/min)",
    "vspeed_gps": "Vertical Speed, GPS (ft/min)",
    "lat_accel": "Lateral Acceleration (G)",
    "norm_accel": "Normal Acceleration (G from 1 G)",
    "mag_var": "Magnetic Variation (°)",
    "volt1": "Bus 1 Voltage (V)",
    "volt2": "Bus 2 Voltage (V)",
    "fuel_qty_left": "Fuel Quantity, Left (gal)",
    "fuel_qty_right": "Fuel Quantity, Right (gal)",
    "fuel_qty_total": "Fuel on Board (gal)",
    "e1_fuel_flow": "Eng 1 Fuel Flow (gph)",
    "e1_fuel_press": "Eng 1 Fuel Pressure (psi)",
    "e1_oil_temp": "Eng 1 Oil Temperature (°F)",
    "e1_oil_press": "Eng 1 Oil Pressure (psi)",
    "e1_rpm": "Eng 1 RPM",
    "e1_power_pct": "Eng 1 Power (%)",
    "e2_fuel_flow": "Eng 2 Fuel Flow (gph)",
    "e2_fuel_press": "Eng 2 Fuel Pressure (psi)",
    "e2_oil_temp": "Eng 2 Oil Temperature (°F)",
    "e2_oil_press": "Eng 2 Oil Pressure (psi)",
    "e2_rpm": "Eng 2 RPM",
    "e2_power_pct": "Eng 2 Power (%)",
    "active_waypoint": "Active Waypoint",
    "wpt_distance": "Distance to Waypoint (nm)",
    "wpt_bearing": "Bearing to Waypoint (°)",
    "hsi_source": "HSI Source",
    "selected_course": "Selected Course (°)",
    "hcdi": "Lateral Deviation (HCDI)",
    "vcdi": "Vertical Deviation (VCDI)",
    "nav1": "NAV1 Frequency (MHz)",
    "nav2": "NAV2 Frequency (MHz)",
    "com1": "COM1 Frequency (MHz)",
    "com2": "COM2 Frequency (MHz)",
    "wind_speed": "Wind Speed (kt)",
    "wind_dir": "Wind Direction (°)",
    "afcs_on": "Autopilot Engaged",
    "roll_mode": "Autopilot Roll Mode",
    "pitch_mode": "Autopilot Pitch Mode",
    "roll_command": "Autopilot Roll Command (°)",
    "pitch_command": "Autopilot Pitch Command (°)",
    "gps_fix": "GPS Fix Type",
    "gnss_hal": "Horizontal Alert Limit (m)",
    "gnss_hpl_was": "Horizontal Protection Level, WAAS (m)",
    "gnss_vpl_was": "Vertical Protection Level, WAAS (m)",
    "gnss_hpl_fd": "Horizontal Protection Level, Fault Detection (m)",
}


def _labels_for(flight_id: str) -> dict:
    """Friendly labels for one flight, with the Garmin overlay where it applies."""
    if flight_source(flight_id) == GARMIN_SOURCE:
        return {**FRIENDLY_NAMES, **GARMIN_LABELS}
    return FRIENDLY_NAMES


# ------------------ GROUPED GRAPHS ------------------

GROUP_ACCEL = ["accX", "accY", "accZ"]
GROUP_ATTITUDE = ["pitch", "roll", "yaw"]

# ADS-B XYZ-style groups (if present)
GROUP_ADSB_POSITION = ["latitude", "longitude"]
GROUP_ADSB_MOVEMENT = ["ground_speed", "track", "heading"]

# Garmin G1000 groups. Channels that belong on one chart because they are the
# same quantity from different sources (three altitudes, three speeds) or a
# left/right pair that only means something compared side by side (the two
# engines). Groups whose fields are absent are skipped by the graphs page, so
# these are harmless on device flights.
GROUP_G_ALTITUDE = ["altitude", "alt_baro", "alt_gps"]
GROUP_G_SPEED = ["air_speed", "true_air_speed", "ground_speed"]
GROUP_G_VSPEED = ["rate_climb", "vspeed_gps"]
GROUP_G_ACCEL = ["lat_accel", "norm_accel"]
GROUP_G_RPM = ["e1_rpm", "e2_rpm"]
GROUP_G_OIL_TEMP = ["e1_oil_temp", "e2_oil_temp"]
GROUP_G_PRESSURE = ["e1_oil_press", "e2_oil_press", "e1_fuel_press", "e2_fuel_press"]
GROUP_G_POWER = ["e1_power_pct", "e2_power_pct"]
GROUP_G_FUEL = ["fuel_qty_left", "fuel_qty_right", "fuel_qty_total"]
GROUP_G_FUEL_FLOW = ["e1_fuel_flow", "e2_fuel_flow"]
GROUP_G_VOLTS = ["volt1", "volt2"]
GROUP_G_AP = ["roll", "roll_command", "pitch", "pitch_command"]

# Raw IMU channels never sent to the UI.
HIDDEN_FIELDS = {"gyroX", "gyroY", "gyroZ", "magX", "magY", "magZ"}


def _trajectory_groups() -> dict:
    """Static field-grouping config shared by the map and graphs pages."""
    return {
        "accel": GROUP_ACCEL,
        "att": GROUP_ATTITUDE,
        "adsb_position": GROUP_ADSB_POSITION,
        "adsb_movement": GROUP_ADSB_MOVEMENT,
        "g_altitude": GROUP_G_ALTITUDE,
        "g_speed": GROUP_G_SPEED,
        "g_vspeed": GROUP_G_VSPEED,
        "g_accel": GROUP_G_ACCEL,
        "g_rpm": GROUP_G_RPM,
        "g_oil_temp": GROUP_G_OIL_TEMP,
        "g_pressure": GROUP_G_PRESSURE,
        "g_power": GROUP_G_POWER,
        "g_fuel": GROUP_G_FUEL,
        "g_fuel_flow": GROUP_G_FUEL_FLOW,
        "g_volts": GROUP_G_VOLTS,
        "g_ap": GROUP_G_AP,
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


def _fmt_duration(seconds) -> str | None:
    """Seconds → "1h 57m" / "42m", for the index cards."""
    if not seconds or seconds <= 0:
        return None
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def _summarize(flight_id: str, df: pd.DataFrame, trajectory: list, events: list,
               raw_path: Path, meta: dict | None = None) -> dict:
    """Compact, display-ready flight card data — written as a tiny sidecar so
    the index never has to parse the (potentially large) full cache."""
    by_severity = {"notice": 0, "caution": 0, "danger": 0}
    names = {1: "notice", 2: "caution", 3: "danger"}
    for e in events:
        key = names.get(e.get("severity"))
        if key:
            by_severity[key] += 1

    date_str = t_start = t_end = None
    duration_s = None
    if "timestamp" in df.columns:
        ts = df["timestamp"].dropna()
        if len(ts):
            tmin, tmax = ts.min(), ts.max()
            date_str = tmin.strftime("%d %b %Y")
            t_start = tmin.strftime("%H:%M")
            t_end = tmax.strftime("%H:%M")
            duration_s = (tmax - tmin).total_seconds()

    def _num_max(col):
        if col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if not s.empty:
                return round(float(s.max()), 1)
        return None

    def _mode_str(col):
        if col in df.columns:
            s = df[col].dropna()
            if not s.empty:
                return str(s.mode().iloc[0])
        return None

    meta = meta or {}
    points = len(trajectory)
    return {
        "source": meta.get("source") or DEVICE_SOURCE,
        "airframe": meta.get("airframe"),
        "origin_ident": meta.get("origin_ident"),
        "source_file": meta.get("source_file"),
        "points": points,
        "points_str": f"{points:,}",
        "date": date_str,
        "time_start": t_start,
        "time_end": t_end,
        "duration_str": _fmt_duration(duration_s),
        "callsign": _mode_str("callsign"),
        "hex_id": _mode_str("hex_id"),
        "max_altitude": _num_max("altitude"),
        "max_ground_speed": _num_max("ground_speed"),
        "events": {"total": len(events), "by_severity": by_severity},
        "source_mtime": raw_path.stat().st_mtime if raw_path.is_file() else None,
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)  # atomic: never serve a half-written file


def extract_adsb_message_points(flight_id: str) -> list:
    """Position each ADS-B message along the GPS track by its place in the log.

    ADS-B log lines carry no position or timestamp of their own, so every
    ADS-B message is placed by linearly interpolating between the two GPS
    fixes that bracket it in the raw stream (leading/trailing runs snap to the
    nearest fix). Returns [{lat, lon, timestamp}, ...] in log order — one entry
    per ADS-B message received.
    """
    file_path = PARSED_DIR / f"{flight_id}.json"
    if not file_path.is_file():
        return []

    points = []
    prev = None   # last GPS fix: {"lat", "lon", "t" (epoch s), "ts" (iso)}
    pending = 0   # ADS-B messages seen since the last GPS fix

    def _emit(next_fix):
        nonlocal pending
        if pending == 0:
            return
        if prev is not None and next_fix is not None:
            for j in range(1, pending + 1):
                f = j / (pending + 1)
                t = prev["t"] + (next_fix["t"] - prev["t"]) * f
                points.append({
                    "lat": round(prev["lat"] + (next_fix["lat"] - prev["lat"]) * f, 6),
                    "lon": round(prev["lon"] + (next_fix["lon"] - prev["lon"]) * f, 6),
                    "timestamp": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                })
        else:
            anchor = prev or next_fix   # no bracket → snap to the one fix we have
            if anchor is not None:
                for _ in range(pending):
                    points.append({"lat": round(anchor["lat"], 6),
                                   "lon": round(anchor["lon"], 6),
                                   "timestamp": anchor["ts"]})
        pending = 0

    with open(file_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record, _ = _parse_log_line(line)
            if record is None:
                continue

            is_gps_fix = (
                "Gps_lat" in record
                and "Gps_lon" in record
                and record.get("Gps_data") != "no_data"
            )
            if not is_gps_fix and "Adsb_HexId" in record:
                pending += 1
                continue

            if is_gps_fix:
                lat = _nmea_to_decimal(record.get("Gps_lat"), record.get("Gps_latsign"))
                lon = _nmea_to_decimal(record.get("Gps_lon"), record.get("Gps_lonsign"))
                ts = _parse_gps_timestamp(record.get("Gps_datum"), record.get("Gps_time"))
                if lat is None or lon is None or pd.isna(ts):
                    continue
                fix = {"lat": lat, "lon": lon, "t": ts.timestamp(), "ts": ts.isoformat()}
                _emit(fix)      # distribute ADS-B seen since prev between prev and this fix
                prev = fix

    _emit(None)                 # trailing ADS-B → snap to the last fix
    return points


# A gap counts as an outage when it exceeds both an absolute floor and a
# multiple of the source's own median cadence (so a fast source needs a
# proportionally longer silence to qualify). Tune here.
OUTAGE_MIN_SECONDS = 8.0
OUTAGE_GAP_FACTOR = 6.0
_OUTAGE_MAX_PATH = 300  # cap points per drawn ADS-B outage segment


def _iso_from_epoch(t) -> str:
    return datetime.fromtimestamp(float(t), timezone.utc).isoformat()


def compute_coverage_outages(df: pd.DataFrame, adsb_messages: list) -> dict:
    """Find where each source went silent long enough to be a coverage hole.

    Returns {"gps": [...], "adsb": [...]}, each item {start, end, duration_s,
    path}. GPS outages have no fixes in the gap, so the path is the straight
    jump from the last fix to the next; ADS-B outages are drawn along the GPS
    path actually flown while ADS-B was silent, which is what shows *where*
    coverage was missing.
    """
    result = {"gps": [], "adsb": []}
    if df.empty:
        return result
    d2 = df.dropna(subset=["timestamp", "latitude", "longitude"]).sort_values("timestamp")
    if len(d2) < 2:
        return result
    gt = d2["timestamp"].astype("int64").to_numpy() / 1e9
    glat = d2["latitude"].to_numpy()
    glon = d2["longitude"].to_numpy()

    def _threshold(intervals):
        if len(intervals) == 0:
            return OUTAGE_MIN_SECONDS
        return max(OUTAGE_MIN_SECONDS, OUTAGE_GAP_FACTOR * float(np.median(intervals)))

    # ---- GPS: gap between consecutive fixes (no position known during it) ----
    gd = np.diff(gt)
    gthr = _threshold(gd)
    for i in np.flatnonzero(gd > gthr):
        result["gps"].append({
            "start": _iso_from_epoch(gt[i]),
            "end": _iso_from_epoch(gt[i + 1]),
            "duration_s": round(float(gd[i]), 1),
            "path": [
                [round(float(glat[i]), 6), round(float(glon[i]), 6)],
                [round(float(glat[i + 1]), 6), round(float(glon[i + 1]), 6)],
            ],
        })

    # ---- ADS-B: gap between messages, drawn along the GPS path flown ----
    if len(adsb_messages) >= 2:
        at = np.array([datetime.fromisoformat(m["timestamp"]).timestamp()
                       for m in adsb_messages])
        ad = np.diff(at)
        athr = _threshold(ad)
        for i in np.flatnonzero(ad > athr):
            t0, t1 = at[i], at[i + 1]
            mask = (gt >= t0) & (gt <= t1)
            path = [[round(float(la), 6), round(float(lo), 6)]
                    for la, lo in zip(glat[mask], glon[mask])]
            if len(path) > _OUTAGE_MAX_PATH:
                step = len(path) // _OUTAGE_MAX_PATH + 1
                path = path[::step] + [path[-1]]
            if len(path) < 2:  # no GPS fixes in the gap — fall back to endpoints
                path = [[adsb_messages[i]["lat"], adsb_messages[i]["lon"]],
                        [adsb_messages[i + 1]["lat"], adsb_messages[i + 1]["lon"]]]
            result["adsb"].append({
                "start": _iso_from_epoch(t0),
                "end": _iso_from_epoch(t1),
                "duration_s": round(float(ad[i]), 1),
                "path": path,
            })
    return result


def process_flight(flight_id: str) -> dict:
    """Run the full pipeline for one flight and cache the result to disk.

    Writes data/processed/<id>.json (trajectory, field list, detected safety
    events, source mtime for staleness) plus a small <id>.summary.json the
    index reads. Called on ingest and by the manual /process endpoint only.
    """
    meta = read_flight_meta(flight_id)
    df = load_flight_data(flight_id)

    payload = build_trajectory_payload(df)
    events = [] if df.empty else analyze_flight(df.sort_values("timestamp"))["events"]

    adsb_messages = extract_adsb_message_points(flight_id)

    raw_path = PARSED_DIR / f"{flight_id}.json"
    cache = {
        "flight_id": flight_id,
        "source": meta.get("source") or DEVICE_SOURCE,
        "meta": meta,
        "trajectory": payload["trajectory"],
        "fields": payload["fields"],
        "events": events,
        "adsb_messages": adsb_messages,
        "outages": compute_coverage_outages(df, adsb_messages),
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "source_mtime": raw_path.stat().st_mtime if raw_path.is_file() else None,
    }
    summary = _summarize(flight_id, df, payload["trajectory"], events, raw_path, meta)

    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(PROCESSED_DIR / f"{flight_id}.json", cache)
    _atomic_write_json(PROCESSED_DIR / f"{flight_id}.summary.json", summary)
    return cache


def load_summary(flight_id: str) -> dict | None:
    """Read a flight's small summary sidecar, or None if not processed."""
    path = PROCESSED_DIR / f"{flight_id}.summary.json"
    if not path.is_file():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


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
    """Flight ids with processing state + summary, for the index page.

    Reads only the small summary sidecar (never the full cache), so the index
    stays fast even for flights with tens of thousands of points.
    """
    out = []
    for fid in list_flights():
        processed = (PROCESSED_DIR / f"{fid}.json").is_file()
        summary = load_summary(fid) if processed else None

        stale = False
        if processed and summary and summary.get("source_mtime") is not None:
            raw = PARSED_DIR / f"{fid}.json"
            if raw.is_file() and raw.stat().st_mtime > summary["source_mtime"] + 1e-6:
                stale = True

        out.append({
            "id": fid,
            "processed": processed,
            "stale": stale,
            "summary": summary,
            # Read from the raw file, so unprocessed cards are styled correctly too.
            "source": flight_source(fid),
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
    static = {"labels": _labels_for(flight_id), "groups": _trajectory_groups()}

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
        "adsb_messages": cache.get("adsb_messages", []),
        "outages": cache.get("outages", {"gps": [], "adsb": []}),
        "source": cache.get("source", DEVICE_SOURCE),
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
