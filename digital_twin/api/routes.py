import hmac
import threading
import time
from collections import deque
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from digital_twin import config, db, logger
from digital_twin.api.middleware import has_valid_api_key, require_api_key

api_bp = Blueprint("api", __name__)
ALLOWED_MODES = {"walking", "transit", "motorcycle"}
ALLOWED_TRANSIT_TYPES = {"", "AUTO", "MRT", "BUS"}
ALLOWED_LOGS = {"all", "route", "error", "security"}
_login_attempts: dict[str, deque[float]] = {}
_login_attempts_lock = threading.Lock()


def _parse_coord(value: str, field_name: str) -> tuple[float, float]:
    if not isinstance(value, str) or "," not in value:
        raise ValueError(f"{field_name} must be 'lat,lng'")
    lat_raw, lng_raw = value.split(",", 1)
    lat, lng = float(lat_raw.strip()), float(lng_raw.strip())
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise ValueError(f"{field_name} is out of range")
    return lat, lng


def _validate_stop(stop: dict, index: int) -> dict:
    if not isinstance(stop, dict):
        raise ValueError(f"stops[{index}] must be an object")
    name = str(stop.get("name", "")).strip()
    mode = str(stop.get("mode", "")).strip()
    transit_type = str(stop.get("transit_type", "")).strip().upper()
    wait_time = str(stop.get("wait_time", "")).strip()
    coord = str(stop.get("coord", "")).strip()
    if not name:
        raise ValueError(f"stops[{index}].name is required")
    if mode not in ALLOWED_MODES:
        raise ValueError(f"stops[{index}].mode must be one of {sorted(ALLOWED_MODES)}")
    if transit_type not in ALLOWED_TRANSIT_TYPES:
        raise ValueError(f"stops[{index}].transit_type must be AUTO, MRT, BUS, or empty")
    if transit_type == "AUTO" or mode != "transit":
        transit_type = ""
    if wait_time:
        try:
            datetime.strptime(wait_time.replace("24:", "00:"), "%H:%M")
        except ValueError as exc:
            raise ValueError(f"stops[{index}].wait_time must use HH:MM format") from exc
    if coord:
        _parse_coord(coord, f"stops[{index}].coord")
    return {
        "name": name,
        "mode": mode,
        "transit_type": transit_type,
        "wait_time": wait_time,
        "skip_if_late": bool(stop.get("skip_if_late", False)),
        "coord": coord,
    }


def _validate_mission_payload(data: dict) -> tuple[str, list[dict]]:
    if not isinstance(data, dict):
        raise ValueError("JSON body is required")
    init_loc = str(data.get("init_loc", "")).strip()
    stops = data.get("stops", [])
    _parse_coord(init_loc, "init_loc")
    if not isinstance(stops, list) or not stops:
        raise ValueError("stops must be a non-empty array")
    if len(stops) > 50:
        raise ValueError("stops cannot exceed 50")
    return init_loc, [_validate_stop(stop, index) for index, stop in enumerate(stops)]


def _record_login_failure(address: str) -> bool:
    now = time.monotonic()
    with _login_attempts_lock:
        for key in list(_login_attempts):
            attempts = _login_attempts[key]
            while attempts and now - attempts[0] > 300:
                attempts.popleft()
            if not attempts:
                _login_attempts.pop(key, None)
        if address not in _login_attempts and len(_login_attempts) >= 1024:
            oldest = min(_login_attempts, key=lambda key: _login_attempts[key][-1])
            _login_attempts.pop(oldest, None)
        attempts = _login_attempts.setdefault(address, deque())
        if len(attempts) >= 5:
            return True
        attempts.append(now)
        return False


def _clear_login_failures(address: str) -> None:
    with _login_attempts_lock:
        _login_attempts.pop(address, None)


