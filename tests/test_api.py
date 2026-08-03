import csv
import json
from pathlib import Path

import pytest

from digital_twin import config, create_app, db, logger
from digital_twin.api import middleware, routes


def _plan(destination: str = "A") -> dict:
    eta = db.utc_now()
    return {
        "initial_google_eta": eta,
        "routes": [{
            "origin": "start",
            "destination": destination,
            "arrival_at": eta,
            "departure_at": eta,
            "steps": [{"points": [[25.0, 121.0], [25.1, 121.1]]}],
        }],
    }


def _app(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "api.sqlite3")
    monkeypatch.setattr(logger, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(logger, "ARCHIVE_ROOT", tmp_path / "logs" / "archives")
    monkeypatch.setattr(logger, "ARCHIVE_CACHE_ROOT", tmp_path / "logs" / ".archive-cache")
    app = create_app()
    app.config.update(TESTING=True)
    return app


def _login(client):
    with client.session_transaction() as browser_session:
        browser_session["authenticated"] = True


def test_status_is_idle_without_missions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        _login(client)
        response = client.get("/api/system_status")
    assert response.status_code == 200
    assert response.get_json()["mission_stats"]["status"] == "idle"


def test_legacy_csv_and_stop_get_are_removed(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        assert client.get("/api/csv?start_line=1").status_code == 404
        assert client.get("/stop_task").status_code == 405


def _reset_rate_limits():
    with routes._login_attempts_lock:
        routes._login_attempts.clear()
    with middleware._api_attempts_lock:
        middleware._api_attempts.clear()


@pytest.fixture(autouse=True)
def reset_rate_limits():
    _reset_rate_limits()
    yield
    _reset_rate_limits()


def test_start_task_returns_202_before_planning(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    payload = {"init_loc": "25,121", "stops": [{"name": "A", "mode": "walking"}]}
    with app.test_client() as client:
        response = client.post(
            "/start_task",
            json=payload,
            headers={"X-API-Key": config.API_ACCESS_KEY},
        )
    assert response.status_code == 202
    assert response.get_json()["initial_google_eta"] is None
    mission = db.latest_mission()
    assert mission["status"] == "planning"
    assert mission["plan"] == {}


def test_health_is_public(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"init_loc": "91,121", "stops": [{"name": "A", "mode": "walking"}]}, "out of range"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "flying"}]}, "mode must be"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "transit", "transit_type": "TRAIN"}]}, "transit_type"),
        ({"init_loc": "25,121", "stops": [{"name": "A", "mode": "walking", "wait_time": "99:99"}]}, "HH:MM"),
        ({"init_loc": "25,121", "stops": [{"name": str(i), "mode": "walking"} for i in range(51)]}, "cannot exceed 50"),
    ],
)
def test_start_task_rejects_unsafe_payloads(tmp_path, monkeypatch, payload, message):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        response = client.post(
            "/start_task", json=payload, headers={"X-API-Key": config.API_ACCESS_KEY}
        )
    assert response.status_code == 400
    assert message in response.get_json()["error"]


def test_invalid_api_key_is_rate_limited_separately(tmp_path, monkeypatch):
    _reset_rate_limits()
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        statuses = [
            client.get("/api/system_status", headers={"X-API-Key": f"wrong-{i}"}).status_code
            for i in range(6)
        ]
    assert statuses == [401, 401, 401, 401, 401, 429]
    assert not routes._login_attempts


def test_dashboard_login_has_its_own_rate_limit(tmp_path, monkeypatch):
    _reset_rate_limits()
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        statuses = [client.post("/login", data={"api_key": f"wrong-{i}"}).status_code for i in range(6)]
    assert statuses == [401, 401, 401, 401, 401, 429]
    assert not middleware._api_attempts


def test_successful_credentials_clear_their_own_failure_buckets(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    with app.test_client() as client:
        for index in range(5):
            assert client.post("/login", data={"api_key": f"wrong-{index}"}).status_code == 401
        assert client.post("/login", data={"api_key": config.API_SECRET_KEY}).status_code == 302

        for index in range(5):
            assert client.post(
                "/stop_task", headers={"X-API-Key": f"wrong-{index}"}
            ).status_code == 401
        assert client.post(
            "/stop_task", headers={"X-API-Key": config.API_ACCESS_KEY}
        ).status_code == 200


def test_route_token_changes_between_missions(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    first = db.create_mission({"init_loc": "25,121", "stops": [{"name": "A"}]}, _plan("A"))
    with app.test_client() as client:
        _login(client)
        first_page = client.get("/api/planned_route").get_json()
        db.update_mission(first, status="completed")
        db.create_mission({"init_loc": "25,121", "stops": [{"name": "B"}]}, _plan("B"))
        response = client.get(f"/api/planned_route?route_token={first_page['route_token']}")
    assert response.status_code == 200
    assert response.get_json()["route_token"] != first_page["route_token"]


def test_history_page_and_apis_are_fixed_to_selected_session(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    date_value, session_value = "2026-08-03", "12-00-00-1"
    session_dir = Path(logger.LOG_ROOT) / date_value / session_value
    session_dir.mkdir(parents=True)
    with (session_dir / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow([1, "2026-08-03 04:00:00.000Z", 25, 121, "Walking", "", db.utc_now(), "", ""])
    for name in ("all", "route", "error", "security"):
        (session_dir / f"{name}.log").write_text(f"{session_value}:{name}", encoding="utf-8")
    mission_id = db.create_mission(
        {"init_loc": "25,121", "stops": [{"name": "Historical"}]}, _plan("Historical")
    )
    db.update_mission(mission_id, log_session=str(session_dir.resolve()), status="completed")
    db.create_mission({"init_loc": "24,120", "stops": [{"name": "Live"}]}, _plan("Live"))
    with app.test_client() as client:
        _login(client)
        page = client.get(f"/history/{date_value}/{session_value}")
        status = client.get(f"/api/history/{date_value}/{session_value}/status").get_json()
        route = client.get(f"/api/history/{date_value}/{session_value}/planned_route").get_json()
        log = client.get(f"/api/history/{date_value}/{session_value}/log/route")
    assert page.status_code == 200
    assert "HISTORY" in page.get_data(as_text=True)
    assert status["mission_id"] == mission_id
    assert route["route_token"].startswith(f"{mission_id}:")
    assert log.get_data(as_text=True) == f"{session_value}:route"


def test_missing_history_log_does_not_fall_back_to_live(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)
    date_value, session_value = "2026-08-03", "13-00-00-1"
    session_dir = Path(logger.LOG_ROOT) / date_value / session_value
    session_dir.mkdir(parents=True)
    with (session_dir / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerow(logger.MOVEMENT_FIELDS)
    with app.test_client() as client:
        _login(client)
        response = client.get(f"/api/history/{date_value}/{session_value}/log/security")
    assert response.status_code == 404
    assert "no data" in response.get_data(as_text=True).lower()


def test_macrodroid_stop_action_uses_post():
    data = json.loads(Path("DigitalTwinGPS(example).category").read_text(encoding="utf-8"))
    requests = []

    def collect(value):
        if isinstance(value, dict):
            request_config = value.get("requestConfig")
            if isinstance(request_config, dict):
                requests.append(request_config)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    start = next(item for item in requests if item.get("urlToOpen", "").endswith("/start_task"))
    stop = next(item for item in requests if item.get("urlToOpen", "").endswith("/stop_task"))
    assert start["requestTimeOutSeconds"] == 30
    assert stop["requestType"] == 1
