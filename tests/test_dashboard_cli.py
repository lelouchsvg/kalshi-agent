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
                "costs", "feeds", "data", "stop", "start", "kill", "unkill", "paper", "model", "backtest"):
        assert cli.main([cmd]) == 0, cmd
    out = capsys.readouterr().out
    assert "Phase 5" in out and "No paper trades yet" in out and "Waiting for data" in out


def test_dashboard_phase2_panels(settings, dash):
    base = dash()
    body = json.loads(call(base + "/api/overview")[1])
    assert {f["key"] for f in body["feeds"]} >= {"feed:coinbase_ws", "feed:index_proxy", "feed:kalshi_ws"}
    status, text = call(base + "/api/quality")
    q = json.loads(text)
    assert status == 200 and q["settled"]["total"] == 0 and q["proxy_check"]["n"] == 0


def _pem():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption()).decode()


def test_save_credentials_writes_private_files(tmp_path, monkeypatch):
    import stat
    from kalshi_agent import credentials
    pem = _pem()
    (tmp_path / ".env").write_text("DASHBOARD_PASSWORD=x\nKALSHI_API_KEY_ID=old\n")
    with pytest.raises(credentials.CredentialError):
        credentials.save(tmp_path, "bad id!", pem)
    with pytest.raises(credentials.CredentialError):
        credentials.save(tmp_path, "abcd1234-ef56", "not a key")
    with pytest.raises(credentials.CredentialError):
        credentials.save(tmp_path, "abcd1234-ef56", "-----BEGIN RSA PRIVATE KEY-----\ngarbage\n-----END RSA PRIVATE KEY-----")
    assert not (tmp_path / "secrets" / "kalshi.key").exists()

    info = credentials.save(tmp_path, " abcd1234-ef56 ", "Private key:\r\n" + pem.replace("\n", "\r\n"))
    assert info == {"key_type": "RSA", "key_id_hint": "…ef56"}
    key = tmp_path / "secrets" / "kalshi.key"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert key.read_text().startswith("-----BEGIN RSA PRIVATE KEY-----")
    env = credentials.read_env(tmp_path)
    assert env == {"DASHBOARD_PASSWORD": "x", "KALSHI_API_KEY_ID": "abcd1234-ef56",
                   "KALSHI_PRIVATE_KEY_PATH": str(key)}
    st = credentials.status(tmp_path)
    assert st == {"configured": True, "key_id_hint": "…ef56", "problem": None}
    assert "BEGIN" not in json.dumps(st)
    key.chmod(0o644)
    assert "other users" in credentials.status(tmp_path)["problem"]


def test_verify_maps_kalshi_answers(tmp_path):
    from kalshi_agent import credentials
    from kalshi_agent.kalshi.client import KalshiAPIError
    credentials.save(tmp_path, "abcd1234-ef56", _pem())
    path = credentials.key_path(tmp_path)

    def client(exc=None):
        class C:
            def __init__(self, signer):
                assert signer.key_id == "abcd1234-ef56"

            def get_balance(self):
                if exc:
                    raise exc
                return {"balance": 0}
        return C
    assert credentials.verify("u", "abcd1234-ef56", path, client())[0] is True
    assert credentials.verify("u", "abcd1234-ef56", path, client(KalshiAPIError(401, "no", "/p")))[0] is False
    assert credentials.verify("u", "abcd1234-ef56", path, client(OSError("down")))[0] is None


def test_dashboard_credentials_endpoint(settings, db, tmp_path, monkeypatch):
    from kalshi_agent import credentials
    from kalshi_agent.dashboard.server import DashboardApp, HTTPError
    monkeypatch.setattr(credentials, "verify", lambda *a, **k: (True, "Kalshi accepted the key."))
    app = DashboardApp(settings, db, root=tmp_path)
    status, _, body = app.handle("GET", "/api/credentials", None)
    assert json.loads(body)["configured"] is False
    pem = _pem()
    payload = json.dumps({"key_id": "abcd1234-ef56", "private_key": pem}).encode()
    for bad in ({"Host": "evil.example:8080"}, {"Host": "127.0.0.1:8080", "Origin": "https://evil.example"}):
        with pytest.raises(HTTPError) as e:
            app.handle("POST", "/api/credentials", None, payload, bad)
        assert e.value.status == 403
    status, _, body = app.handle("POST", "/api/credentials", None, payload,
                                 {"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"})
    out = json.loads(body)
    assert out["ok"] and out["verified"] is True and "BEGIN" not in body.decode()
    assert db.get_control("collector_restart") == "requested"
    assert settings.has_kalshi_credentials
    events = json.dumps(db.query("SELECT message FROM system_events"))
    assert "BEGIN" not in events and "abcd1234-ef56" not in events
    bad = json.loads(app.handle("POST", "/api/credentials", None, b'{"key_id":"x","private_key":""}', {})[2])
    assert bad["ok"] is False and "Key ID" in bad["error"]
