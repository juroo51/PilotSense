"""Pre-process flights into data/processed/<id>.json (trajectory + safety events).

The web app no longer computes these on view — it reads the cache these files
provide. Run this once after adding the feature (to bootstrap existing flights),
or any time you want to rebuild caches:

    python tools/process.py            # process every flight in data/parsed/
    python tools/process.py OMCAT ...  # process only the named flight(s)
"""
import sys
from pathlib import Path

# Allow running as `python tools/process.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import list_flights, process_flight  # noqa: E402


def main(argv):
    ids = argv or list_flights()
    if not ids:
        print("No flights found in data/parsed/.")
        return
    for fid in ids:
        cache = process_flight(fid)
        print(f"{fid}: {len(cache['trajectory'])} points, {len(cache['events'])} events")
    print(f"Done — {len(ids)} flight(s) processed into data/processed/.")


if __name__ == "__main__":
    main(sys.argv[1:])
