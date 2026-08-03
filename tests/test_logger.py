import csv
import logging
from datetime import datetime, timedelta
from pathlib import Path

from digital_twin import logger


def _row(sequence: int, action: str = "Walking", note: str = "") -> list[str]:
    return [
        str(sequence), "2026-01-01 00:00:00.000Z", "25", "121", action, note,
        "2026-01-01T00:00:00.000+00:00", "1.000", "0.00",
    ]


def test_movement_cursor_preserves_sequence_across_pages(tmp_path):
    path = tmp_path / "movement.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
        writer.writerow(_row(2, "MRT", "台北車站"))
    first = logger.read_movement_records(path, 0, limit=1)
    second = logger.read_movement_records(path, first["next_offset"], limit=1)
    assert [first["records"][0]["sequence"], second["records"][0]["sequence"]] == [1, 2]
    assert second["records"][0]["note"] == "台北車站"
    assert first["has_more"] is True
    assert second["has_more"] is False


def test_cursor_resets_when_offset_exceeds_file(tmp_path):
    path = tmp_path / "movement.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
    page = logger.read_movement_records(path, 99999)
    assert page["records"][0]["sequence"] == 1


def test_task_log_status_always_returns_absolute_paths(tmp_path, monkeypatch):
    relative_root = Path("relative-test-logs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logger, "LOG_ROOT", relative_root)
    status = logger.init_task_logs(7)
    assert Path(status["session_dir"]).is_absolute()
    assert Path(status["movement_csv"]).is_absolute()


def test_shared_handler_is_closed_only_after_all_loggers_release_it():
    shared = logging.StreamHandler()
    originals = {
        managed: managed.handlers[:]
        for managed in (logger.sys_logger, logger.route_logger, logger.security_logger)
    }
    try:
        logger.sys_logger.handlers = [shared]
        logger.route_logger.handlers = [shared]
        logger.security_logger.handlers = []
        logger._attach_handlers(logger.sys_logger, [])
        assert not shared._closed
        logger._attach_handlers(logger.route_logger, [])
        assert shared._closed
    finally:
        for managed, handlers in originals.items():
            managed.handlers = handlers


def test_expired_session_is_verified_archived_and_all_files_replayable(tmp_path, monkeypatch):
    log_root = tmp_path / "logs"
    archive_root = log_root / "archives"
    cache_root = log_root / ".archive-cache"
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    session = log_root / old_date / "12-00-00"
    session.mkdir(parents=True)
    with (session / "movement.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(logger.MOVEMENT_FIELDS)
        writer.writerow(_row(1))
    (session / "route.log").write_text("route data", encoding="utf-8")
    monkeypatch.setattr(logger, "LOG_ROOT", log_root)
    monkeypatch.setattr(logger, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(logger, "ARCHIVE_CACHE_ROOT", cache_root)
    archived = logger.archive_expired_sessions(30)
    assert archived == [f"{old_date}__12-00-00"]
    assert not session.exists()
    assert logger.read_history_log(old_date, "12-00-00", "route") == "route data"
    restored = logger.resolve_history_movement(old_date, "12-00-00")
    assert restored.exists()
    assert (restored.parent / ".touched").exists()


def test_invalid_existing_archive_is_rebuilt_before_source_deletion(tmp_path, monkeypatch):
    log_root = tmp_path / "logs"
    archive_root = log_root / "archives"
    old_date = (datetime.now() - timedelta(days=31)).strftime("%Y-%m-%d")
    session = log_root / old_date / "12-00-00"
    session.mkdir(parents=True)
    (session / "movement.csv").write_text("broken source", encoding="utf-8")
    archive_root.mkdir(parents=True)
    (archive_root / f"{old_date}__12-00-00.zip").write_bytes(b"not a zip")
    monkeypatch.setattr(logger, "LOG_ROOT", log_root)
    monkeypatch.setattr(logger, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(logger, "ARCHIVE_CACHE_ROOT", log_root / ".archive-cache")
    assert logger.archive_expired_sessions(30) == [f"{old_date}__12-00-00"]
    assert not session.exists()
    assert logger._verified_archive(archive_root / f"{old_date}__12-00-00.zip")