def _status_payload(mission: dict | None) -> dict:
    if not mission:
        return {
            "mission_active": False,
            "mission_stats": {
                "status": "idle", "total_stops": 0,
                "completed_stops": 0, "current_target": "",
                "schedule_debt_seconds": 0,
            },
            "last_position": {"lat": None, "lng": None},
            "phone_status": "standby",
            "planned_route_points": 0,
            "is_holding_final_position": False,
        }
    started = datetime.fromisoformat(mission["started_at"]) if mission["started_at"] else None
    finished = datetime.fromisoformat(mission["finished_at"]) if mission["finished_at"] else None
    elapsed_end = finished or datetime.now(timezone.utc)
    elapsed = int((elapsed_end - started).total_seconds()) if started else None
    payload = mission.get("payload", {})
    return {
        "mission_id": mission["id"],
        "mission_active": mission["status"] in {"planning", "queued", "running", "degraded"}
        or mission["is_holding_final_position"],
        "is_holding_final_position": mission["is_holding_final_position"],
        "mission_stats": {
            "status": mission["status"],
            "total_stops": len(payload.get("stops", [])),
            "completed_stops": mission["completed_stops"],
            "current_target": mission["current_target"],
            "last_error": mission["last_error"],
            "schedule_debt_seconds": mission["schedule_debt_seconds"],
        },
        "started_at": mission["started_at"],
        "finished_at": mission["finished_at"],
        "elapsed_seconds": elapsed,
        "last_position": {"lat": mission["last_lat"], "lng": mission["last_lng"]},
        "planned_route_points": mission["planned_route_points"],
        "route_token": f"{mission['id']}:{mission['route_revision']}",
        "initial_google_eta": mission["initial_google_eta"],
        "latest_google_eta": mission["latest_google_eta"],
        "phone_status": mission["phone_status"],
        "phone_last_success": mission["phone_last_success"],
        "phone_last_failure": mission["phone_last_failure"],
        "p2p_target": mission["p2p_target"],
        "log_session": mission["log_session"],
    }


def _route_payload(mission: dict) -> dict:
    points = [
        {"lat": point[0], "lng": point[1]}
        for route in mission["plan"].get("routes", [])
        for step in route.get("steps", [])
        for point in step.get("points", [])
    ]
    return {
        "route_token": f"{mission['id']}:{mission['route_revision']}",
        "points": points,
    }


def _history_mission(date_value: str, session_value: str, *, include_plan: bool) -> dict | None:
    log_session = logger.history_log_session_id(date_value, session_value)
    return db.mission_by_log_session(log_session, include_plan=include_plan)


@api_bp.route("/")
def index():
    return redirect(url_for("api.show_map"))


@api_bp.route("/login", methods=["GET", "POST"])
def login():
    address = request.remote_addr or "unknown"
    if request.method == "POST":
        key = request.form.get("api_key", "")
        if key and hmac.compare_digest(key, config.API_SECRET_KEY):
            _clear_login_failures(address)
            session["authenticated"] = True
            logger.log_security(f"Dashboard login succeeded from {address}")
            return redirect(url_for("api.show_map"))
        if _record_login_failure(address):
            return render_template("login.html", error="Too many attempts. Try again later."), 429
        logger.log_security(f"Dashboard login failed from {address}", "warning")
        return render_template("login.html", error="Invalid API key."), 401
    return render_template("login.html", error="")


@api_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("api.login"))


@api_bp.route("/stop_task", methods=["POST"])
@require_api_key(allow_session=False)
def stop_task_route():
    mission_id = db.request_cancel()
    return jsonify({"status": "Cancellation requested", "mission_id": mission_id})


