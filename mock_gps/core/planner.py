import re
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

import polyline

from mock_gps import config

_HTML_TAG = re.compile(r"<[^>]+>")


def _instruction_text(value: str) -> str:
    return unescape(_HTML_TAG.sub("", value)).strip()


def _google_mode(mode: str) -> str:
    return "two_wheeler" if mode == "motorcycle" else mode


def _select_route(routes: list[dict], transit_type: str) -> dict:
    if transit_type == "MRT":
        for route in routes:
            if any(
                step.get("transit_details", {}).get("line", {}).get("vehicle", {}).get("type") == "SUBWAY"
                for leg in route.get("legs", []) for step in leg.get("steps", [])
            ):
                return route
    return routes[0]


def scheduled_departure(arrival: datetime, stop: dict) -> datetime:
    """When the mission may leave `stop` after arriving at `arrival`.

    `arrival` must already be in the configured local timezone, because the
    wait_time is resolved against that date. Navigation needs this when it
    replans mid-mission.
    """
    value = stop.get("wait_time", "")
    if not value:
        return arrival
    target = datetime.strptime(value.replace("24:", "00:"), "%H:%M").time()
    candidate = datetime.combine(arrival.date(), target, tzinfo=arrival.tzinfo)
    if candidate <= arrival:
        if stop.get("skip_if_late"):
            return arrival
        candidate += timedelta(days=1)
    return candidate


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def plan_mission(init_loc: str, stops: list[dict], *, start_time: datetime | None = None) -> dict[str, Any]:
    if not config.gmaps_client:
        raise RuntimeError("Google Maps client is not configured")
    departure = start_time or config.local_now()
    origin = init_loc
    routes: list[dict] = []
    for index, stop in enumerate(stops):
        mode = _google_mode(stop.get("mode", "walking"))
        transit_type = stop.get("transit_type", "")
        kwargs: dict[str, Any] = {
            "mode": mode,
            "departure_time": departure,
            "language": "en-US",
            "alternatives": True,
        }
        if mode == "transit" and transit_type in {"MRT", "BUS"}:
            kwargs["transit_mode"] = ["subway" if transit_type == "MRT" else "bus"]
        destination = stop.get("coord") or stop["name"]
        options = config.gmaps_client.directions(origin, destination, **kwargs)
        if not options:
            raise RuntimeError(f"Google returned no route for stop {index + 1}: {stop['name']}")
        selected = _select_route(options, transit_type)
        leg = selected["legs"][0]
        duration = int(leg.get("duration", {}).get("value", 0))
        if duration <= 0:
            raise RuntimeError(f"Google returned an invalid duration for stop {index + 1}: {stop['name']}")
        arrival = departure + timedelta(seconds=duration)
        steps = []
        for step_index, step in enumerate(leg.get("steps", [])):
            transit = step.get("transit_details", {}) or {}
            vehicle_type = transit.get("line", {}).get("vehicle", {}).get("type", "")
            points = polyline.decode(step.get("polyline", {}).get("points", ""))
            steps.append({
                "index": step_index,
                "travel_mode": step.get("travel_mode", ""),
                "vehicle_type": vehicle_type,
                "line": transit.get("line", {}).get("short_name", ""),
                "duration_seconds": int(step.get("duration", {}).get("value", 0)),
                "distance_meters": int(step.get("distance", {}).get("value", 0)),
                "points": [[lat, lng] for lat, lng in points],
                "instruction": _instruction_text(step.get("html_instructions", "")),
            })
        routes.append({
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "transit_type": transit_type,
            "departure_at": _utc_iso(departure),
            "arrival_at": _utc_iso(arrival),
            "distance_meters": int(leg.get("distance", {}).get("value", 0)),
            "duration_seconds": duration,
            "start_address": leg.get("start_address", ""),
            "end_address": leg.get("end_address", ""),
            "steps": steps,
        })
        departure = scheduled_departure(arrival, stop)
        origin = destination
    return {
        "planned_at": _utc_iso(start_time or config.local_now()),
        "initial_google_eta": routes[-1]["arrival_at"],
        "routes": routes,
    }
