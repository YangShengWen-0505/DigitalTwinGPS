from datetime import datetime

import pytest

from mock_gps import config
from mock_gps.core.planner import plan_mission


class FakeMaps:
    def __init__(self):
        self.departures = []
        self.requests = []

    def directions(self, origin, destination, **kwargs):
        self.departures.append(kwargs["departure_time"])
        self.requests.append((origin, destination))
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


def test_precise_coordinate_is_a_final_walking_alignment_not_the_google_destination(monkeypatch):
    maps = FakeMaps()
    monkeypatch.setattr(config, "gmaps_client", maps)
    start = datetime(2026, 8, 3, 8, 0, tzinfo=config.TIMEZONE)
    precise = "40.700100,-120.950100"
    stops = [
        {
            "name": "Named Place",
            "mode": "walking",
            "transit_type": "",
            "wait_time": "",
            "skip_if_late": False,
            "coord": precise,
        },
        {
            "name": "Next Place",
            "mode": "walking",
            "transit_type": "",
            "wait_time": "",
            "skip_if_late": False,
            "coord": "",
        },
    ]

    plan = plan_mission("25,121", stops, start_time=start)

    assert maps.requests == [("25,121", "Named Place"), (precise, "Next Place")]
    route = plan["routes"][0]
    alignment = route["steps"][-1]
    assert route["destination"] == "Named Place"
    assert route["precision_coord"] == precise
    assert alignment["precision_alignment"] is True
    assert alignment["travel_mode"] == "WALKING"
    assert alignment["points"][-1] == [40.7001, -120.9501]
    assert alignment["duration_seconds"] > 0
    assert route["duration_seconds"] == 120 + alignment["duration_seconds"]


def test_precise_alignment_requires_a_google_route_endpoint(monkeypatch):
    maps = FakeMaps()
    monkeypatch.setattr(config, "gmaps_client", maps)
    maps.directions = lambda *_args, **_kwargs: [{
        "legs": [{
            "duration": {"value": 120},
            "distance": {"value": 1000},
            "steps": [],
        }]
    }]

    with pytest.raises(RuntimeError, match="no endpoint"):
        plan_mission(
            "25,121",
            [{"name": "A", "mode": "walking", "coord": "25.1,121.1"}],
        )
