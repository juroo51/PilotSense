"""Post-upload flight analysis: scan a flight timeline for dangerous patterns.

Input is the merged DataFrame produced by load_flight_data() (sorted by
timestamp). Each detector yields event dicts; analyze_flight() runs them all
and returns the combined, time-ordered list.

Severity is a three-level scale shared by the API and both frontends:
    1 = Notice   (worth a look)
    2 = Caution  (unusual, possibly unsafe)
    3 = Danger   (exceeded a safety limit)
"""

import numpy as np
import pandas as pd

SEVERITY_LEVELS = {
    1: {"name": "Notice", "color": "#f4b400"},
    2: {"name": "Caution", "color": "#ed7117"},
    3: {"name": "Danger", "color": "#d7191c"},
}


def _runs(mask, seconds=None, merge_gap_s=0):
    """Yield (start, end) index pairs of consecutive True runs in a boolean array.

    Runs separated by less than merge_gap_s seconds are merged into one,
    so a brief dip below a threshold doesn't split one manoeuvre into
    several events.
    """
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return
    breaks = np.flatnonzero(np.diff(idx) > 1)
    starts = np.concatenate(([0], breaks + 1))
    ends = np.concatenate((breaks, [idx.size - 1]))
    runs = [(int(idx[s]), int(idx[e])) for s, e in zip(starts, ends)]

    if merge_gap_s and seconds is not None:
        merged = [runs[0]]
        for s, e in runs[1:]:
            if seconds[s] - seconds[merged[-1][1]] < merge_gap_s:
                merged[-1] = (merged[-1][0], e)
            else:
                merged.append((s, e))
        runs = merged

    yield from runs


def _severity_for(value, thresholds):
    """thresholds = (notice, caution, danger), ascending magnitudes."""
    level = 0
    for i, t in enumerate(thresholds, start=1):
        if value >= t:
            level = i
    return level


def _event(df, etype, severity, peak_idx, start_idx, end_idx, summary,
           peak_field, peak_value, unit):
    ts = df["timestamp"]
    duration = (ts.iloc[end_idx] - ts.iloc[start_idx]).total_seconds()
    return {
        "type": etype,
        "severity": severity,
        "severity_name": SEVERITY_LEVELS[severity]["name"],
        "start": ts.iloc[start_idx].isoformat(),
        "end": ts.iloc[end_idx].isoformat(),
        "duration_s": round(duration, 1),
        "lat": float(df["latitude"].iloc[peak_idx]),
        "lon": float(df["longitude"].iloc[peak_idx]),
        "peak": {"field": peak_field, "value": round(float(peak_value), 1), "unit": unit},
        "summary": summary,
    }


# ------------------ DETECTORS ------------------

def detect_gps_gaps(df, seconds):
    """Periods with no position data. Notice > 10 s, Caution > 60 s, Danger > 5 min."""
    events = []
    gaps = np.diff(seconds)
    for i, gap in enumerate(gaps):
        severity = _severity_for(gap, (10, 60, 300))
        if not severity:
            continue
        events.append(_event(
            df, "gps_gap", severity, i + 1, i, i + 1,
            f"GPS signal lost for {gap:.0f} s — no position data was recorded; "
            f"the track is interpolated over this period.",
            "gap", gap, "s",
        ))
    return events


