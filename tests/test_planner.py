from datetime import datetime

from digital_twin import config
from digital_twin.core.planner import plan_mission


class FakeMaps:
    def __init__(self):
        self.departures = []

    def directions(self, origin, destination, **kwargs):
        self.departures.append(kwargs["departure_time"])
        return [{
            "legs": [{
                "duration": {"value": 120},
                "distance": {"value": 1000},
                "start_address": str(origin),
                "end_address": str(destination),
                "steps": [{
                    "travel_mode": "WALKING",
                    "html_instructions": "Turn <b>left</b> onto <b>Main &amp; First</b>",
                    "duration": {"value": 120},
                    "distance": {"value": 1000},
                    "polyline": {"points": "_p~iF~ps|U_ulLnnqC"},
                }],
            }]
        }]


def test_full_mission_is_planned_before_queueing(monkeypatch):
    maps = FakeMaps()
    monkeypatch.setattr(config, "gmaps_client", maps)
    start = datetime(2026, 8, 3, 8, 0, tzinfo=config.TIMEZONE)
    stops = [
        {"name": "A", "mode": "walking", "transit_type": "", "wait_time": "", "skip_if_late": False, "coord": ""},
        {"name": "B", "mode": "walking", "transit_type": "", "wait_time": "", "skip_if_late": False, "coord": ""},
    ]
    plan = plan_mission("25,121", stops, start_time=start)
    assert len(plan["routes"]) == 2
    assert maps.departures[1] > maps.departures[0]
    assert plan["initial_google_eta"] == plan["routes"][-1]["arrival_at"]
    assert plan["routes"][0]["steps"][0]["instruction"] == "Turn left onto Main & First"
