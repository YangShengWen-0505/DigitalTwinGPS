import pytest

from digital_twin import config


def test_runtime_secrets_are_required_and_distinct(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET_KEY", "same-secret-value")
    monkeypatch.setattr(config, "API_ACCESS_KEY", "same-secret-value")
    monkeypatch.setattr(config, "FLASK_SESSION_SECRET", "different-secret-value")
    with pytest.raises(RuntimeError, match="must be distinct"):
        config.validate_runtime_config()


def test_runtime_secrets_must_be_at_least_16_characters(monkeypatch):
    monkeypatch.setattr(config, "API_SECRET_KEY", "short")
    monkeypatch.setattr(config, "API_ACCESS_KEY", "different-access-secret")
    monkeypatch.setattr(config, "FLASK_SESSION_SECRET", "different-session-secret")
    with pytest.raises(RuntimeError, match="at least 16"):
        config.validate_runtime_config()
