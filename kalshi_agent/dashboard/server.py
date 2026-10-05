"""Dashboard web server: JSON API + the single-page dashboard.

Built on Python's standard library only, so it installs on any Mac.
Security: listens on 127.0.0.1 (this computer only) by default. If it is ever
bound to another address, a DASHBOARD_PASSWORD is required (HTTP Basic auth).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .. import state
from ..config import Settings, load_settings
from ..db import Database, open_db
from ..safety import engage_kill, release_kill

STATIC = Path(__file__).with_name("static")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
log = logging.getLogger("dashboard")


class HTTPError(Exception):
    def __init__(self, status: int, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status, self.message, self.headers = status, message, headers or {}


class DashboardApp:
    """Routes requests to handlers. Kept separate from the HTTP plumbing so it is easy to test."""

    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.get_routes: dict[str, Callable[[str], Any]] = {
            "/api/overview": self.overview,
            "/api/trades": lambda u: state.recent_trades(self.db),
            "/api/signals": lambda u: state.recent_signals(self.db),
            "/api/research": lambda u: state.research(self.db),
            "/api/quality": lambda u: state.data_quality(self.db, self.s),
            "/api/model": lambda u: state.model_info(self.db, self.s),
            "/api/health": lambda u: state.status(self.db, self.s)["health"] or {"overall": "unknown", "checks": []},
        }

    def authenticate(self, auth_header: str | None) -> str:
        if not self.s.dashboard_password:
            if self.s.dashboard_host in LOCAL_HOSTS:
                return "local"
            raise HTTPError(503, "Dashboard password not configured")
        user, pw = "", ""
        if auth_header and auth_header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(auth_header[6:]).decode().partition(":")
            except (binascii.Error, UnicodeDecodeError):
                pass
        if not secrets.compare_digest(pw.encode(), self.s.dashboard_password.encode()):
            raise HTTPError(401, "Login required", {"WWW-Authenticate": 'Basic realm="kalshi-agent"'})
        return user or "user"

    def overview(self, user: str) -> dict[str, Any]:
        return {
            "status": state.status(self.db, self.s),
            "markets": state.markets(self.db, self.s),
            "gates": state.trade_gates(self.db, self.s),
            "performance": {m: state.performance(self.db, m) for m in ("PAPER", "DEMO", "LIVE")},
            "costs": state.costs(self.s),
            "data": state.data_stats(self.db),
            "events": state.events(self.db, 25),
            "crypto": {sym: state.crypto_series(self.db, sym, 60) for sym in self.s.symbols},
            "feeds": state.feeds(self.db, self.s),
            "positions": state.open_positions(self.db, "PAPER"),
            "readiness": state.live_readiness(self.db, self.s),
        }

    def post(self, path: str, user: str) -> dict[str, Any]:
        if path == "/api/kill":
            engage_kill(self.db, f"dashboard:{user}", "kill button pressed")
            return {"ok": True}
        if path == "/api/unkill":
            release_kill(self.db, f"dashboard:{user}")
            return {"ok": True, "note": "Only the dashboard kill was released. Config/file/env kills stay until removed."}
        if path.startswith("/api/collector/"):
            action = path.rsplit("/", 1)[1]
            if action not in ("pause", "resume"):
                raise HTTPError(400, "action must be pause or resume")
            self.db.set_control("collector", "paused" if action == "pause" else "running", f"dashboard:{user}")
            self.db.log_event("dashboard", "info", f"Collector {action}d by {user}")
            return {"ok": True}
        raise HTTPError(404, "not found")

    def handle(self, method: str, path: str, auth_header: str | None) -> tuple[int, str, bytes]:
        """Returns (status, content_type, body). Raises HTTPError for errors."""
        path = path.split("?", 1)[0]
        user = self.authenticate(auth_header)
        if method == "GET" and path in ("/", "/index.html"):
            return 200, "text/html; charset=utf-8", (STATIC / "index.html").read_bytes()
        if method == "GET" and path in self.get_routes:
            return 200, "application/json", json.dumps(self.get_routes[path](user), default=str).encode()
        if method == "POST":
            return 200, "application/json", json.dumps(self.post(path, user)).encode()
        raise HTTPError(404, "not found")


def make_handler(app: DashboardApp) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "kalshi-agent"

        def _respond(self, status: int, ctype: str, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            try:
                status, ctype, body = app.handle(method, self.path, self.headers.get("Authorization"))
                self._respond(status, ctype, body)
            except HTTPError as e:
                self._respond(e.status, "application/json", json.dumps({"error": e.message}).encode(), e.headers)
            except Exception:
                log.exception("Dashboard error on %s %s", method, self.path)
                self._respond(500, "application/json", b'{"error": "internal error"}')

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(min(length, 10_000))
            self._dispatch("POST")

        def log_message(self, fmt: str, *args) -> None:  # keep the console quiet
            pass

    return Handler


def create_server(settings: Settings | None = None, db: Database | None = None,
                  port: int | None = None) -> ThreadingHTTPServer:
    s = settings or load_settings()
    database = db or open_db(s.db_path)
    server = ThreadingHTTPServer((s.dashboard_host, s.dashboard_port if port is None else port),
                                 make_handler(DashboardApp(s, database)))
    server.daemon_threads = True
    return server


def serve_in_thread(server: ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def main() -> None:
    s = load_settings()
    if s.dashboard_host not in LOCAL_HOSTS and not s.dashboard_password:
        raise SystemExit("Refusing to serve the dashboard beyond this computer without DASHBOARD_PASSWORD set in .env")
    server = create_server(s)
    print(f"Dashboard running at http://{s.dashboard_host}:{s.dashboard_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
