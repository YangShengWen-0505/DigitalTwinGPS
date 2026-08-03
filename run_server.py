import os

from waitress import serve

from digital_twin import create_app

app = create_app()

if __name__ == "__main__":
    pc_tailscale_ip = os.getenv("PC_TAILSCALE_IP", "").strip()
    use_caddy       = os.getenv("USE_CADDY", "false").lower() == "true"
    flask_port      = int(os.getenv("FLASK_PORT", "5050"))
    bind_host       = os.getenv("BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"

    print("=====================================================")
    print("[SYSTEM] Digital Twin Server Starting")
    if use_caddy:
        print("[SYSTEM] Mode    : Caddy Reverse Proxy (HTTPS handled by Caddy)")
        print("[SYSTEM] Local   : https://localhost/map")
        if pc_tailscale_ip:
            print(f"[SYSTEM] Network : https://{pc_tailscale_ip}/map")
    else:
        print("[SYSTEM] Mode    : Local Waitress HTTP")
        print(f"[SYSTEM] Local   : http://localhost:{flask_port}/map")
        print("[SYSTEM] Network : disabled; enable Caddy for private-network HTTPS")
    print("=====================================================")

    try:
        # Waitress is production-safe on Windows. Network exposure belongs to
        # Caddy/Tailscale; the application itself listens only on localhost.
        serve(app, host=bind_host, port=flask_port, threads=8)
    except Exception as e:
        print(f"Startup Failed: {e}")
