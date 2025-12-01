import json
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import pandas as pd
import matplotlib.pyplot as plt

PARSED_DIR = Path("data/parsed")

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def list_flights():
    """Return a sorted list of flight identifiers based on filenames."""
    files = PARSED_DIR.glob("*.json")
    return sorted([f.stem for f in files])

def load_flight_data(flight_id: str) -> pd.DataFrame:
    """Load the JSONL file for one flight into a DataFrame."""
    file_path = PARSED_DIR / f"{flight_id}.json"
    rows = []
    with open(file_path, "r") as f:
        for line in f:
            rows.append(json.loads(line))

    df = pd.DataFrame(rows)
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df

ADS_FIELDS = [
    "a","ab","ag","br","c","ct","em","f","gpb","gr","gs","gv","h","i","la","lo","m",
    "mh","n","naf","nam","nb","nh","np","nq","nv","oat","og","rc","rdi","rds","ro",
    "rs","s","sda","sn","sp","spi","sq","st","t","tat","th","tr","trr","v","wd","ws"
]

@app.get("/flight/{flight_id}/trajectory")
async def flight_trajectory(flight_id: str):
    df = load_flight_data(flight_id).sort_values("timestamp")

    trajectory = []
    for _, row in df.iterrows():
        if pd.notna(row.get("la")) and pd.notna(row.get("lo")):
            point = {
                "lat": float(row["la"]),
                "lon": float(row["lo"]),
                "timestamp": row["timestamp"].isoformat(),
            }
            # add all fields safely (NaN → None → JSON null)
            for field in ADS_FIELDS:
                val = row.get(field)
                if pd.notna(val):
                    point[field] = val
                else:
                    point[field] = None

            trajectory.append(point)

    return {"trajectory": trajectory}





@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    flights = list_flights()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "flights": flights}
    )

@app.get("/flight/{flight_id}", response_class=HTMLResponse)
async def show_flight(request: Request, flight_id: str):
    return templates.TemplateResponse(
        "flight.html",
        {"request": request, "flight_id": flight_id}
    )

@app.get("/flight/{flight_id}/graphs", response_class=HTMLResponse)
async def show_graphs(request: Request, flight_id: str):
    return templates.TemplateResponse(
        "graphs.html",
        {"request": request, "flight_id": flight_id}
    )
