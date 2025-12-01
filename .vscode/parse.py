import os
import json

INPUT_FILE = "data/flights.json"                     # path to the big source file
OUTPUT_DIR = "data/parsed"                       # output folder

def ensure_output_dir():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

def sanitize_filename(name):
    # Remove characters that can't be in filenames
    return "".join(c for c in name if c.isalnum() or c in ("_", "-", "."))

def process_file():
    ensure_output_dir()

    # We will keep open file handles per flight to avoid reopening constantly
    file_handles = {}

    with open(INPUT_FILE, "r") as infile:
        for line_num, line in enumerate(infile, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                print(f"Skipping invalid JSON on line {line_num}")
                continue

            flight_id = record.get("f")

            # Skip records with no flight/callsign
            if not flight_id:
                continue

            flight_id = sanitize_filename(flight_id)

            # Lazily open the output file for this flight
            if flight_id not in file_handles:
                out_path = os.path.join(OUTPUT_DIR, f"{flight_id}.json")
                file_handles[flight_id] = open(out_path, "a")

            # Write the original JSON line to the corresponding file
            file_handles[flight_id].write(line + "\n")

            if line_num % 100000 == 0:
                print(f"Processed {line_num} lines...")

    # Close all file handles
    for f in file_handles.values():
        f.close()

    print("✅ Parsing complete!")
    print(f"✅ Files saved in: {OUTPUT_DIR}")

if __name__ == "__main__":
    process_file()
