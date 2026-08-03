import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("DIGITAL_TWIN_DB_PATH", BASE_DIR / "data" / "digital_twin.sqlite3"))
_connections = threading.local()

MISSION_COLUMNS = {
    "id", "status", "payload_json", "plan_json", "initial_google_eta",
    "latest_google_eta", "created_at", "started_at", "finished_at",
    "cancel_requested", "completed_stops", "current_target", "last_error",
    "schedule_debt_seconds", "last_lat", "last_lng", "route_revision",
    "planned_route_points", "log_session", "worker_id",
    "is_holding_final_position", "phone_status", "phone_last_success",
    "phone_last_failure", "p2p_target",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def utc_epoch_ms() -> int:
    return int(time.time() * 1000)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    path = str(DB_PATH.resolve())
    connection = getattr(_connections, "connection", None)
    if connection is None or getattr(_connections, "path", None) != path:
        if connection is not None:
            connection.close()
        connection = sqlite3.connect(path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        _connections.connection = connection
        _connections.path = path
    try:
        yield connection
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def _prepare_schema(connection: sqlite3.Connection) -> None:
    table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='missions'"
    ).fetchone()
    if not table:
        return
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(missions)")}
    if columns == MISSION_COLUMNS:
        return
    count = int(connection.execute("SELECT COUNT(*) FROM missions").fetchone()[0])
    if count:
        raise RuntimeError(
            "The SQLite schema is outdated and contains missions; migrate or archive it before starting."
        )
    connection.execute("DROP TABLE IF EXISTS missions")
    connection.execute("DROP TABLE IF EXISTS worker_lock")


def init_db(*, interrupt_running: bool = False) -> None:
    with connect() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        _prepare_schema(connection)
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS missions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                initial_google_eta TEXT NOT NULL,
                latest_google_eta TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                completed_stops INTEGER NOT NULL DEFAULT 0,
                current_target TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                schedule_debt_seconds REAL NOT NULL DEFAULT 0,
                last_lat REAL,
                last_lng REAL,
                route_revision INTEGER NOT NULL DEFAULT 1,
                planned_route_points INTEGER NOT NULL DEFAULT 0,
                log_session TEXT UNIQUE,
                worker_id TEXT,
                is_holding_final_position INTEGER NOT NULL DEFAULT 0,
                phone_status TEXT NOT NULL DEFAULT 'standby',
                phone_last_success TEXT,
                phone_last_failure TEXT,
                p2p_target TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_missions_status ON missions(status, id);
            CREATE INDEX IF NOT EXISTS idx_missions_log_session ON missions(log_session);
            CREATE TABLE IF NOT EXISTS worker_lock (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1),
                worker_id TEXT NOT NULL,
                lease_until_ms INTEGER NOT NULL
            );
            """
        )
        if interrupt_running:
            connection.execute(
                """UPDATE missions
                   SET status='interrupted', finished_at=?, worker_id=NULL,
                       is_holding_final_position=0,
                       last_error=COALESCE(last_error, 'Worker restarted while mission was active')
                   WHERE status IN ('running', 'degraded')""",
                (utc_now(),),
            )
            connection.execute(
                """UPDATE missions SET is_holding_final_position=0, worker_id=NULL
                   WHERE is_holding_final_position=1"""
            )
            connection.execute(
                "UPDATE missions SET worker_id=NULL WHERE status='planning'"
            )


def _decode(
    row: sqlite3.Row | None,
    *,
    include_plan: bool = True,
    include_payload: bool = True,
) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    payload_json = result.pop("payload_json")
    plan_json = result.pop("plan_json")
    if include_payload:
        result["payload"] = json.loads(payload_json)
    if include_plan:
        result["plan"] = json.loads(plan_json)
    result["cancel_requested"] = bool(result["cancel_requested"])
    result["is_holding_final_position"] = bool(result["is_holding_final_position"])
    return result


def _point_count(plan: dict) -> int:
    return sum(
        len(step.get("points", []))
        for route in plan.get("routes", [])
        for step in route.get("steps", [])
    )


def create_mission(payload: dict, plan: dict | None = None, *, p2p_target: str = "") -> int:
    now = utc_now()
    status = "queued" if plan else "planning"
    stored_plan = plan or {}
    eta = plan["initial_google_eta"] if plan else ""
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """UPDATE missions SET cancel_requested=1
               WHERE status IN ('planning','queued','running','degraded')
                  OR is_holding_final_position=1"""
        )
        cursor = connection.execute(
            """INSERT INTO missions
               (status,payload_json,plan_json,initial_google_eta,latest_google_eta,
                created_at,current_target,planned_route_points,p2p_target)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                status,
                json.dumps(payload, ensure_ascii=False),
                json.dumps(stored_plan, ensure_ascii=False),
                eta,
                eta,
                now,
                payload["stops"][0]["name"],
                _point_count(stored_plan),
                p2p_target,
            ),
        )
        if not plan:
            connection.execute("UPDATE missions SET route_revision=0 WHERE id=?", (cursor.lastrowid,))
        mission_id = int(cursor.lastrowid)
        connection.execute("COMMIT")
        return mission_id


