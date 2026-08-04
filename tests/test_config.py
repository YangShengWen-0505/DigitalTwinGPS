import importlib

import pytest

from mock_gps import config


def test_runtime_secrets_are_required(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET_KEY", "password")
    monkeypatch.setattr(config, "API_ACCESS_KEY", "")
    monkeypatch.setattr(config, "FLASK_SESSION_SECRET", "password")
    with pytest.raises(RuntimeError, match="API_ACCESS_KEY"):
        config.validate_runtime_config()


def test_runtime_secrets_may_be_short_and_reused(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET_KEY", "x")
    monkeypatch.setattr(config, "API_ACCESS_KEY", "x")
    monkeypatch.setattr(config, "FLASK_SESSION_SECRET", "x")
    config.validate_runtime_config()


def test_invalid_timezone_falls_back_instead_of_breaking_import(monkeypatch):
    # A typo in TZ used to raise ZoneInfoNotFoundError during import, killing
    # the app before validate_runtime_config() could report anything readable.
    monkeypatch.setenv("TZ", "Asia/Taipeii")
    reloaded = importlib.reload(config)
    try:
        assert str(reloaded.TIMEZONE) == "Asia/Taipei"
        assert "Asia/Taipeii" in reloaded.TIMEZONE_WARNING
    finally:
        monkeypatch.delenv("TZ", raising=False)
        importlib.reload(config)


def test_valid_timezone_produces_no_warning(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    reloaded = importlib.reload(config)
    try:
        assert str(reloaded.TIMEZONE) == "UTC"
        assert reloaded.TIMEZONE_WARNING == ""
    finally:
        monkeypatch.delenv("TZ", raising=False)
        importlib.reload(config)


def test_shared_bind_host_keeps_loopback_listening():
    # Binding only the Tailscale address silently kills
    # http://127.0.0.1:5050/map, so the local dashboard must always keep its
    # own listener. 100.64.0.0/10 is the CGNAT range Tailscale assigns from.
    assert config.listen_spec("100.64.0.1", 5050) == [
        "127.0.0.1:5050",
        "100.64.0.1:5050",
    ]


def test_loopback_bind_host_does_not_duplicate_listeners():
    for value in ("127.0.0.1", "localhost", "", "  "):
        assert config.listen_spec(value, 5050) == ["127.0.0.1:5050"]


def test_wildcard_bind_host_is_left_alone():
    # 0.0.0.0 already covers loopback; adding it again would fail to bind.
    assert config.listen_spec("0.0.0.0", 5050) == ["0.0.0.0:5050"]


def test_ipv6_bind_host_is_bracketed_for_waitress():
    assert config.listen_spec("fd7a::1", 5050) == ["127.0.0.1:5050", "[fd7a::1]:5050"]
