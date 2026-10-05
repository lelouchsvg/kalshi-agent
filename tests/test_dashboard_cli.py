import base64
import json
import urllib.error
import urllib.request

import pytest

from kalshi_agent.dashboard.server import create_server, serve_in_thread
from kalshi_agent.logging_setup import redact


@pytest.fixture
def dash(settings, db):
    servers = []

    def start():
        srv = create_server(settings, db, port=0)
        serve_in_thread(srv)
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for srv in servers:
        srv.shutdown()
        srv.server_close()


def call(url, method="GET", pw=None):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    if pw is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"zeke:{pw}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_dashboard_requires_password_when_set(settings, dash):
    settings.dashboard_password = "s3cret"
    base = dash()
    assert call(base + "/api/overview")[0] == 401
    assert call(base + "/api/overview", pw="wrong")[0] == 401
    status, text = call(base + "/api/overview", pw="s3cret")
    assert status == 200
    body = json.loads(text)
    assert body["status"]["effective_mode"] == "PAPER"
    assert body["status"]["live_trading_unlocked"] is False
    assert body["performance"]["PAPER"]["has_data"] is False
    assert body["costs"]["total_monthly_usd"] == 5.0
    assert "s3cret" not in text
    status, html = call(base + "/", pw="s3cret")
    assert status == 200 and "Kalshi Agent" in html


def test_dashboard_public_without_password_refused(settings, dash):
    settings.dashboard_host = "0.0.0.0"
    base = dash().replace("0.0.0.0", "127.0.0.1")
    assert call(base + "/api/overview")[0] == 503


def test_dashboard_local_no_password_kill_and_pause(settings, db, dash):
    base = dash()
    assert json.loads(call(base + "/api/kill", "POST")[1])["ok"]
    assert json.loads(call(base + "/api/overview")[1])["status"]["kill_switch"]["engaged"]
    call(base + "/api/unkill", "POST")
    assert not json.loads(call(base + "/api/overview")[1])["status"]["kill_switch"]["engaged"]
    call(base + "/api/collector/pause", "POST")
    assert db.get_control("collector") == "paused"
    assert call(base + "/api/collector/explode", "POST")[0] == 400
    assert call(base + "/api/nope")[0] == 404
    for path in ("/api/trades", "/api/signals", "/api/research", "/api/health"):
        assert call(base + path)[0] == 200, path


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