def claim_next(worker_id: str) -> dict[str, Any] | None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT id,status FROM missions
               WHERE status IN ('planning','queued') AND cancel_requested=0
               ORDER BY id LIMIT 1"""
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        if row["status"] == "queued":
            connection.execute(
                "UPDATE missions SET status='running', started_at=?, worker_id=? WHERE id=?",
                (utc_now(), worker_id, row["id"]),
            )
        else:
            connection.execute(
                "UPDATE missions SET worker_id=? WHERE id=?",
                (worker_id, row["id"]),
            )
        connection.execute("COMMIT")
    return get_mission(int(row["id"]))


def get_mission(
    mission_id: int,
    *,
    include_plan: bool = True,
    include_payload: bool = True,
) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        return _decode(row, include_plan=include_plan, include_payload=include_payload)


def latest_mission(*, include_plan: bool = True, include_payload: bool = True) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute("SELECT * FROM missions ORDER BY id DESC LIMIT 1").fetchone()
        return _decode(row, include_plan=include_plan, include_payload=include_payload)


def mission_by_log_session(
    log_session: str,
    *,
    include_plan: bool = True,
    include_payload: bool = True,
) -> dict[str, Any] | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM missions WHERE log_session=? LIMIT 1", (log_session,)
        ).fetchone()
        return _decode(row, include_plan=include_plan, include_payload=include_payload)


def is_cancel_requested(mission_id: int) -> bool:
    with connect() as connection:
        row = connection.execute(
            "SELECT cancel_requested FROM missions WHERE id=?", (mission_id,)
        ).fetchone()
        return row is None or bool(row["cancel_requested"])


def update_mission(mission_id: int, **values: Any) -> None:
    if not values:
        return
    allowed = {
        "status", "started_at", "finished_at", "cancel_requested", "completed_stops",
        "current_target", "last_error", "schedule_debt_seconds", "last_lat", "last_lng",
        "route_revision", "planned_route_points", "log_session", "worker_id",
        "is_holding_final_position", "phone_status", "phone_last_success",
        "phone_last_failure", "latest_google_eta", "plan_json",
        "initial_google_eta",
    }
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"Unsupported mission fields: {sorted(unknown)}")
    if "plan_json" in values and not isinstance(values["plan_json"], str):
        values["plan_json"] = json.dumps(values["plan_json"], ensure_ascii=False)
    assignments = ", ".join(f"{key}=?" for key in values)
    with connect() as connection:
        connection.execute(
            f"UPDATE missions SET {assignments} WHERE id=?",
            (*values.values(), mission_id),
        )


def request_cancel() -> int | None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """SELECT id FROM missions
               WHERE status IN ('planning','queued','running','degraded')
                  OR is_holding_final_position=1
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            connection.execute("COMMIT")
            return None
        connection.execute("UPDATE missions SET cancel_requested=1 WHERE id=?", (row["id"],))
        connection.execute("COMMIT")
        return int(row["id"])


def acquire_worker(worker_id: str, lease_until_ms: int) -> bool:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT worker_id, lease_until_ms FROM worker_lock WHERE singleton=1"
        ).fetchone()
        if row and row["worker_id"] != worker_id and int(row["lease_until_ms"]) > utc_epoch_ms():
            connection.execute("COMMIT")
            return False
        connection.execute(
            """INSERT INTO worker_lock(singleton,worker_id,lease_until_ms) VALUES(1,?,?)
               ON CONFLICT(singleton) DO UPDATE SET
                   worker_id=excluded.worker_id, lease_until_ms=excluded.lease_until_ms""",
            (worker_id, lease_until_ms),
        )
        connection.execute("COMMIT")
        return True


def renew_worker(worker_id: str, lease_until_ms: int) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            "UPDATE worker_lock SET lease_until_ms=? WHERE singleton=1 AND worker_id=?",
            (lease_until_ms, worker_id),
        )
        return cursor.rowcount == 1


def release_worker(worker_id: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM worker_lock WHERE singleton=1 AND worker_id=?", (worker_id,))
