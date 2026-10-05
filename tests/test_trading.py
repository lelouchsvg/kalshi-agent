import json
from pathlib import Path

import pytest

from kalshi_agent import backtest, risk, state
from kalshi_agent.config import RiskLimits
from kalshi_agent.dataset import build_examples, split_by_market
from kalshi_agent.db import now_ms
from kalshi_agent.paper import PaperTrader
from kalshi_agent.strategy import decide, size
from kalshi_agent.trainer import active_model, train_once
from synth import make_history


# --- decision rule ---------------------------------------------------------------

def test_decide_defaults_to_pass_and_buys_only_real_edges():
    kw = dict(min_edge=0.04, max_spread=0.06, slippage=0.01)
    assert decide(0.6, None, None, **kw).reason_code == "NO_QUOTE"
    assert decide(0.9, 0.40, 0.50, **kw).reason_code == "SPREAD_TOO_WIDE"
    assert decide(0.53, 0.48, 0.50, **kw).reason_code == "EDGE_TOO_SMALL"
    buy = decide(0.70, 0.48, 0.50, **kw)
    assert buy.action == "BUY" and buy.outcome == "yes" and buy.price == 0.51
    assert buy.edge == pytest.approx(0.70 - 0.51 - 0.02, abs=1e-9)     # 1-contract fee rounds up to 2¢
    no = decide(0.25, 0.48, 0.50, **kw)
    assert no.action == "BUY" and no.outcome == "no" and no.price == pytest.approx(0.53)
    assert decide(0.999, 0.96, 0.98, **kw).reason_code in ("PRICE_EXTREME", "EDGE_TOO_SMALL")
    assert size(0.51, max_order_size=5, max_market_exposure=10) == 5
    assert size(0.90, max_order_size=50, max_market_exposure=10) == 11


# --- model + backtest on synthetic history -------------------------------------------

def test_dataset_is_point_in_time(db, settings):
    make_history(db, days=1)
    ex = build_examples(db, ["BTC"])
    assert ex
    for e in ex[:200]:
        assert e.t_ms < e.close_ms
        last_closed = db.query_one("SELECT MAX(start_ms) AS s FROM crypto_candles WHERE start_ms + 60000 <= ?",
                                   (e.t_ms,))["s"]
        spot = db.query_one("SELECT close FROM crypto_candles WHERE start_ms=?", (last_closed,))["close"]
        assert e.features.spot == spot          # never a candle that closed after t
    train, test = split_by_market(ex)
    assert max(e.close_ms for e in train) + 900_000 <= min(e.close_ms for e in test)


def test_trainer_finds_edge_in_sluggish_market_and_backtests_it(db, settings):
    make_history(db, days=3, sluggish=True)
    r = train_once(db, settings, min_markets=50)
    assert r["state"] == "trained" and r["status"] == "paper", r["metrics"]["reasons"]
    m = r["metrics"]
    assert m["test"]["brier"] < m["test"]["market_brier"]
    assert m["backtest"]["label"] == "SIMULATED"
    assert m["backtest"]["n_trades"] > 10 and m["backtest"]["pnl"] > 0
    assert active_model(db)[0] == r["version"]
    assert db.query_one("SELECT COUNT(*) AS n FROM backtest_runs")["n"] == 1
    assert db.query_one("SELECT decision FROM experiments")["decision"] == "promoted"
    # a second approved model retires the first; code never writes promoted_live
    r2 = train_once(db, settings, min_markets=50)
    statuses = {row["version"]: row["status"] for row in db.query("SELECT version, status FROM model_versions")}
    assert statuses[r["version"]] == "retired" and statuses[r2["version"]] == "paper"
    assert "promoted_live" not in statuses.values()


def test_trainer_waits_for_enough_data(db, settings):
    r = train_once(db, settings)
    assert r["state"] == "waiting" and active_model(db) is None
    assert "Waiting for data" in json.loads(db.get_control("trainer"))["message"]


def test_efficient_market_gives_little_or_no_edge(db, settings):
    make_history(db, days=3, sluggish=False, seed=11)
    ex = build_examples(db, ["BTC"])
    train, test = split_by_market(ex)
    from kalshi_agent.model import LogisticModel
    model = LogisticModel().fit([e.features.vector() for e in train], [e.label_above for e in train])
    bt = backtest.run(model, test, settings.risk)
    assert bt.n_trades <= max(3, 0.05 * bt.n_markets)       # no fake edge where there is none


# --- paper engine ----------------------------------------------------------------

def _live_setup(db, settings, *, spot=60300.0, strike=60000.0, bid=0.48, ask=0.50, depth=3):
    now = now_ms()
    db.upsert("markets", {"ticker": "KXBTC15M-LIVE", "series_ticker": "KXBTC15M", "symbol": "BTC", "title": "t",
                          "status": "active", "open_ms": now - 600_000, "close_ms": now + 300_000,
                          "strike_type": "greater_or_equal", "floor_strike": strike, "first_seen_ms": now,
                          "updated_ms": now}, "ticker")
    db.insert("orderbook_snapshots", {"ticker": "KXBTC15M-LIVE", "ts_ms": now - 2000, "best_yes_bid": bid,
                                      "best_yes_ask": ask, "yes_depth": 10, "no_depth": depth,
                                      "levels_json": json.dumps({"yes": [[bid, 10]], "no": [[round(1 - ask, 2), depth]]})})
    rows = []
    for i in range(40):            # 40 minutes of gently wiggling prices from the stream
        ts = now - (40 - i) * 60_000 + 30_000
        rows.append({"symbol": "BTC", "ts_ms": ts, "received_ms": ts + 50, "price": 60000 + (i % 3) * 20,
                     "provider": "coinbase_ws"})
    rows.append({"symbol": "BTC", "ts_ms": now - 1000, "received_ms": now - 900, "price": spot, "provider": "coinbase_ws"})
    db.insert_many("crypto_prices", rows)
    db.set_control("mode_state", json.dumps({"effective": "PAPER"}), "test")


