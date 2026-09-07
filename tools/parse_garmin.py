"""Convert Garmin G1000 / G1000 NXi SD-card logs into PilotSense flight files.

The avionics write one CSV per power-on cycle into a `data_log/` folder on the
data card, named `log_YYMMDD_HHMMSS_<IDENT>.csv` (IDENT = nearest airport at
power-up, underscores when there was no GPS fix yet). Each file has three
header lines — airframe info, units, column names — then 1 Hz samples.

This tool normalizes those samples into the same NDJSON shape the rest of the
app reads (`data/parsed/<FLIGHT_ID>.json`), one JSON object per line, with a
leading `{"_meta": {...}}` record that marks the file as Garmin-sourced. The
loader in app.py dispatches on that marker, so Garmin flights flow through the
existing pre-processing, safety analysis, map and graphs unchanged.

    python tools/parse_garmin.py ~/Downloads/zasilka-XXXX/data_log
    python tools/parse_garmin.py path/to/log_260828_132656_LZIB.csv
    python tools/parse_garmin.py <dir> --min-points 600   # flights only
    python tools/parse_garmin.py <dir> --no-process       # skip cache build
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "garmin_g1000"
OUTPUT_DIR = Path("data/parsed")

# ---------------------------------------------------------------------------
# Column map: Garmin column name -> (our field name, kind)
#
# Names on the left are exactly as they appear in the CSV's third header line.
# Where a Garmin channel means the same thing as an existing PilotSense field
# we reuse that name (altitude, ground_speed, track, heading, pitch, roll,
# air_speed, true_air_speed, rate_climb) so the safety detectors, the index
# summary and the map's metric colouring all work without special-casing.
#
# kind: "num" -> float, "str" -> kept verbatim (enums and radio channels),
# "freq" -> float re-rendered as a 3-decimal string, because 8.33 kHz channel
# spacing (118.305) would otherwise be lost to the payload's 2-decimal rounding.
# ---------------------------------------------------------------------------
COLUMN_MAP = {
    # Position / altitude
    "AltMSL":    ("altitude",        "num"),   # baro-corrected MSL, ft
    "AltB":      ("alt_baro",        "num"),   # indicated baro altitude, ft
    "AltGPS":    ("alt_gps",         "num"),   # geometric GPS altitude, ft WGS84
    "BaroA":     ("baro_setting",    "num"),   # altimeter setting, inHg
    "OAT":       ("oat",             "num"),   # outside air temperature, °C

    # Speeds / vertical
    "IAS":       ("air_speed",       "num"),   # indicated airspeed, kt
    "TAS":       ("true_air_speed",  "num"),   # true airspeed, kt
    "GndSpd":    ("ground_speed",    "num"),   # GPS ground speed, kt
    "VSpd":      ("rate_climb",      "num"),   # baro vertical speed, ft/min
    "VSpdG":     ("vspeed_gps",      "num"),   # GPS-derived vertical speed, ft/min

    # Attitude / accelerations (AHRS)
    "Pitch":     ("pitch",           "num"),
    "Roll":      ("roll",            "num"),
    "LatAc":     ("lat_accel",       "num"),   # lateral acceleration, G
    "NormAc":    ("norm_accel",      "num"),   # normal acceleration, G from 1 G
    "HDG":       ("heading",         "num"),   # magnetic heading, °
    "TRK":       ("track",           "num"),   # ground track, °
    "MagVar":    ("mag_var",         "num"),

    # Electrical / fuel
    "volt1":     ("volt1",           "num"),
    "volt2":     ("volt2",           "num"),
    "FQtyL":     ("fuel_qty_left",   "num"),   # gal
    "FQtyR":     ("fuel_qty_right",  "num"),   # gal

    # Engine 1 (left) / Engine 2 (right)
    "E1 FFlow":  ("e1_fuel_flow",    "num"),   # gph
    "E1 FPres":  ("e1_fuel_press",   "num"),   # psi
    "E1 OilT":   ("e1_oil_temp",     "num"),   # °F
    "E1 OilP":   ("e1_oil_press",    "num"),   # psi
    "E1 RPM":    ("e1_rpm",          "num"),
    "E1 %Pwr":   ("e1_power_pct",    "pct"),   # logged 0..1, served as 0..100 %
    "E2 FFlow":  ("e2_fuel_flow",    "num"),
    "E2 FPres":  ("e2_fuel_press",   "num"),
    "E2 OilT":   ("e2_oil_temp",     "num"),
    "E2 OilP":   ("e2_oil_press",    "num"),
    "E2 RPM":    ("e2_rpm",          "num"),
    "E2 %Pwr":   ("e2_power_pct",    "pct"),

    # Navigation
    "AtvWpt":    ("active_waypoint", "str"),
    "WptDst":    ("wpt_distance",    "num"),   # nm
    "WptBrg":    ("wpt_bearing",     "num"),   # °
    "HSIS":      ("hsi_source",      "str"),   # GPS / NAV1 / ...
    "CRS":       ("selected_course", "num"),
    "HCDI":      ("hcdi",            "num"),   # lateral deviation, full-scale units
    "VCDI":      ("vcdi",            "num"),   # vertical deviation, full-scale units
    "NAV1":      ("nav1",            "freq"),
    "NAV2":      ("nav2",            "freq"),
    "COM1":      ("com1",            "freq"),
    "COM2":      ("com2",            "freq"),

    # Wind
    "WndSpd":    ("wind_speed",      "num"),   # kt
    "WndDr":     ("wind_dir",        "num"),   # °, signed ±180

    # Autopilot / AFCS
    "AfcsOn":    ("afcs_on",         "num"),   # status/mode value, 0..6
    "RollM":     ("roll_mode",       "str"),   # NONE/HDG/GPS/LOC/LOCa/WL
    "PitchM":    ("pitch_mode",      "str"),   # NONE/PIT/ALT/ALTS/VS/FLCIAS/GS/VPTH
    "RollC":     ("roll_command",    "num"),
    "PichC":     ("pitch_command",   "num"),

    # GNSS integrity (WAAS / SBAS)
    "GPSfix":    ("gps_fix",         "str"),   # NoSoln / 3D / 3DDiff
    "HAL":       ("gnss_hal",        "num"),   # horizontal alert limit, m
    "HPLwas":    ("gnss_hpl_was",    "num"),   # horizontal protection level (WAAS), m
    "VPLwas":    ("gnss_vpl_was",    "num"),   # vertical protection level (WAAS), m
    "HPLfd":     ("gnss_hpl_fd",     "num"),   # horizontal protection level (fault detection), m
}

# Turbine channels the DA62 (and every piston airframe) never fills, plus the
# vertical alert limit, which this software version leaves blank throughout.
ALWAYS_EMPTY = {"E1 ITT", "E1 N1", "E1 N2", "VAL"}

FILENAME_RE = re.compile(r"^log_(\d{6})_(\d{6})(.*)\.csv$", re.IGNORECASE)
KV_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')


def _airframe_header(line: str) -> dict:
    """Parse the `#airframe_info, key="value", ...` first line of a log."""
    return dict(KV_RE.findall(line))


def _read_airframe_xml(path: Path) -> dict:
    """Pull the extra identity fields from a sibling airframe_info.xml, if any."""
    if not path.is_file():
        return {}
    text = path.read_text(errors="replace")
    out = {}
    for tag in ("airframe_name", "unit_software_part_number", "unit_software_version",
                "system_id", "this_cards_serial_number", "system_software_part_number",
                "system_software_version"):
        m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.DOTALL)
        if m and m.group(1).strip():
            out[tag] = m.group(1).strip()
    return out


def _parse_offset(raw: str) -> timezone:
    """`+02:00` / `-05:00` / `+00:00` -> tzinfo."""
    m = re.match(r"^([+-])(\d{2}):(\d{2})$", raw.strip())
    if not m:
        return timezone.utc
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def _to_utc(date_s: str, time_s: str, offset_s: str):
    try:
        naive = datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=_parse_offset(offset_s)).astimezone(timezone.utc)


def _num(cell: str):
    try:
        return float(cell)
    except ValueError:
        return None


def read_log(path: Path) -> dict:
    """Parse one Garmin CSV into {"meta": {...}, "samples": [...]}"""
    lines = path.read_text(errors="replace").splitlines()
    if len(lines) < 4:
        return {"meta": {}, "samples": [], "malformed": 0}

    header = _airframe_header(lines[0])
    units = [c.strip() for c in lines[1].lstrip("#").split(",")]
    columns = [c.strip() for c in lines[2].split(",")]
    index = {name: i for i, name in enumerate(columns)}

    unknown = [c for c in columns
               if c not in COLUMN_MAP and c not in ALWAYS_EMPTY
               and c not in ("Lcl Date", "Lcl Time", "UTCOfst", "Latitude", "Longitude")]

    samples = []
    malformed = 0
    for raw in lines[3:]:
        if not raw.strip():
            continue
        cells = [c.strip() for c in raw.split(",")]
        # Garmin stops writing mid-line when the card loses power, so the last
        # record of every file is short. Drop any row that isn't complete.
        if len(cells) != len(columns):
            malformed += 1
            continue

        ts = _to_utc(cells[index["Lcl Date"]], cells[index["Lcl Time"]],
                     cells[index["UTCOfst"]])
        if ts is None:
            malformed += 1
            continue

        sample = {"t": ts.isoformat()}

        lat = _num(cells[index["Latitude"]]) if "Latitude" in index else None
        lon = _num(cells[index["Longitude"]]) if "Longitude" in index else None
        if lat is not None and lon is not None:
            sample["lat"] = lat
            sample["lon"] = lon

        for column, (field, kind) in COLUMN_MAP.items():
            i = index.get(column)
            if i is None:
                continue
            cell = cells[i]
            if not cell:
                continue
            if kind == "str":
                sample[field] = cell
            elif kind == "freq":
                v = _num(cell)
                if v is not None:
                    sample[field] = f"{v:.3f}"
            elif kind == "pct":
                v = _num(cell)
                if v is not None:
                    sample[field] = round(v * 100.0, 1)
            else:
                v = _num(cell)
                if v is not None:
                    sample[field] = v

        left, right = sample.get("fuel_qty_left"), sample.get("fuel_qty_right")
        if left is not None and right is not None:
            sample["fuel_qty_total"] = round(left + right, 2)

        samples.append(sample)

    m = FILENAME_RE.match(path.name)
    origin = (m.group(3).strip("_").upper() if m else "") or None

    positioned = [s for s in samples if "lat" in s]
    meta = {
        "source": SOURCE,
        "source_file": path.name,
        "airframe": header.get("airframe_name"),
        "system_id": header.get("system_id"),
        "unit_software_part_number": header.get("unit_software_part_number"),
        "unit_software_version": header.get("unit_software_version"),
        "log_mode": "NORMAL" if "mode=NORMAL" in lines[0] else None,
        "origin_ident": origin,
        "samples": len(samples),
        "positioned_samples": len(positioned),
        "malformed_rows": malformed,
        "unmapped_columns": unknown,
        "units": {COLUMN_MAP[c][0]: units[index[c]]
                  for c in columns if c in COLUMN_MAP and index[c] < len(units)},
    }
    return {"meta": {k: v for k, v in meta.items() if v not in (None, [], {})},
            "samples": samples, "malformed": malformed}


def flight_id_for(path: Path, samples: list, meta: dict) -> str:
    """`G1000-20260828-1325-LZIB` — date/time of the first sample, plus origin."""
    stamp = None
    if samples:
        try:
            stamp = datetime.fromisoformat(samples[0]["t"])
        except ValueError:
            stamp = None
    if stamp is None:
        m = FILENAME_RE.match(path.name)
        if m:
            stamp = datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S")
    when = stamp.strftime("%Y%m%d-%H%M") if stamp else "unknown"
    return f"G1000-{when}-{meta.get('origin_ident') or 'GND'}"


def collect_csvs(targets) -> list:
    found = []
    for target in targets:
        p = Path(target).expanduser()
        if p.is_dir():
            found.extend(sorted(p.glob("*.csv")))
        elif p.is_file():
            found.append(p)
        else:
            print(f"  ! not found: {p}")
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="+",
                    help="a data_log directory and/or individual log_*.csv files")
    ap.add_argument("--out", default=str(OUTPUT_DIR),
                    help=f"output directory (default: {OUTPUT_DIR})")
    ap.add_argument("--min-points", type=int, default=1,
                    help="skip logs with fewer than N positioned samples "
                         "(default 1 — drops power-on cycles that never got a fix)")
    ap.add_argument("--no-process", action="store_true",
                    help="don't build the data/processed cache afterwards")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be written, write nothing")
    args = ap.parse_args(argv)

    csvs = collect_csvs(args.targets)
    if not csvs:
        print("No Garmin CSV logs found.")
        return 1

    # airframe_info.xml usually sits one level above data_log/
    xml_meta = {}
    for candidate in {c.parent.parent / "airframe_info.xml" for c in csvs} | \
                     {c.parent / "airframe_info.xml" for c in csvs}:
        xml_meta.update(_read_airframe_xml(candidate))

    out_dir = Path(args.out)
    if not args.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    skipped = []
    for path in csvs:
        parsed = read_log(path)
        meta, samples = parsed["meta"], parsed["samples"]
        positioned = meta.get("positioned_samples", 0)

        if positioned < args.min_points:
            skipped.append((path.name, f"{positioned} positioned samples "
                                       f"< --min-points {args.min_points}"))
            continue

        if xml_meta:
            meta.setdefault("card_serial", xml_meta.get("this_cards_serial_number"))
            meta.setdefault("system_software_version",
                            xml_meta.get("system_software_version"))
            meta = {k: v for k, v in meta.items() if v is not None}

        fid = flight_id_for(path, samples, meta)
        meta["flight_id"] = fid
        dest = out_dir / f"{fid}.json"

        if args.dry_run:
            print(f"  would write {dest}  ({len(samples)} samples, "
                  f"{positioned} positioned)")
            written.append(fid)
            continue

        tmp = dest.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            f.write(json.dumps({"_meta": meta}) + "\n")
            for s in samples:
                f.write(json.dumps(s) + "\n")
        os.replace(tmp, dest)
        print(f"  {path.name} -> {dest.name}  "
              f"({len(samples)} samples, {positioned} positioned, "
              f"{parsed['malformed']} malformed row(s) dropped)")
        written.append(fid)

    for name, why in skipped:
        print(f"  skipped {name}: {why}")

    if not written:
        print("Nothing written.")
        return 0

    print(f"\n{len(written)} flight(s) written to {out_dir}/.")

    if args.no_process or args.dry_run:
        print("Run `python tools/process.py` to build the view caches.")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import process_flight  # noqa: E402  (heavy import; only when needed)

    print("\nPre-processing:")
    for fid in written:
        cache = process_flight(fid)
        print(f"  {fid}: {len(cache['trajectory'])} points, "
              f"{len(cache['events'])} safety event(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
