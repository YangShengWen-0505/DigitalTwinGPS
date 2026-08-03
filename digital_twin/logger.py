import csv
import io
import json
import logging
import os
import shutil
import sys
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_ROOT = Path(os.getenv("DIGITAL_TWIN_LOG_ROOT", BASE_DIR / "logs"))
ARCHIVE_ROOT = LOG_ROOT / "archives"
ARCHIVE_CACHE_ROOT = LOG_ROOT / ".archive-cache"
HISTORY_FILES = {
    "all": "all.log",
    "route": "route.log",
    "error": "error.log",
    "security": "security.log",
    "movement": "movement.csv",
    "mission": "mission.json",
}
MOVEMENT_FIELDS = [
    "Sequence", "Timestamp", "Latitude", "Longitude", "Action", "Note",
    "TimestampISO", "DeltaSeconds", "DistanceMeters",
]

current_session_dir: Path | None = None
current_csv_file: Path | None = None
_csv_file_handle = None
_csv_writer = None
_movement_sequence = 0
_csv_lock = threading.Lock()
_handler_lock = threading.Lock()
_process_handler: logging.Handler | None = None

sys_logger = logging.getLogger("DigitalTwin.system")
route_logger = logging.getLogger("DigitalTwin.route")
security_logger = logging.getLogger("DigitalTwin.security")


class TerminalFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or record.name == route_logger.name


def _formatter(with_date: bool = False) -> logging.Formatter:
    datefmt = "%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S"
    return logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt=datefmt)


def _file_handler(path: Path, level: int, with_date: bool = False) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(_formatter(with_date=with_date))
    return handler


def _rotating_handler(path: Path, level: int) -> RotatingFileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setLevel(level)
    handler.setFormatter(_formatter(with_date=True))
    return handler


def _terminal_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(TerminalFilter())
    return handler


def _attach_handlers(
    logger_obj: logging.Logger,
    handlers: list[logging.Handler],
    level: int = logging.DEBUG,
) -> None:
    removed: list[logging.Handler] = []
    for old_handler in logger_obj.handlers[:]:
        if old_handler not in handlers:
            removed.append(old_handler)
        logger_obj.removeHandler(old_handler)
    logger_obj.setLevel(level)
    logger_obj.propagate = False
    for handler in handlers:
        logger_obj.addHandler(handler)
    active = {
        handler
        for managed_logger in (sys_logger, route_logger, security_logger)
        for handler in managed_logger.handlers
    }
    for old_handler in removed:
        if old_handler not in active:
            old_handler.close()


def init_server_logs(process_name: str) -> None:
    global _process_handler
    safe_name = "worker" if process_name == "worker" else "web"
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    with _handler_lock:
        _process_handler = _rotating_handler(LOG_ROOT / f"{safe_name}.log", logging.DEBUG)
        _attach_handlers(sys_logger, [_process_handler, _terminal_handler()])
        _attach_handlers(security_logger, [_process_handler])
        _attach_handlers(route_logger, [_process_handler, _terminal_handler()])


def _close_csv() -> None:
    global _csv_file_handle, _csv_writer
    with _csv_lock:
        if _csv_file_handle:
            try:
                _csv_file_handle.flush()
                _csv_file_handle.close()
            finally:
                _csv_file_handle = None
                _csv_writer = None


def init_task_logs(mission_id: int | None = None) -> dict[str, str | None]:
    global current_session_dir, current_csv_file
    global _csv_file_handle, _csv_writer, _movement_sequence

    _close_csv()
    now = datetime.now(timezone.utc)
    suffix = f"-{mission_id}" if mission_id is not None else ""
    current_session_dir = LOG_ROOT / now.strftime("%Y-%m-%d") / f"{now.strftime('%H-%M-%S')}{suffix}"
    current_session_dir.mkdir(parents=True, exist_ok=True)
    current_csv_file = current_session_dir / "movement.csv"
    _movement_sequence = 0

    with _csv_lock:
        _csv_file_handle = current_csv_file.open(mode="w", newline="", encoding="utf-8")
        _csv_writer = csv.writer(_csv_file_handle)
        _csv_writer.writerow(MOVEMENT_FIELDS)
        _csv_file_handle.flush()

    all_handler = _file_handler(current_session_dir / "all.log", logging.DEBUG)
    error_handler = _file_handler(current_session_dir / "error.log", logging.WARNING)
    route_handler = _file_handler(current_session_dir / "route.log", logging.INFO)
    security_handler = _file_handler(current_session_dir / "security.log", logging.INFO)
    process_handlers = [_process_handler] if _process_handler else []
    with _handler_lock:
        _attach_handlers(sys_logger, [all_handler, error_handler, *process_handlers, _terminal_handler()])
        _attach_handlers(route_logger, [route_handler, all_handler, *process_handlers, _terminal_handler()])
        _attach_handlers(security_logger, [security_handler, all_handler, *process_handlers])

    log_sys(f"Task log session created: {current_session_dir}")
    return get_log_status()