def detect_vertical_rate(df, seconds):
    """Sustained climb/descent rate from ADS-B altitude.

    Notice > 800 ft/min, Caution > 1500 ft/min, Danger > 2500 ft/min,
    sustained for at least 10 s.
    """
    if "altitude" not in df.columns:
        return []
    alt = pd.to_numeric(df["altitude"], errors="coerce")
    if alt.notna().sum() < 20:
        return []

    # ADS-B altitude is quantized (25 ft steps), so differentiate over a
    # ~15-sample window instead of sample-to-sample.
    window = 15
    dt = pd.Series(seconds).diff(window)
    rate = alt.diff(window) / dt.replace(0, np.nan) * 60.0  # ft/min
    rate = rate.rolling(5, min_periods=1).mean()

    events = []
    for s, e in _runs((rate.abs() > 800).fillna(False).to_numpy(), seconds, 15):
        if seconds[e] - seconds[s] < 10:
            continue
        seg = rate.iloc[s:e + 1]
        peak_rel = seg.abs().idxmax()
        peak = seg.loc[peak_rel]
        severity = _severity_for(abs(peak), (800, 1500, 2500))
        word = "climb" if peak > 0 else "descent"
        events.append(_event(
            df, "vertical_rate", severity, peak_rel, s, e,
            f"Rapid {word}: vertical rate reached {abs(peak):.0f} ft/min "
            f"for {seconds[e] - seconds[s]:.0f} s.",
            "vertical_rate", peak, "ft/min",
        ))
    return events


def detect_sharp_turns(df, seconds):
    """High turn rate while moving (taxi manoeuvres are gated out).

    A standard-rate turn is 3°/s. Notice > 3°/s, Caution > 4.5°/s,
    Danger > 8°/s, sustained for at least 5 s.
    """
    if "track" not in df.columns:
        return []
    track = pd.to_numeric(df["track"], errors="coerce")
    speed = pd.to_numeric(df.get("gps_speed"), errors="coerce")

    dtrack = track.diff()
    dtrack = (dtrack + 180) % 360 - 180  # wrap to [-180, 180]
    dt = pd.Series(np.concatenate(([np.nan], np.diff(seconds))))
    turn_rate = (dtrack / dt.replace(0, np.nan)).rolling(3, min_periods=1).mean()

    moving = speed.fillna(0) > 30  # ignore taxi/stationary heading noise
    mask = ((turn_rate.abs() > 3) & moving).fillna(False).to_numpy()

    events = []
    for s, e in _runs(mask):
        if seconds[e] - seconds[s] < 5:
            continue
        seg = turn_rate.iloc[s:e + 1]
        peak_rel = seg.abs().idxmax()
        peak = seg.loc[peak_rel]
        severity = _severity_for(abs(peak), (3, 4.5, 8))
        events.append(_event(
            df, "sharp_turn", severity, peak_rel, s, e,
            f"Sharp turn: heading changed at {abs(peak):.1f}°/s for "
            f"{seconds[e] - seconds[s]:.0f} s (standard rate is 3°/s).",
            "turn_rate", peak, "deg/s",
        ))
    return events


def detect_speed_anomaly(df, seconds):
    """Abrupt deceleration while moving fast — possible hard manoeuvre or stop.

    Notice > 1.5 kt/s, Caution > 3 kt/s, Danger > 6 kt/s, sustained ≥ 4 s,
    starting above 40 kt.
    """
    speed = pd.to_numeric(df.get("gps_speed"), errors="coerce")
    if speed is None or speed.notna().sum() < 20:
        return []

    dt = pd.Series(np.concatenate(([np.nan], np.diff(seconds))))
    accel = (speed.diff() / dt.replace(0, np.nan)).rolling(3, min_periods=1).mean()
    mask = ((accel < -1.5) & (speed.shift(3).fillna(0) > 40)).fillna(False).to_numpy()

    events = []
    for s, e in _runs(mask):
        if seconds[e] - seconds[s] < 4:
            continue
        seg = accel.iloc[s:e + 1]
        peak_rel = seg.idxmin()
        peak = seg.loc[peak_rel]
        severity = _severity_for(abs(peak), (1.5, 3, 6))
        events.append(_event(
            df, "rapid_deceleration", severity, peak_rel, s, e,
            f"Rapid deceleration: speed dropped at {abs(peak):.1f} kt/s "
            f"(from {speed.iloc[s]:.0f} kt) over {seconds[e] - seconds[s]:.0f} s.",
            "deceleration", peak, "kt/s",
        ))
    return events


VS0_KT = 50.0    # assumed clean stall speed (typical light single); tune per aircraft
FIELD_AGL_GATE_FT = 800  # below this, slow flight is normal (approach/departure)


