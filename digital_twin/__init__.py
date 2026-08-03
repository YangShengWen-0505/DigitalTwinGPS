import hashlib
import logging
import os

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from digital_twin import config, db, logger

NOISY_GET_PATHS = {
    "/api/system_status",
    "/api/planned_route",
    "/api/navigation_history",
    "/api/movements/current",
    "/health",
    "/favicon.ico",
}


def create_app():
    logger.init_server_logs("web")
    try:
        config.validate_runtime_config()
    except RuntimeError as exc:
        logger.log_sys(f"Configuration validation failed: {exc}", "error")
        raise
    if not config.GOOGLE_MAPS_API_KEY:
        logger.log_sys("GOOGLE_MAPS_API_KEY is not configured; mission planning will fail", "warning")
    elif config.gmaps_client is None:
        logger.log_sys("Google Maps API client initialization failed", "warning")
    db.init_db()
    app = Flask(__name__)
    if os.getenv("USE_CADDY", "false").lower() == "true":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)
    app.secret_key = hashlib.sha256(config.FLASK_SESSION_SECRET.encode("utf-8")).hexdigest()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=os.getenv("USE_CADDY", "false").lower() == "true",
        MAX_CONTENT_LENGTH=64 * 1024,
    )

    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    @app.before_request
    def log_request_info():
        # The dashboard polls these read-only endpoints frequently; keeping
        # them out of all.log makes mission events much easier to audit.
        if request.method == "GET" and (
            request.path in NOISY_GET_PATHS
            or request.path.startswith("/api/history/")
            or request.path.startswith("/static/")
        ):
            return
        content_length = request.content_length or 0
        logger.log_sys(
            f"Request: {request.method} {request.path} "
            f"from {request.remote_addr} content_length={content_length}",
            "info",
        )

    @app.after_request
    def add_security_headers(response):
        # Keep browser-facing defaults strict because this app is normally
        # exposed over Tailscale and authenticated with a shared API key.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        return response

    from digital_twin.api.routes import api_bp
    app.register_blueprint(api_bp)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app