def get_log_status() -> dict[str, str | None]:
    return {
        "session_dir": str(current_session_dir.resolve()) if current_session_dir else None,
        "movement_csv": str(current_csv_file.resolve()) if current_csv_file else None,
    }


def write_mission_snapshot(mission: dict) -> None:
    if not current_session_dir:
        return
    try:
        snapshot = {"created_at": datetime.now(timezone.utc).isoformat(), "mission": mission}
        (current_session_dir / "mission.json").write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        log_sys(f"Mission snapshot write failed: {exc}", "warning")


def _validate_history_parts(date_value: str, session_value: str) -> None:
    try:
        datetime.strptime(date_value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise FileNotFoundError("History session not found") from exc
    if not session_value or any(char not in "0123456789-" for char in session_value):
        raise FileNotFoundError("History session not found")


def _resolve_history_session(date_value: str, session_value: str) -> Path:
    _validate_history_parts(date_value, session_value)
    session_dir = (LOG_ROOT / date_value / session_value).resolve()
    if LOG_ROOT.resolve() not in session_dir.parents or not session_dir.is_dir():
        raise FileNotFoundError("History session not found")
    return session_dir


def _read_mission_metadata(session_dir: Path) -> dict:
    path = session_dir / "mission.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _list_unarchived_sessions(limit: int = 100) -> list[dict]:
    sessions: list[dict] = []
    if not LOG_ROOT.exists():
        return sessions
    for date_dir in sorted(LOG_ROOT.iterdir(), reverse=True):
        if not date_dir.is_dir() or date_dir.name.startswith(".") or date_dir == ARCHIVE_ROOT:
            continue
        for session_dir in sorted(date_dir.iterdir(), reverse=True):
            movement = session_dir / "movement.csv"
            if not session_dir.is_dir() or not movement.exists():
                continue
            metadata = _read_mission_metadata(session_dir)
            mission = metadata.get("mission", {})
            payload = mission.get("payload", mission)
            stat = movement.stat()
            sessions.append({
                "date": date_dir.name,
                "session": session_dir.name,
                "id": f"{date_dir.name}/{session_dir.name}",
                "created_at": metadata.get("created_at"),
                "start": payload.get("init_loc", ""),
                "stops": len(payload.get("stops", [])),
                "csv_size": stat.st_size,
                "updated_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "archived": False,
            })
            if len(sessions) >= limit:
                return sessions
    return sessions


def _archive_metadata() -> list[dict]:
    if not ARCHIVE_ROOT.exists():
        return []
    result = []
    for path in sorted(ARCHIVE_ROOT.glob("*.zip"), reverse=True):
        if "__" not in path.stem:
            continue
        date_value, session_value = path.stem.split("__", 1)
        result.append({
            "date": date_value,
            "session": session_value,
            "id": f"{date_value}/{session_value}",
            "archived": True,
            "csv_size": path.stat().st_size,
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        })
    return result


def list_history_sessions(limit: int = 100) -> list[dict]:
    sessions = _list_unarchived_sessions(limit)
    if len(sessions) < limit:
        sessions.extend(_archive_metadata()[: limit - len(sessions)])
    return sessions


def _archive_path(date_value: str, session_value: str) -> Path:
    _validate_history_parts(date_value, session_value)
    return ARCHIVE_ROOT / f"{date_value}__{session_value}.zip"


def _touch_cache(target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / ".touched").touch()


def resolve_history_file(date_value: str, session_value: str, kind: str) -> Path:
    filename = HISTORY_FILES.get(kind)
    if not filename:
        raise ValueError("Unsupported history file")
    try:
        path = _resolve_history_session(date_value, session_value) / filename
        if not path.exists():
            raise FileNotFoundError(f"History {kind} data not found")
        return path
    except FileNotFoundError:
        archive = _archive_path(date_value, session_value)
        if not archive.exists():
            raise
        target = ARCHIVE_CACHE_ROOT / date_value / session_value
        path = target / filename
        _touch_cache(target)
        if not path.exists():
            with zipfile.ZipFile(archive, "r") as bundle:
                try:
                    with bundle.open(filename) as source, path.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                except KeyError as exc:
                    raise FileNotFoundError(f"Archived {kind} data not found") from exc
        return path


def resolve_history_movement(date_value: str, session_value: str) -> Path:
    return resolve_history_file(date_value, session_value, "movement")


def history_log_session_id(date_value: str, session_value: str) -> str:
    _validate_history_parts(date_value, session_value)
    return str((LOG_ROOT / date_value / session_value).resolve())


def read_history_log(date_value: str, session_value: str, log_name: str) -> str:
    return resolve_history_file(date_value, session_value, log_name).read_text(encoding="utf-8")


def read_movement_records(path: Path, offset: int = 0, limit: int = 250) -> dict:
    if not path.exists():
        raise FileNotFoundError("Movement data not found")
    size = path.stat().st_size
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 1000))
    if offset > size:
        offset = 0
    records = []
    with path.open("rb") as handle:
        handle.seek(offset)
        if offset == 0:
            handle.readline()
        while len(records) < limit:
            raw = handle.readline()
            if not raw:
                break
            try:
                row = next(csv.reader(io.StringIO(raw.decode("utf-8"))))
                values = dict(zip(MOVEMENT_FIELDS, row, strict=False))
                records.append({
                    "sequence": int(values["Sequence"]),
                    "time": values["TimestampISO"],
                    "lat": float(values["Latitude"]),
                    "lng": float(values["Longitude"]),
                    "action": values["Action"],
                    "note": values["Note"],
                })
            except (KeyError, TypeError, ValueError, csv.Error):
                continue
        next_offset = handle.tell()
    return {
        "records": records,
        "next_offset": next_offset,
        "stream_id": f"{path.parent.parent.name}/{path.parent.name}",
        "has_more": next_offset < size,
    }


