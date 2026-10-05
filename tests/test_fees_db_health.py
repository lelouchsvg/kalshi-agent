import json

import pytest

from kalshi_agent import state
from kalshi_agent.db import now_ms
from kalshi_agent.discovery import discover, refresh_markets
from kalshi_agent.fees import executable_ev, maker_fee, taker_fee
from kalshi_agent.health import run_health
from kalshi_agent.kalshi.models import Market, Orderbook


def test_taker_fee_rounds_up_to_cent():
    assert taker_fee(1, 0.50) == 0.02          # 0.0175 -> 0.02
    assert taker_fee(100, 0.50) == 1.75
    assert taker_fee(10, 0.10) == 0.07         # 0.063 -> 0.07
    assert taker_fee(0, 0.5) == 0.0
    assert maker_fee(100, 0.5) == 0.0          # makers free by default
    with pytest.raises(ValueError):
        taker_fee(1, 55)                       # cents passed by mistake


def test_ev_yes_and_no():
    r = executable_ev(0.684, "yes", 0.61, 10)
    assert r.fees == taker_fee(10, 0.61)
    assert r.ev == pytest.approx(6.84 - 6.10 - r.fees)
    assert r.positive
    n = executable_ev(0.684, "no", 0.40, 10)
    assert n.p_win == pytest.approx(0.316)
    assert not n.positive


def test_ev_negative_when_price_equals_probability():
    # zero raw edge is always negative after fees: the system must PASS
    assert executable_ev(0.55, "yes", 0.55, 5).ev < 0


def test_ev_slippage_and_fill_probability():
    base = executable_ev(0.7, "yes", 0.6, 10)
    slip = executable_ev(0.7, "yes", 0.6, 10, slippage_per_contract=0.02)
    half = executable_ev(0.7, "yes", 0.6, 10, fill_probability=0.5)
    assert slip.ev < base.ev
    assert half.ev == pytest.approx(base.ev * 0.5)


def test_schema_has_all_spec_tables(db):
    names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    for t in ("markets market_snapshots orderbook_snapshots crypto_prices features signals predictions "
              "orders fills positions trades pnl model_versions backtest_runs experiments risk_events "
              "system_events").split():
        assert t in names


def test_prediction_probability_constraint(db):
    with pytest.raises(Exception):
        db.execute("INSERT INTO predictions (ticker, ts_ms, model_version, p_yes) VALUES ('T', 1, 'M', 1.5)")


class FakeClient:
    def __init__(self, markets):
        self.markets = markets

    def get_markets(self, series_ticker=None, status=None, tickers=None, max_pages=10, **kw):
        if tickers:
            return [m for m in self.markets if m.ticker in tickers]
        return [m for m in self.markets if m.event_ticker.startswith(series_ticker)]

    def list_series(self, category=None):
        return []

    def get_orderbook(self, ticker, depth=10):
        return Orderbook(ticker, [(0.48, 10.0)], [(0.49, 5.0)])


def _market(sym, close_in_s=600):
    now = now_ms()
    return Market(f"KX{sym}15M-X", f"KX{sym}15M-E", "active", "t", now - 300_000,
                  now + close_in_s * 1000, None, 0.48, 0.51, 0.49, 0.52, 0.5, 10, 5,
                  "greater", 100.0, None, None, None, raw={})


def test_discovery_stores_markets_and_snapshots(db, settings):
    client = FakeClient([_market("BTC"), _market("ETH")])
    found = discover(client, db, ["BTC", "ETH", "SOL"], dict(settings.series))
    assert len(found["BTC"]) == 1 and found["SOL"] == []
    assert db.query_one("SELECT COUNT(*) n FROM markets")["n"] == 2
    assert db.query_one("SELECT symbol FROM markets WHERE ticker='KXBTC15M-X'")["symbol"] == "BTC"
    refresh_markets(client, db, ["KXBTC15M-X"], 10)
    ob = db.query_one("SELECT * FROM orderbook_snapshots")
    assert ob["best_yes_ask"] == 0.51 and json.loads(ob["levels_json"])["yes"] == [[0.48, 10.0]]
    rows = state.markets(db, settings)
    assert rows[0]["signal"] == "PASS" and rows[0]["spread_ok"] is True


def test_health_blocks_trading_without_model_and_when_killed(db, settings):
    db.heartbeat("collector")
    db.insert("market_snapshots", {"ticker": "T", "ts_ms": now_ms()})
    db.insert("crypto_prices", {"symbol": "BTC", "ts_ms": now_ms(), "received_ms": now_ms(),
                                "price": 1.0, "provider": "test"})
    r = run_health(db, settings, api_probe=lambda: {"trading_active": True}, crypto_probe=lambda: 1)
    assert r.overall in ("ok", "warning") and not r.trading_allowed  # no model yet
    r2 = run_health(db, settings, killed=True)
    assert r2.overall == "critical" and not r2.trading_allowed


def test_health_flags_stale_data_and_api_failure(db, settings):
    db.insert("market_snapshots", {"ticker": "T", "ts_ms": now_ms() - 10 * 60_000})

    def boom():
        raise RuntimeError("down")
    r = run_health(db, settings, api_probe=boom)
    by = {c.name: c.status for c in r.checks}
    assert by["kalshi_api"] == "critical" and by["market_data_freshness"] == "critical"
    assert r.overall == "critical"


def test_performance_empty_says_so(db):
    p = state.performance(db, "PAPER")
    assert p["has_data"] is False and p["n_trades"] == 0 and "total_pnl" not in p


def test_performance_from_real_rows_only(db):
    for i, pnl in enumerate([1.0, -0.5, 2.0]):
        db.insert("trades", {"mode": "PAPER", "ticker": "T", "side": "yes", "count": 1, "entry_ms": i,
                             "exit_ms": now_ms(), "entry_price": 0.5, "fees": 0.02, "pnl": pnl})
    db.insert("trades", {"mode": "LIVE", "ticker": "T", "side": "yes", "count": 1, "entry_ms": 0,
                         "exit_ms": now_ms(), "entry_price": 0.5, "fees": 0, "pnl": 100.0})
    p = state.performance(db, "PAPER")
    assert p["total_pnl"] == pytest.approx(2.5)
    assert p["win_rate"] == pytest.approx(2 / 3)
    assert p["profit_factor"] == pytest.approx(3.0 / 0.5)
    assert p["max_drawdown"] == pytest.approx(0.5)


def test_trade_gates_all_closed_in_phase_1(db, settings):
    gates = state.trade_gates(db, settings)
    assert len(gates) == 8 and not all(g["ok"] for g in gates)
