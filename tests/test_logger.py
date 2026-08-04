import logging
from datetime import datetime
from pathlib import Path

from mock_gps import logger


def test_task_log_status_always_returns_absolute_paths(tmp_path, monkeypatch):
    relative_root = Path("relative-test-logs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(logger, "LOG_ROOT", relative_root)
    status = logger.init_task_logs(7)
    assert Path(status["session_dir"]).is_absolute()
    assert Path(status["movement_csv"]).is_absolute()


def test_task_log_session_directory_uses_configured_timezone(tmp_path, monkeypatch):
    configured_now = datetime(2026, 8, 4, 10, 30, 45, tzinfo=logger.config.TIMEZONE)
    monkeypatch.setattr(logger, "LOG_ROOT", tmp_path / "logs")
    monkeypatch.setattr(logger.config, "local_now", lambda: configured_now)
    status = logger.init_task_logs(9)
    session = Path(status["session_dir"])
    assert session.parent.name == "2026-08-04"
    assert session.name == "10-30-45-9"


def test_log_formatter_uses_configured_timezone():
    record = logging.LogRecord("test", logging.INFO, "", 0, "message", (), None)
    record.created = datetime(2026, 8, 4, 2, 0, 0).timestamp()
    expected = datetime.fromtimestamp(record.created, logger.config.TIMEZONE).strftime("%H:%M:%S")
    assert expected in logger._formatter().format(record)


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
