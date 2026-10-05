import json
from pathlib import Path

from kalshi_agent import service
from kalshi_agent.clock import ClockReading
from kalshi_agent.crypto.provider import PriceTick
from kalshi_agent.db import now_ms
from kalshi_agent.kalshi.models import Market, Orderbook


def _market(close_in_s=600):
    now = now_ms()
    return Market("KXBTC15M-A", "E", "active", "t", now - 300_000, now + close_in_s * 1000, None,
                  0.48, 0.51, 0.49, 0.52, 0.5, 10, 5, "greater", 100.0, None, None, None, raw={})


class FakeClient:
    def __init__(self):
        self.trade_calls = []

    def get_markets(self, **kw):
        return [_market()] if kw.get("series_ticker") in (None, "KXBTC15M") else []

    def list_series(self, **kw):
        return []

    def get_orderbook(self, ticker, depth=10):
        return Orderbook(ticker, [(0.48, 10.0)], [(0.49, 5.0)])

    def get_trades(self, ticker, min_ts=None, max_pages=3):
        self.trade_calls.append((ticker, min_ts))
        return [{"trade_id": f"x{len(self.trade_calls)}", "ticker": ticker,
                 "created_time": "2026-10-05T12:00:00Z", "yes_price_dollars": "0.5", "count_fp": "1"}]

    def get_exchange_status(self):
        return {"trading_active": True}


class FakeProvider:
    def latest(self, sym):
        return PriceTick(sym, 100.0, now_ms(), now_ms(), "coinbase")


def test_collector_step_collects_everything_without_trading(db, settings, monkeypatch):
    settings.symbols = ["BTC"]
    monkeypatch.setattr(service, "measure", lambda **kw: ClockReading(42.0, 10.0, "coinbase", now_ms()))
    c = service.Collector(settings, db, client=FakeClient(), provider=FakeProvider())
    c.step()
    assert c.tradeable == ["KXBTC15M-A"]
    assert db.query_one("SELECT COUNT(*) AS n FROM kalshi_trades")["n"] == 1
    assert db.query_one("SELECT COUNT(*) AS n FROM crypto_prices")["n"] == 1      # REST fallback (no stream)
    assert json.loads(db.get_control("clock"))["offset_ms"] == 42.0
    assert json.loads(db.get_control("last_health"))["overall"] in ("ok", "warning", "critical")
    assert db.query_one("SELECT COUNT(*) AS n FROM orders")["n"] == 0
    assert c._kalshi_stream().startswith("needs a Kalshi API key")


def test_maintenance_trims_old_ladders_only(db, settings):
    old, new = now_ms() - 30 * 86_400_000, now_ms()
    for ts in (old, new):
        db.insert("orderbook_snapshots", {"ticker": "T", "ts_ms": ts, "best_yes_bid": 0.4, "levels_json": "{}"})
    c = service.Collector(settings, db, client=FakeClient(), provider=FakeProvider())
    c.maintenance()
    rows = db.query("SELECT ts_ms, levels_json, best_yes_bid FROM orderbook_snapshots ORDER BY ts_ms")
    assert rows[0]["levels_json"] is None and rows[0]["best_yes_bid"] == 0.4
    assert rows[1]["levels_json"] == "{}"


def test_data_modules_cannot_place_orders():
    root = Path(__file__).resolve().parent.parent / "kalshi_agent"
    for rel in ("stream", "backfill.py", "crypto", "clock.py", "timeseries.py", "service.py", "discovery.py"):
        for f in ([root / rel] if rel.endswith(".py") else (root / rel).rglob("*.py")):
            text = f.read_text()
            assert "create_order" not in text and "authorize_order" not in text, f