@api_bp.route("/start_task", methods=["POST"])
@require_api_key(allow_session=False)
def start_task():
    try:
        init_loc, stops = _validate_mission_payload(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    mission_id = db.create_mission(
        {"init_loc": init_loc, "stops": stops},
        p2p_target=config.PHONE_TAILSCALE_IP,
    )
    response = jsonify({
        "status": "Mission planning",
        "mission_id": mission_id,
        "initial_google_eta": None,
    })
    response.headers["Location"] = url_for("api.get_system_status")
    return response, 202


@api_bp.route("/api/system_status")
@require_api_key()
def get_system_status():
    return jsonify(_status_payload(db.latest_mission(include_plan=False)))


@api_bp.route("/api/planned_route")
@require_api_key()
def get_planned_route():
    mission = db.latest_mission()
    if not mission:
        return jsonify({"route_token": "", "points": []})
    token = f"{mission['id']}:{mission['route_revision']}"
    if request.args.get("route_token", "") == token:
        return "", 304
    return jsonify(_route_payload(mission))


@api_bp.route("/api/navigation_history")
@require_api_key()
def get_navigation_history():
    mission = db.latest_mission()
    return jsonify(mission["plan"].get("routes", []) if mission else [])


@api_bp.route("/api/movements/current")
@require_api_key()
def current_movements():
    mission = db.latest_mission(include_plan=False, include_payload=False)
    if not mission or not mission["log_session"]:
        return jsonify({
            "records": [], "next_offset": 0, "stream_id": None, "has_more": False,
        })
    try:
        path = logger.movement_path_for_session(mission["log_session"])
        return jsonify(logger.read_movement_records(
            path,
            request.args.get("offset", type=int, default=0),
            request.args.get("limit", type=int, default=250),
        ))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@api_bp.route("/api/mission")
@require_api_key()
def get_mission_data():
    mission = db.latest_mission(include_plan=False)
    return jsonify(mission["payload"] if mission else {"init_loc": "", "stops": []})


@api_bp.route("/api/history")
@require_api_key()
def get_history_sessions():
    limit = min(max(request.args.get("limit", type=int, default=100), 1), 500)
    return jsonify(logger.list_history_sessions(limit))


@api_bp.route("/api/history/<date_value>/<session_value>/status")
@require_api_key()
def history_status(date_value: str, session_value: str):
    try:
        mission = _history_mission(date_value, session_value, include_plan=False)
        if not mission:
            return jsonify({"error": "This session has no status data"}), 404
        return jsonify(_status_payload(mission))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@api_bp.route("/api/history/<date_value>/<session_value>/planned_route")
@require_api_key()
def history_planned_route(date_value: str, session_value: str):
    try:
        mission = _history_mission(date_value, session_value, include_plan=True)
        if not mission:
            return jsonify({"error": "This session has no planned route data"}), 404
        return jsonify(_route_payload(mission))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@api_bp.route("/api/history/<date_value>/<session_value>/navigation")
@require_api_key()
def history_navigation(date_value: str, session_value: str):
    try:
        mission = _history_mission(date_value, session_value, include_plan=True)
        if not mission:
            return jsonify({"error": "This session has no navigation data"}), 404
        return jsonify(mission["plan"]["routes"])
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@api_bp.route("/api/history/<date_value>/<session_value>/movements")
@require_api_key()
def history_movements(date_value: str, session_value: str):
    try:
        path = logger.resolve_history_movement(date_value, session_value)
        return jsonify(logger.read_movement_records(
            path,
            request.args.get("offset", type=int, default=0),
            request.args.get("limit", type=int, default=250),
        ))
    except FileNotFoundError as exc:
        return jsonify({"error": str(exc)}), 404


@api_bp.route("/api/history/<date_value>/<session_value>/log/<log_name>")
@require_api_key()
def history_log(date_value: str, session_value: str, log_name: str):
    if log_name not in ALLOWED_LOGS:
        return "Unsupported log name", 400
    try:
        content = logger.read_history_log(date_value, session_value, log_name)
        return content, 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return "This session has no data for this log", 404


@api_bp.route("/api/log/<log_name>")
@require_api_key()
def get_log_data(log_name: str):
    if log_name not in ALLOWED_LOGS:
        return "Unsupported log name", 400
    mission = db.latest_mission(include_plan=False, include_payload=False)
    if not mission or not mission["log_session"]:
        return "Task not started", 404
    try:
        path = logger.log_path_for_session(mission["log_session"], log_name)
        return path.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain; charset=utf-8"}
    except FileNotFoundError:
        return "Log file not found", 404


def _authenticated() -> bool:
    return bool(session.get("authenticated")) or has_valid_api_key()


@api_bp.route("/map")
def show_map():
    if not _authenticated():
        return redirect(url_for("api.login"))
    return render_template("map.html", page_context={"mode": "live"})


@api_bp.route("/history/<date_value>/<session_value>")
def show_history(date_value: str, session_value: str):
    if not _authenticated():
        return redirect(url_for("api.login"))
    try:
        logger.resolve_history_movement(date_value, session_value)
    except FileNotFoundError:
        return "History session not found", 404
    return render_template(
        "map.html",
        page_context={
            "mode": "history",
            "date": date_value,
            "session": session_value,
            "id": f"{date_value}/{session_value}",
        },
    )
