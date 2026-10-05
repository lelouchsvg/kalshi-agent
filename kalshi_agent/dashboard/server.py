"""Dashboard web server: JSON API + the single-page dashboard.

Security: every route needs the dashboard password (HTTP Basic auth, username
"zeke" or anything). The server refuses to listen on a public address without a
password. On the VPS it listens on localhost behind Caddy, which adds HTTPS.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status as http
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import state
from ..config import Settings, load_settings
from ..db import Database, open_db
from ..safety import engage_kill, release_kill

STATIC = Path(__file__).with_name("static")


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    s = settings or load_settings()
    database = db or open_db(s.db_path)
    app = FastAPI(title="Kalshi Agent", docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic(auto_error=False)

    def auth(creds: HTTPBasicCredentials | None = Depends(basic)) -> str:
        if not s.dashboard_password:
            if s.dashboard_host in ("127.0.0.1", "localhost"):
                return "local"
            raise HTTPException(http.HTTP_503_SERVICE_UNAVAILABLE, "Dashboard password not configured")
        if creds is None or not secrets.compare_digest(creds.password.encode(), s.dashboard_password.encode()):
            raise HTTPException(http.HTTP_401_UNAUTHORIZED, "Login required",
                                headers={"WWW-Authenticate": 'Basic realm="kalshi-agent"'})
        return creds.username or "user"

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    def index(user: str = Depends(auth)):
        return FileResponse(STATIC / "index.html")

    @app.get("/api/overview")
    def overview(user: str = Depends(auth)):
        return {
            "status": state.status(database, s),
            "markets": state.markets(database, s),
            "gates": state.trade_gates(database, s),
            "performance": {m: state.performance(database, m) for m in ("PAPER", "DEMO", "LIVE")},
            "costs": state.costs(s),
            "data": state.data_stats(database),
            "events": state.events(database, 25),
            "crypto": {sym: state.crypto_series(database, sym, 60) for sym in s.symbols},
        }

    @app.get("/api/trades")
    def trades(user: str = Depends(auth)):
        return state.recent_trades(database)

    @app.get("/api/signals")
    def signals(user: str = Depends(auth)):
        return state.recent_signals(database)

    @app.get("/api/research")
    def research(user: str = Depends(auth)):
        return state.research(database)

    @app.get("/api/health")
    def health(user: str = Depends(auth)):
        return state.status(database, s)["health"] or {"overall": "unknown", "checks": []}

    @app.post("/api/kill")
    def kill(user: str = Depends(auth)):
        engage_kill(database, f"dashboard:{user}", "kill button pressed")
        return {"ok": True}

    @app.post("/api/unkill")
    def unkill(user: str = Depends(auth)):
        release_kill(database, f"dashboard:{user}")
        return {"ok": True, "note": "Only the dashboard kill was released. Config/file/env kills stay until removed."}

    @app.post("/api/collector/{action}")
    def collector(action: str, user: str = Depends(auth)):
        if action not in ("pause", "resume"):
            raise HTTPException(400, "action must be pause or resume")
        database.set_control("collector", "paused" if action == "pause" else "running", f"dashboard:{user}")
        database.log_event("dashboard", "info", f"Collector {action}d by {user}")
        return {"ok": True}

    @app.exception_handler(Exception)
    async def errors(request: Request, exc: Exception):
        return JSONResponse({"error": "internal error"}, status_code=500)

    return app


def main() -> None:
    import uvicorn
    s = load_settings()
    if s.dashboard_host not in ("127.0.0.1", "localhost") and not s.dashboard_password:
        raise SystemExit("Refusing to serve the dashboard publicly without DASHBOARD_PASSWORD set in .env")
    uvicorn.run(create_app(s), host=s.dashboard_host, port=s.dashboard_port, log_level="warning")


if __name__ == "__main__":
    main()
