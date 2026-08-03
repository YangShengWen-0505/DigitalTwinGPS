import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import googlemaps
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Explicit process/container environment wins over the developer's local .env.
load_dotenv(os.path.join(BASE_DIR, '.env'), override=False)

if hasattr(time, 'tzset') and 'TZ' in os.environ:
    time.tzset()

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "").strip().strip('"')
PHONE_TAILSCALE_IP = os.getenv("PHONE_TAILSCALE_IP", "").strip().strip('"')
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "").strip().strip('"')
API_ACCESS_KEY = os.getenv("API_ACCESS_KEY", API_SECRET_KEY).strip().strip('"')
FLASK_SESSION_SECRET = os.getenv("FLASK_SESSION_SECRET", API_SECRET_KEY).strip().strip('"')
gmaps_client = None
if GOOGLE_MAPS_API_KEY:
    try:
        gmaps_client = googlemaps.Client(
            key=GOOGLE_MAPS_API_KEY,
            connect_timeout=5,
            read_timeout=20,
            retry_timeout=30,
        )
    except Exception:
        gmaps_client = None

config_path = os.path.join(BASE_DIR, "digital_twin", "data", "settings.json")
try:
    with open(config_path, encoding="utf-8") as f:
        CONFIG = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    CONFIG = {"mrt_station_groups": {}}


def validate_runtime_config() -> None:
    secrets = {
        "API_SECRET_KEY": API_SECRET_KEY,
        "API_ACCESS_KEY": API_ACCESS_KEY,
        "FLASK_SESSION_SECRET": FLASK_SESSION_SECRET,
    }
    missing = [name for name, value in secrets.items() if not value]
    if missing:
        raise RuntimeError(f"Missing required secret(s): {', '.join(missing)}")
    if any(len(value) < 16 for value in secrets.values()):
        raise RuntimeError("API and session secrets must each be at least 16 characters")
    if len(set(secrets.values())) != len(secrets):
        raise RuntimeError("API_SECRET_KEY, API_ACCESS_KEY, and FLASK_SESSION_SECRET must be distinct")

def _flatten_station_groups(groups: dict) -> dict[str, list[float]]:
    stations: dict[str, list[float]] = {}
    if not isinstance(groups, dict):
        return stations
    for system_name, lines in groups.items():
        if not isinstance(lines, dict):
            continue
        for line_name, line_stations in lines.items():
            if not isinstance(line_stations, dict):
                continue
            for station_name, coords in line_stations.items():
                if not isinstance(coords, list) or len(coords) != 2:
                    continue
                key = f"{station_name} ({line_name})"
                stations[key] = [float(coords[0]), float(coords[1])]
    return stations


MRT_STATIONS_DB = {}
MRT_STATIONS_DB.update(_flatten_station_groups(CONFIG.get("mrt_station_groups", {})))

TIMEZONE = ZoneInfo(os.getenv("TZ", "Asia/Taipei"))


def local_now() -> datetime:
    return datetime.now(TIMEZONE)