def movement_path_for_session(session_dir: str | Path) -> Path:
    path = Path(session_dir).resolve() / "movement.csv"
    if LOG_ROOT.resolve() not in path.parents or not path.exists():
        raise FileNotFoundError("Movement data not found")
    return path


def log_path_for_session(session_dir: str | Path, log_name: str) -> Path:
    filename = HISTORY_FILES.get(log_name)
    if log_name not in {"all", "route", "error", "security"} or not filename:
        raise ValueError("Unsupported log name")
    path = Path(session_dir).resolve() / filename
    if LOG_ROOT.resolve() not in path.parents or not path.exists():
        raise FileNotFoundError("Log file not found")
    return path


def cleanup_archive_cache(max_age_seconds: int = 3600) -> int:
    if not ARCHIVE_CACHE_ROOT.exists():
        return 0
    cutoff = datetime.now().timestamp() - max_age_seconds
    removed = 0
    for marker in ARCHIVE_CACHE_ROOT.rglob(".touched"):
        if marker.stat().st_mtime < cutoff:
            shutil.rmtree(marker.parent, ignore_errors=True)
            removed += 1
    return removed


def _verified_archive(path: Path) -> bool:
    try:
        with zipfile.ZipFile(path, "r") as bundle:
            return bundle.testzip() is None and "movement.csv" in bundle.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def archive_expired_sessions(days: int = 30) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for date_dir in list(LOG_ROOT.iterdir()) if LOG_ROOT.exists() else []:
        if not date_dir.is_dir() or date_dir.name.startswith(".") or date_dir == ARCHIVE_ROOT:
            continue
        try:
            date_value = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if date_value >= cutoff:
            continue
        for session_dir in list(date_dir.iterdir()):
            if not session_dir.is_dir():
                continue
            archive_path = ARCHIVE_ROOT / f"{date_dir.name}__{session_dir.name}.zip"
            try:
                if not _verified_archive(archive_path):
                    temp_path = archive_path.with_suffix(".zip.tmp")
                    temp_path.unlink(missing_ok=True)
                    with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                        for file in session_dir.rglob("*"):
                            if file.is_file():
                                bundle.write(file, file.relative_to(session_dir))
                    if not _verified_archive(temp_path):
                        temp_path.unlink(missing_ok=True)
                        raise RuntimeError(f"Archive verification failed: {session_dir}")
                    temp_path.replace(archive_path)
                if not _verified_archive(archive_path):
                    raise RuntimeError(f"Archive verification failed: {archive_path}")
                shutil.rmtree(session_dir)
                archived.append(archive_path.stem)
            except Exception as exc:
                log_sys(f"History archive deferred for {session_dir}: {exc}", "warning")
        try:
            if not any(date_dir.iterdir()):
                date_dir.rmdir()
        except OSError:
            pass
    return archived


def log_sys(message: str, level: str = "info") -> None:
    getattr(sys_logger, level.lower(), sys_logger.info)(message)


def log_route(message: str) -> None:
    route_logger.info("[ROUTE] %s", message)


def log_security(message: str, level: str = "info") -> None:
    getattr(security_logger, level.lower(), security_logger.info)(message)


def log_movement(
    lat: float,
    lng: float,
    action: str,
    note: str = "",
    *,
    sent_at: float | None = None,
    delta_seconds: float | None = None,
    distance_meters: float | None = None,
) -> None:
    global _movement_sequence
    if not _csv_writer or not _csv_file_handle:
        return
    sent_dt = datetime.fromtimestamp(sent_at, timezone.utc) if sent_at else datetime.now(timezone.utc)
    timestamp_iso = sent_dt.isoformat(timespec="milliseconds")
    timestamp = sent_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"
    delta_value = "" if delta_seconds is None else f"{delta_seconds:.3f}"
    distance_value = "" if distance_meters is None else f"{distance_meters:.2f}"
    safe_action = str(action).replace("\r", " ").replace("\n", " ")
    safe_note = str(note).replace("\r", " ").replace("\n", " ")
    try:
        with _csv_lock:
            _movement_sequence += 1
            _csv_writer.writerow([
                _movement_sequence, timestamp, f"{float(lat):.7f}", f"{float(lng):.7f}",
                safe_action, safe_note, timestamp_iso, delta_value, distance_value,
            ])
            _csv_file_handle.flush()
    except Exception as exc:
        log_sys(f"CSV write failed: {exc}", "error")
