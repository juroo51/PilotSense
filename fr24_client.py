"""Thin client for the official Flightradar24 API (fr24api.flightradar24.com).

Used by the map viewer's "Compare with FlightRadar" button to pull the same
flight's track and overlay it on top of our locally-recorded ADS-B/GPS data so
the user can sanity-check coverage and values (notably `air_speed`).

Auth: set FR24_API_TOKEN in the environment (a token from your FR24 API
subscription). Without it every call raises Fr24ConfigError and the endpoint
degrades to a clear 503.

Flight matching (per product decision): ICAO 24-bit hex + flight date. FR24's
flight-summary is queried over the day window, then the result whose `hex`
matches is selected; its `fr24_id` is used to pull the full track. A caller may
also pass an explicit fr24_id to bypass resolution (handy when auto-match fails
on a given plan tier).

The two FR24-specific calls are deliberately isolated in `_resolve_fr24_id`
and `_fetch_track` so the exact endpoints/fields can be adjusted to your plan
without touching the rest of the app.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx

BASE_URL = os.environ.get("FR24_API_BASE", "https://fr24api.flightradar24.com")
API_VERSION = os.environ.get("FR24_API_VERSION", "v1")
TIMEOUT_S = 20.0


class Fr24Error(Exception):
    """Base error; carries an HTTP status to surface to the frontend."""

    status_code = 502

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class Fr24ConfigError(Fr24Error):
    status_code = 503


class Fr24NotFoundError(Fr24Error):
    status_code = 404


def _token() -> str:
    token = os.environ.get("FR24_API_TOKEN", "").strip()
    if not token:
        raise Fr24ConfigError(
            "FlightRadar comparison is not configured: set FR24_API_TOKEN."
        )
    return token


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        timeout=TIMEOUT_S,
        headers={
            "Authorization": f"Bearer {_token()}",
            "Accept": "application/json",
            "Accept-Version": API_VERSION,
        },
    )


def _day_window(date_utc: datetime) -> tuple[str, str]:
    """[start, end] covering the UTC day, in FR24's required format.

    FR24 wants 'YYYY-MM-DDTHH:MM:SS' with no timezone suffix (it rejects the
    '+00:00'/'Z' that datetime.isoformat() emits).
    """
    fmt = "%Y-%m-%dT%H:%M:%S"
    start = date_utc.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start + timedelta(days=1) - timedelta(seconds=1)
    return start.strftime(fmt), end.strftime(fmt)


def _err_detail(resp: httpx.Response) -> str:
    """Short, safe snippet of an FR24 error body for debugging."""
    text = (resp.text or "").strip().replace("\n", " ")
    return f": {text[:200]}" if text else ""


def _resolve_fr24_id(client: httpx.Client, hex_id: str, date_utc: datetime) -> str:
    """Find the FR24 flight id for an ICAO hex on a given UTC day.

    FR24's flight-summary requires the day window plus at least one flight
    filter (it 400s on a window alone), and it has no `hex` filter. So we pass
    the ICAO 24-bit address via the `aircraft` filter, then confirm the `hex`
    on the returned rows. Adjust the endpoint/params here to match your plan.
    """
    dt_from, dt_to = _day_window(date_utc)
    resp = client.get(
        "/api/flight-summary/full",
        params={
            "flight_datetime_from": dt_from,
            "flight_datetime_to": dt_to,
            "aircraft": hex_id.strip().lower(),
        },
    )
    if resp.status_code == 401:
        raise Fr24ConfigError("FlightRadar rejected the API token (401).")
    if resp.status_code >= 400:
        raise Fr24Error(
            f"FlightRadar summary request failed ({resp.status_code})"
            f"{_err_detail(resp)}."
        )

    rows = resp.json().get("data", []) or []
    target = hex_id.strip().upper()
    matches = [r for r in rows if str(r.get("hex", "")).strip().upper() == target]
    if not matches:
        raise Fr24NotFoundError(
            f"No FlightRadar flight found for hex {hex_id} on "
            f"{date_utc.date().isoformat()}."
        )

    fr24_id = matches[0].get("fr24_id") or matches[0].get("id")
    if not fr24_id:
        raise Fr24NotFoundError("FlightRadar returned a match without a flight id.")
    return str(fr24_id)


def _fetch_track(client: httpx.Client, fr24_id: str) -> list[dict]:
    """Return the raw FR24 track point list for a flight id."""
    resp = client.get("/api/flight-tracks", params={"flight_id": fr24_id})
    if resp.status_code == 401:
        raise Fr24ConfigError("FlightRadar rejected the API token (401).")
    if resp.status_code == 404:
        raise Fr24NotFoundError(f"FlightRadar has no track for flight {fr24_id}.")
    if resp.status_code >= 400:
        raise Fr24Error(
            f"FlightRadar track request failed ({resp.status_code})"
            f"{_err_detail(resp)}."
        )

    payload = resp.json()
    # Documented shape: [{"fr24_id": ..., "tracks": [ {point}, ... ]}]
    if isinstance(payload, list):
        if not payload:
            raise Fr24NotFoundError("FlightRadar returned an empty track.")
        return payload[0].get("tracks", []) or []
    # Be tolerant of a {"tracks": [...]} / {"data": [...]} variant.
    return payload.get("tracks") or payload.get("data") or []


def _round(value, ndigits=2):
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return None


def _normalize_point(p: dict) -> dict | None:
    """Map an FR24 track point to our trajectory schema (shared field names)."""
    lat = p.get("lat")
    lon = p.get("lon")
    if lat is None or lon is None:
        return None
    return {
        "lat": _round(lat, 6),
        "lon": _round(lon, 6),
        "timestamp": p.get("timestamp"),
        # FR24 reports GROUND speed (not airspeed); altitude in ft; track in deg.
        "ground_speed": _round(p.get("gspeed")),
        "altitude": _round(p.get("alt")),
        "track": _round(p.get("track")),
        "rate_climb": _round(p.get("vspeed")),
    }


def fetch_flight_track(hex_id: str, date_utc: datetime, fr24_id: str | None = None) -> dict:
    """Fetch and normalize the FR24 track for a flight.

    Returns {source, fr24_id, count, trajectory:[points...]}. Raises an
    Fr24Error subclass (with .status_code/.message) on any failure.
    """
    if not hex_id and not fr24_id:
        raise Fr24NotFoundError("This flight has no ADS-B hex id to match on.")

    with _client() as client:
        resolved = fr24_id or _resolve_fr24_id(client, hex_id, date_utc)
        raw = _fetch_track(client, resolved)

    trajectory = [pt for pt in (_normalize_point(p) for p in raw) if pt]
    if not trajectory:
        raise Fr24NotFoundError("FlightRadar track contained no usable points.")

    return {
        "source": "flightradar24",
        "fr24_id": resolved,
        "count": len(trajectory),
        "trajectory": trajectory,
    }
