import base64

from fastapi.testclient import TestClient

from kalshi_agent.dashboard.server import create_app
from kalshi_agent.logging_setup import redact


def _auth(pw):
    return {"Authorization": "Basic " + base64.b64encode(f"zeke:{pw}".encode()).decode()}


def test_dashboard_requires_password(settings, db):
    settings.dashboard_password = "s3cret"
    c = TestClient(create_app(settings, db))
    assert c.get("/api/overview").status_code == 401
    assert c.get("/api/overview", headers=_auth("wrong")).status_code == 401
    r = c.get("/api/overview", headers=_auth("s3cret"))
    assert r.status_code == 200
    body = r.json()
    assert body["status"]["effective_mode"] == "PAPER"
    assert body["status"]["live_trading_unlocked"] is False
    assert body["performance"]["PAPER"]["has_data"] is False
    assert body["costs"]["total_monthly_usd"] == 5.0
    assert "s3cret" not in r.text
    assert c.get("/", headers=_auth("s3cret")).status_code == 200


def test_dashboard_public_without_password_refused(settings, db):
    settings.dashboard_host = "0.0.0.0"
    c = TestClient(create_app(settings, db))
    assert c.get("/api/overview").status_code == 503


def test_dashboard_kill_and_pause(settings, db):
    c = TestClient(create_app(settings, db))  # localhost, no password
    assert c.post("/api/kill").json()["ok"]
    assert c.get("/api/overview").json()["status"]["kill_switch"]["engaged"]
    c.post("/api/unkill")
    assert not c.get("/api/overview").json()["status"]["kill_switch"]["engaged"]
    c.post("/api/collector/pause")
    assert db.get_control("collector") == "paused"
    assert c.post("/api/collector/explode").status_code == 400


def test_redaction():
    assert "abc123" not in redact("KALSHI-ACCESS-SIGNATURE: abc123")
    assert "hunter2" not in redact("password=hunter2")
    assert "MIIB" not in redact("-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----")


def test_cli_commands(settings, db, monkeypatch, capsys):
    from kalshi_agent import cli
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    for cmd in ("status", "health", "markets", "signals", "trades", "performance", "research",
                "costs", "stop", "start", "kill", "unkill", "paper", "backtest"):
        assert cli.main([cmd]) == 0, cmd
    out = capsys.readouterr().out
    assert "Phase 1" in out and "No paper trades yet" in out and "Phase 4" in out
