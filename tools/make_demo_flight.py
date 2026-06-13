"""Generate a synthetic flight that triggers safety events at every severity.

Writes data/parsed/DEMO-EVENTS.json in the device NDJSON format. Useful for
demoing/testing the analysis pipeline (analysis.py) and both visualizations.

    python tools/make_demo_flight.py
"""

import json
import math
import os

OUTPUT = "data/parsed/DEMO-EVENTS.json"
DATE = "11-06-26"  # DD-MM-YY


def to_nmea(decimal_deg):
    deg = int(decimal_deg)
    return deg * 100 + (decimal_deg - deg) * 60


def main():
    lines = []
    lat, lon = 48.20, 17.30
    track = 90.0
    speed = 100.0  # kt
    alt = 3000.0   # ft

    t = 0
    skip_until = None
    while t < 600:
        # --- scripted phases ---
        if 120 <= t < 150:        # Danger: dive at ~3000 ft/min
            alt -= 50.0
        elif 330 <= t < 360:      # Notice: climb at ~900 ft/min
            alt += 15.0
        if 160 <= t < 170:        # Danger: 11°/s turn
            track += 11.0
        elif 400 <= t < 410:      # Caution: 5°/s turn
            track += 5.0
        if 180 <= t < 192:        # Danger: ~7 kt/s deceleration
            speed = max(speed - 7.0, 25.0)
        elif t == 192:
            speed = 100.0
        if t == 240:              # Caution: 80 s GPS gap
            skip_until = 320

        if skip_until and t < skip_until:
            t += 1
            continue
        skip_until = None

        # advance position along current track
        dist_deg = speed * 0.000514 / 3600 * 60  # rough deg per second at this speed
        lat += dist_deg * math.cos(math.radians(track))
        lon += dist_deg * math.sin(math.radians(track)) / math.cos(math.radians(lat))
        track %= 360

        hh, mm, ss = 12 + t // 3600, (t // 60) % 60, t % 60
        lines.append(json.dumps({
            "Gps_data": "ok",
            "Gps_time": f"{hh:02d}:{mm:02d}:{ss:02d}",
            "Gps_datum": DATE,
            "Gps_lat": round(to_nmea(lat), 5),
            "Gps_latsign": "N",
            "Gps_lon": round(to_nmea(lon), 5),
            "Gps_lonsign": "E",
            "Gps_speed": round(speed),
            "Gps_tr": round(track, 3),
        }))
        if t % 4 == 0:  # ADS-B altitude every few seconds, like real logs
            lines.append(json.dumps({
                "Adsb_HexId": "DEAD01",
                "Adsb_Fs": "no alert, no SPI, aircraft is airborne",
                "Adsb_alt": round(alt / 25) * 25,
                "Adsb_alt_type": "feet",
                "Adsb_msg": 50,
                "Adsb_gnd_speed": round(speed),
            }))
        t += 1

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with open(OUTPUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"Wrote {len(lines)} lines to {OUTPUT}")


if __name__ == "__main__":
    main()