def _approve(db, weights):
    db.insert("model_versions", {"version": "MODEL_V001", "created_ms": now_ms(), "status": "paper",
                                 "hyperparams_json": json.dumps({"weights": weights,
                                  "features": ["z", "market_logit", "momentum_5m", "time_left"], "l2": 1})})


def test_paper_trader_buys_fills_settles_and_never_doubles_up(db, settings):
    _live_setup(db, settings, depth=3)
    _approve(db, [0.0, 1.0, 0.0, 0.0, 0.0])            # trusts z: spot well above strike -> high P(yes)
    pt = PaperTrader(db, settings)
    assert pt.step()["buys"] == 1
    t = db.query_one("SELECT * FROM trades")
    assert t["mode"] == "PAPER" and t["side"] == "yes" and t["count"] == 3     # capped by resting depth
    assert t["entry_price"] == 0.51 and t["fees"] > 0 and t["pnl"] is None
    assert db.query_one("SELECT status FROM orders")["status"] == "filled"
    pt.step()
    assert db.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 1
    last = db.query_one("SELECT reason_code FROM signals ORDER BY id DESC LIMIT 1")["reason_code"]
    assert last == "RISK_ALREADY_IN_MARKET"
    db.execute("UPDATE markets SET result='yes', status='settled' WHERE ticker='KXBTC15M-LIVE'")
    assert pt.settle() == 1
    t = db.query_one("SELECT * FROM trades")
    assert t["result"] == "win" and t["pnl"] == pytest.approx(3 * (1 - 0.51) - t["fees"])
    perf = state.performance(db, "PAPER")
    assert perf["has_data"] and perf["n_trades"] == 1
    assert db.query("SELECT * FROM pnl WHERE mode='PAPER'")


def test_paper_trader_passes_without_model_when_killed_and_on_stale_data(db, settings):
    _live_setup(db, settings)
    pt = PaperTrader(db, settings)
    pt.step()
    assert db.query_one("SELECT reason_code FROM signals")["reason_code"] == "NO_MODEL"
    _approve(db, [0.0, 1.0, 0.0, 0.0, 0.0])
    db.set_control("kill_switch", "on", "test")
    pt.step()
    assert db.query_one("SELECT reason_code FROM signals ORDER BY id DESC LIMIT 1")["reason_code"] == "KILL_SWITCH"
    db.set_control("kill_switch", "off", "test")
    db.execute("UPDATE orderbook_snapshots SET ts_ms = ts_ms - 60000")
    pt.step()
    assert db.query_one("SELECT reason_code FROM signals ORDER BY id DESC LIMIT 1")["reason_code"] == "STALE_DATA"
    db.set_control("mode_state", json.dumps({"effective": "WATCH"}), "test")
    pt.step()
    assert db.query_one("SELECT reason_code FROM signals ORDER BY id DESC LIMIT 1")["reason_code"] == "NOT_PAPER_MODE"
    assert db.query_one("SELECT COUNT(*) AS n FROM trades")["n"] == 0


def test_risk_limits_block_trades():
    lim = RiskLimits()
    st = risk.AccountState("PAPER", 1000.0, peak_equity=1000.0)
    ok = risk.check(st, lim, ticker="T", price=0.5, contracts=5, seconds_to_close=300, data_age_s=1)
    assert ok.ok
    def code(**over):
        s2 = risk.AccountState("PAPER", 1000.0, peak_equity=1000.0, **over.pop("st", {}))
        args = dict(ticker="T", price=0.5, contracts=5, seconds_to_close=300, data_age_s=1) | over
        return risk.check(s2, lim, **args).code
    assert code(contracts=6) == "ORDER_TOO_BIG"
    assert code(seconds_to_close=30) == "TOO_CLOSE_TO_EXPIRY"
    assert code(data_age_s=60) == "STALE_DATA"
    assert code(st={"realized_today": -25.0}) == "DAILY_LOSS_LIMIT"
    assert code(st={"realized": -60.0}) == "MAX_DRAWDOWN"
    assert code(st={"consecutive_losses": 5}) == "LOSING_STREAK"
    assert code(st={"open_tickers": {"A": 1, "B": 1, "C": 1}}) == "MAX_OPEN_POSITIONS"
    assert code(st={"open_tickers": {"T": 1}}) == "ALREADY_IN_MARKET"


def test_live_readiness_reports_honestly(db, settings):
    rd = state.live_readiness(db, settings)
    assert not rd["all_met"] and rd["live_locked"]
    assert all(not c["ok"] for c in rd["criteria"])
    now = now_ms()
    for i in range(3):
        db.insert("trades", {"mode": "PAPER", "ticker": f"T{i}", "side": "yes", "count": 1, "entry_ms": now - 1000,
                             "exit_ms": now, "entry_price": 0.5, "exit_price": 1.0, "fees": 0.02, "pnl": 0.48})
    rd = state.live_readiness(db, settings)
    crit = {c["name"]: c for c in rd["criteria"]}
    assert crit["Profitable after fees"]["ok"]
    assert not crit["At least 200 settled paper trades"]["ok"]
    assert not rd["all_met"]


def test_trading_code_cannot_send_real_orders():
    root = Path(__file__).resolve().parent.parent / "kalshi_agent"
    for name in ("paper.py", "strategy.py", "backtest.py", "trainer.py", "risk.py", "model.py", "dataset.py"):
        text = (root / name).read_text()
        for banned in ("create_order", "authorize_order", "KalshiClient", "build_client"):
            assert banned not in text, (name, banned)
