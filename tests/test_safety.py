import pytest

from kalshi_agent import safety
from kalshi_agent.config import load_settings
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.modes import TradingMode
from kalshi_agent.safety import (OrderPermit, authorize_order, engage_kill, evaluate_mode,
                                 kill_status, release_kill, verify_permit)

PROD = "https://api.elections.kalshi.com/trade-api/v2"
DEMO = "https://demo-api.kalshi.co/trade-api/v2"


def test_default_mode_is_paper(tmp_path):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("{}")
    assert load_settings(cfg, env={}).trading_mode is TradingMode.PAPER


def test_unknown_mode_fails_safe_to_watch():
    assert TradingMode.parse("YOLO") is TradingMode.WATCH
    assert TradingMode.parse(None) is TradingMode.PAPER


def test_live_impossible_even_with_every_other_gate(db, monkeypatch):
    db.execute("INSERT INTO model_versions (version, created_ms, status) VALUES ('MODEL_V001', 0, 'promoted_live')")
    gate = evaluate_mode(TradingMode.LIVE, config_mode=TradingMode.LIVE, kalshi_env="prod",
                         has_credentials=True, db=db, killed=False, health_ok=True,
                         env={"KALSHI_LIVE_CONFIRM": safety.LIVE_CONFIRM_PHRASE})
    assert safety.LIVE_TRADING_UNLOCKED is False
    assert not gate.allowed
    assert gate.effective_mode is TradingMode.WATCH
    assert gate.reasons == ["G1_code_unlocked"]
    with pytest.raises(PermissionError):
        authorize_order(gate, PROD)


def test_env_var_alone_cannot_enable_live(tmp_path, db, monkeypatch):
    cfg = tmp_path / "s.yaml"
    cfg.write_text("trading_mode: PAPER\n")
    s = load_settings(cfg, env={"TRADING_MODE": "LIVE"})
    monkeypatch.setattr(safety, "LIVE_TRADING_UNLOCKED", True)  # even if code were unlocked
    gate = evaluate_mode(s.trading_mode, config_mode=TradingMode.PAPER, kalshi_env="prod",
                         has_credentials=True, db=db, killed=False, health_ok=True, env={})
    assert not gate.allowed
    assert "G2_config_file_says_live" in gate.reasons
    assert "G3_confirm_phrase" in gate.reasons
    assert "G4_validated_model" in gate.reasons


def test_kill_switch_blocks_live_gate(db, monkeypatch):
    monkeypatch.setattr(safety, "LIVE_TRADING_UNLOCKED", True)
    db.execute("INSERT INTO model_versions (version, created_ms, status) VALUES ('M', 0, 'promoted_live')")
    gate = evaluate_mode(TradingMode.LIVE, config_mode=TradingMode.LIVE, kalshi_env="prod",
                         has_credentials=True, db=db, killed=True, health_ok=True,
                         env={"KALSHI_LIVE_CONFIRM": safety.LIVE_CONFIRM_PHRASE})
    assert gate.reasons == ["G6_not_killed_and_healthy"]


def test_demo_only_on_demo_exchange_with_credentials(db):
    ok = evaluate_mode(TradingMode.DEMO, config_mode=TradingMode.DEMO, kalshi_env="demo",
                       has_credentials=True, db=db, killed=False, health_ok=True, env={})
    assert ok.allowed
    with pytest.raises(PermissionError):
        authorize_order(ok, PROD)          # a demo permit can never point at the real exchange
    for kw in ({"kalshi_env": "prod"}, {"has_credentials": False}, {"killed": True}, {"health_ok": False}):
        args = {"kalshi_env": "demo", "has_credentials": True, "killed": False, "health_ok": True, **kw}
        assert not evaluate_mode(TradingMode.DEMO, config_mode=TradingMode.DEMO, db=db, env={}, **args).allowed


def test_live_still_locked_in_code():
    from kalshi_agent import safety
    assert safety.LIVE_TRADING_UNLOCKED is False


def test_paper_and_watch_never_get_order_permits():
    for mode in (TradingMode.PAPER, TradingMode.WATCH, TradingMode.BACKTEST):
        gate = evaluate_mode(mode, config_mode=mode, kalshi_env="prod", has_credentials=True,
                             db=None, killed=False, health_ok=True, env={})
        assert gate.allowed
        with pytest.raises(PermissionError):
            authorize_order(gate, PROD)


def test_forged_permit_rejected():
    forged = OrderPermit(mode=TradingMode.LIVE, base_url=PROD, seal="guess")
    with pytest.raises(PermissionError):
        verify_permit(forged, PROD)
    with pytest.raises(PermissionError):
        verify_permit({"mode": "LIVE"}, PROD)


def test_client_refuses_order_without_permit():
    c = KalshiClient(PROD)
    with pytest.raises(PermissionError):
        c.create_order(None, ticker="X", outcome="yes", count=1, limit_price=0.5, client_order_id="a")
    with pytest.raises(PermissionError):
        c.cancel_order(None, "abc")


def test_kill_switch_sources(db, settings):
    assert not kill_status(db, False, settings.kill_file, env={}).killed
    assert kill_status(db, True, settings.kill_file, env={}).sources == ["config"]
    assert kill_status(db, False, settings.kill_file, env={"KILL_SWITCH": "1"}).sources == ["env"]
    settings.kill_file.write_text("x")
    assert "file" in kill_status(db, False, settings.kill_file, env={}).sources
    settings.kill_file.unlink()
    engage_kill(db, "test", "because")
    ks = kill_status(db, False, settings.kill_file, env={})
    assert ks.killed and ks.sources == ["dashboard"]
    assert db.query_one("SELECT code FROM risk_events ORDER BY id DESC")["code"] == "KILL_SWITCH_ENGAGED"
    release_kill(db, "test")
    assert not kill_status(db, False, settings.kill_file, env={}).killed


def test_release_does_not_clear_config_kill(db, settings):
    engage_kill(db, "t", "r")
    release_kill(db, "t")
    assert kill_status(db, True, settings.kill_file, env={}).killed
