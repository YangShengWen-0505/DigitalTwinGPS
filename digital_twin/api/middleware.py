import hmac
import threading
import time
from collections import deque
from functools import wraps

from flask import jsonify, request, session

from digital_twin import config, logger

_api_attempts: dict[str, deque[float]] = {}
_api_attempts_lock = threading.Lock()
_WINDOW_SECONDS = 300
_MAX_ATTEMPTS = 5
_MAX_SOURCES = 1024


def _record_api_failure(address: str) -> bool:
    now = time.monotonic()
    with _api_attempts_lock:
        for key in list(_api_attempts):
            attempts = _api_attempts[key]
            while attempts and now - attempts[0] > _WINDOW_SECONDS:
                attempts.popleft()
            if not attempts:
                _api_attempts.pop(key, None)
        if address not in _api_attempts and len(_api_attempts) >= _MAX_SOURCES:
            oldest = min(_api_attempts, key=lambda key: _api_attempts[key][-1])
            _api_attempts.pop(oldest, None)
        attempts = _api_attempts.setdefault(address, deque())
        if len(attempts) >= _MAX_ATTEMPTS:
            return True
        attempts.append(now)
        return False


def clear_api_failures(address: str) -> None:
    with _api_attempts_lock:
        _api_attempts.pop(address, None)


def mask_secret(value: str | None) -> str:
    if not value:
        return "None"
    return f"{value[:4]}..." if len(value) > 4 else "****"


def has_valid_api_key() -> bool:
    api_key = request.headers.get("X-API-Key", "")
    # Constant-time comparison avoids leaking secret validity through timing.
    return bool(api_key) and hmac.compare_digest(api_key, config.API_ACCESS_KEY)


def has_valid_session() -> bool:
    return bool(session.get("authenticated"))


def require_api_key(allow_session: bool = True):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            address = request.remote_addr or "unknown"
            if has_valid_api_key():
                clear_api_failures(address)
                return f(*args, **kwargs)
            if allow_session and has_valid_session():
                return f(*args, **kwargs)

            if _record_api_failure(address):
                return jsonify({"error": "Too many attempts"}), 429

            logger.log_security(
                # Store only a masked key prefix so security logs remain useful
                # without becoming a source of credential disclosure.
                f"Unauthorized request: {request.method} {request.path} "
                f"from {address}; key={mask_secret(request.headers.get('X-API-Key'))}",
                "warning",
            )
            return jsonify({"error": "Unauthorized"}), 401

        return decorated_function

    return decorator