def detect_low_stall_margin(df, seconds):
    """Speed close to the stall speed estimated for the current bank angle.

    Bank is inferred from ground speed and turn rate (coordinated-turn
    physics); stall speed grows with sqrt(load factor). Notice < 1.3×Vs,
    Caution < 1.25×Vs, Danger < 1.12×Vs, sustained ≥ 5 s. Banking more
    than 10° escalates severity one level — a stall in a bank can
    develop into a spin. Note: GPS ground speed approximates airspeed,
    so wind shifts the margin.
    """
    speed = pd.to_numeric(df.get("gps_speed"), errors="coerce")
    track = pd.to_numeric(df.get("track"), errors="coerce")
    if speed is None or speed.notna().sum() < 20:
        return []

    if "aircraft_status" in df.columns:
        airborne = df["aircraft_status"].astype(str).str.contains("airborne")
    else:
        airborne = speed.fillna(0) > 40

    # Slow flight near the ground is a normal approach/departure — gate it
    # out using field elevation taken from the on-ground samples.
    gate = airborne.copy()
    if "altitude" in df.columns:
        alt = pd.to_numeric(df["altitude"], errors="coerce")
        ground_alts = alt[~airborne].dropna()
        if len(ground_alts):
            gate &= (alt - ground_alts.median()) > FIELD_AGL_GATE_FT

    dt = pd.Series(np.concatenate(([np.nan], np.diff(seconds))))
    dtrack = (track.diff() + 180) % 360 - 180
    turn_rate = (dtrack / dt.replace(0, np.nan)).rolling(5, min_periods=1).mean()

    v_ms = speed * 0.51444
    omega = np.radians(turn_rate.abs())
    bank = np.degrees(np.arctan(v_ms * omega / 9.81))
    load_factor = 1 / np.cos(np.radians(bank))
    ratio = speed / (VS0_KT * np.sqrt(load_factor))

    mask = (gate & (ratio < 1.3)).fillna(False).to_numpy()

    events = []
    for s, e in _runs(mask, seconds, 15):
        if seconds[e] - seconds[s] < 5:
            continue
        seg_ratio = ratio.iloc[s:e + 1]
        peak_rel = seg_ratio.idxmin()
        min_ratio = seg_ratio.loc[peak_rel]
        max_bank = bank.iloc[s:e + 1].max()

        severity = 1
        if min_ratio < 1.25:
            severity = 2
        if min_ratio < 1.12:
            severity = 3
        banked = max_bank > 10
        if banked:
            severity = min(3, severity + 1)

        vs_here = VS0_KT * np.sqrt(1 / np.cos(np.radians(max_bank)))
        summary = (
            f"Low stall margin: {speed.iloc[peak_rel]:.0f} kt is only "
            f"{min_ratio:.2f}× the estimated stall speed"
        )
        if banked:
            summary += (
                f" while banking ~{max_bank:.0f}° (stall speed rises to "
                f"~{vs_here:.0f} kt in this turn — a stall here can become a spin)"
            )
        summary += f" for {seconds[e] - seconds[s]:.0f} s."
        events.append(_event(
            df, "low_stall_margin", severity, peak_rel, s, e, summary,
            "stall_margin", min_ratio, "×Vs",
        ))
    return events


DETECTORS = [
    detect_gps_gaps,
    detect_vertical_rate,
    detect_sharp_turns,
    detect_speed_anomaly,
    detect_low_stall_margin,
]


def analyze_flight(df: pd.DataFrame) -> dict:
    """Run all detectors over a sorted flight DataFrame."""
    df = df.dropna(subset=["timestamp", "latitude", "longitude"]).reset_index(drop=True)
    if len(df) < 2:
        return {"events": [], "levels": SEVERITY_LEVELS}

    seconds = df["timestamp"].astype("int64").to_numpy() / 1e9

    events = []
    for detector in DETECTORS:
        events.extend(detector(df, seconds))

    events.sort(key=lambda e: e["start"])
    for i, event in enumerate(events, start=1):
        event["id"] = i
    return {"events": events, "levels": SEVERITY_LEVELS}
