import json
import sqlite3
import threading

from kalshi_agent import state
from kalshi_agent.backfill import Backfill, parse_candles
from kalshi_agent.clock import ClockSync, measure
from kalshi_agent.crypto.index import IndexProxy, Quote, combine
from kalshi_agent.crypto.provider import Candle
from kalshi_agent.db import now_ms, open_db
from kalshi_agent.discovery import record_trades
from kalshi_agent.health import run_health
from kalshi_agent.kalshi.client import KalshiAPIError
from kalshi_agent.kalshi.models import Market
from kalshi_agent.timeseries import asof, crypto_candles_asof, market_candles_asof, settlement_proxy


# --- migrations: updating must keep collected data -------------------------------

def test_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    c.executescript("""CREATE TABLE markets (ticker TEXT PRIMARY KEY, event_ticker TEXT, series_ticker TEXT,
        symbol TEXT, title TEXT, status TEXT, open_ms INTEGER, close_ms INTEGER, expiration_ms INTEGER,
        strike_type TEXT, floor_strike REAL, cap_strike REAL, result TEXT, rules_primary TEXT,
        first_seen_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL, raw_json TEXT);
        CREATE TABLE schema_version (version INTEGER NOT NULL, applied_ms INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1, 0);
        INSERT INTO markets (ticker, first_seen_ms, updated_ms) VALUES ('OLD', 1, 1);""")
    c.commit()
    c.close()
    db = open_db(path)
    assert db.query_one("SELECT ticker, expiration_value FROM markets") == {"ticker": "OLD", "expiration_value": None}
    assert db.query_one("SELECT MAX(version) AS v FROM schema_version")["v"] == 2
    db.close()
    open_db(path).close()      # second open is a no-op


# --- index proxy -------------------------------------------------------------------

def test_combine_drops_broken_exchange():
    qs = [Quote("a", 100.0, 0), Quote("b", 100.2, 0), Quote("c", 99.9, 0), Quote("d", 150.0, 0)]
    value, kept = combine(qs)
    assert value == 100.0 and {q.source for q in kept} == {"a", "b", "c"}
    assert combine([]) == (None, [])


class FakeQuotes:
    def __init__(self, fail=()):
        self.fail = fail

    def _q(self, src, syms, px):
        if src in self.fail:
            raise RuntimeError("down")
        return {s: Quote(src, px[s], now_ms()) for s in syms}

    def coinbase(self, syms): return self._q("coinbase", syms, {"BTC": 100.0})
    def kraken(self, syms): return self._q("kraken", syms, {"BTC": 101.0})
    def bitstamp(self, syms): return self._q("bitstamp", syms, {"BTC": 102.0})
    def gemini(self, syms): return self._q("gemini", syms, {"BTC": 103.0})


def test_index_proxy_writes_median_and_rolling_average(db):
    ip = IndexProxy(db, threading.Event(), ["BTC"], quotes=FakeQuotes(fail=("gemini",)))
    assert ip.step() == 1
    r = db.query_one("SELECT * FROM index_ticks")
    assert r["value"] == 101.0 and r["n_sources"] == 3 and r["avg60"] == 101.0
    assert "gemini" in ip.source_errors
    ip.quotes = FakeQuotes()
    ip.step()
    rows = db.query("SELECT value, avg60 FROM index_ticks ORDER BY id")
    assert rows[1]["value"] == 101.5 and abs(rows[1]["avg60"] - 101.25) < 1e-9
    hb = json.loads(db.query_one("SELECT info_json FROM heartbeats WHERE component='feed:index_proxy'")["info_json"])
    assert hb["connected"] and hb["source_errors"] == {}


def test_index_proxy_prefers_live_stream_over_rest(db):
    class Tick:
        price, bid, ask, received_ms = 101.0, 100.0, 102.0, now_ms()
    q = FakeQuotes(fail=("coinbase",))       # REST would fail; the stream supplies Coinbase
    ip = IndexProxy(db, threading.Event(), ["BTC"], quotes=q, coinbase_live=lambda s: Tick())
    ip.step()
    srcs = json.loads(db.query_one("SELECT sources_json FROM index_ticks")["sources_json"])
    assert srcs["coinbase"] == 101.0 and "coinbase" not in ip.source_errors


# --- clock ---------------------------------------------------------------------------

def test_clock_picks_lowest_latency_sample_and_health_flags_drift(db, settings):
    samples = iter([(900.0, 300.0), (120.0, 20.0), (500.0, 90.0)])
    reading = measure(samples=3, sampler=lambda: next(samples))
    assert reading.offset_ms == 120.0 and reading.rtt_ms == 20.0
    cs = ClockSync(db)
    cs.update(reading)
    assert ClockSync(db).current() == 120.0          # survives restart
    chk = {c.name: c for c in run_health(db, settings).checks}
    assert chk["clock"].status == "ok"
    cs.update(measure(samples=1, sampler=lambda: (3000.0, 10.0)))
    assert {c.name: c for c in run_health(db, settings).checks}["clock"].status == "warning"
    cs.update(measure(samples=1, sampler=lambda: (-9000.0, 10.0)))
    assert {c.name: c for c in run_health(db, settings).checks}["clock"].status == "critical"


# --- look-ahead protection ------------------------------------------------------------

def test_asof_uses_arrival_time_not_source_time(db):
    t = 1_000_000
    # source says 900k but it only reached us at 1.2M: unknown at t
    db.insert("crypto_prices", {"symbol": "BTC", "ts_ms": 900_000, "received_ms": 1_200_000, "price": 2.0,
                                "provider": "x"})
    db.insert("crypto_prices", {"symbol": "BTC", "ts_ms": 950_000, "received_ms": 990_000, "price": 1.0,
                                "provider": "x"})
    assert asof(db, "crypto_prices", "BTC", t)["price"] == 1.0
    assert asof(db, "crypto_prices", "BTC", t, max_age_ms=5_000) is None
    db.insert("crypto_candles", {"symbol": "BTC", "provider": "coinbase", "start_ms": 900_000, "open": 1,
                                 "high": 1, "low": 1, "close": 1, "volume": 1})
    db.insert("crypto_candles", {"symbol": "BTC", "provider": "coinbase", "start_ms": 960_000, "open": 1,
                                 "high": 1, "low": 1, "close": 2, "volume": 1})
    got = crypto_candles_asof(db, "BTC", t, 5)
    assert [c["start_ms"] for c in got] == [900_000]        # the 960k candle closes at 1.02M
    db.insert("market_candles", {"ticker": "T", "end_ms": 1_000_000, "price_close": 0.5})
    db.insert("market_candles", {"ticker": "T", "end_ms": 1_060_000, "price_close": 0.6})
    assert len(market_candles_asof(db, "T", t)) == 1


def test_settlement_proxy_averages_last_minute(db):
    close = 10_000_000
    for i, v in enumerate([100.0, 102.0, 104.0]):
        db.insert("index_ticks", {"symbol": "BTC", "received_ms": close - 50_000 + i * 20_000, "value": v,
                                  "n_sources": 3})
    db.insert("index_ticks", {"symbol": "BTC", "received_ms": close + 5_000, "value": 999.0, "n_sources": 3})
    db.insert("index_ticks", {"symbol": "BTC", "received_ms": close - 70_000, "value": 1.0, "n_sources": 3})
    assert settlement_proxy(db, "BTC", close) == 102.0


# --- trades ---------------------------------------------------------------------------

def test_record_trades_parses_and_dedupes(db):
    class C:
        def get_trades(self, ticker, min_ts=None, max_pages=3):
            self.min_ts = min_ts
            return [{"trade_id": "t1", "ticker": ticker, "created_time": "2026-10-05T12:00:00.5Z",
                     "yes_price_dollars": "0.6100", "count_fp": "3.00", "taker_side": "yes"},
                    {"trade_id": "t2", "ticker": ticker, "created_time": "2026-10-05T12:00:01Z",
                     "yes_price": 62, "count": 1, "taker_side": "no"},
                    {"trade_id": None}]
    c = C()
    assert record_trades(c, db, "T", since_ms=1_700_000_000_123) == 2
    assert c.min_ts == 1_700_000_000
    record_trades(c, db, "T")
    rows = db.query("SELECT * FROM kalshi_trades ORDER BY ts_ms")
    assert len(rows) == 2 and rows[0]["yes_price"] == 0.61 and rows[1]["yes_price"] == 0.62


# --- backfill -------------------------------------------------------------------------

def _settled(i, result, value, strike=100.0):
    close = now_ms() - (i + 1) * 900_000
    return Market(f"KXBTC15M-{i}", "E", "settled", "t", close - 900_000, close, close, None, None, None, None,
                  None, None, None, "greater_or_equal", strike, None, result, "rules", expiration_value=value, raw={})


class BackfillClient:
    def __init__(self):
        self.calls = 0

    def get_markets(self, **kw):
        assert kw["status"] == "settled" and kw["series_ticker"] == "KXBTC15M"
        return [_settled(0, "yes", 101.0), _settled(1, "no", 99.0), _settled(2, "no", 99.5)]

    def get_candlesticks(self, series, ticker, start, end, period):
        self.calls += 1
        if ticker.endswith("-2"):
            raise KalshiAPIError(404, "moved", "/x")
        return [{"end_period_ts": start + 60, "yes_bid": {"close_dollars": "0.40"},
                 "yes_ask": {"close_dollars": "0.45"}, "price": {"close_dollars": "0.42"},
                 "volume_fp": "10", "open_interest_fp": "5"}]

    def get_historical_candlesticks(self, ticker, start, end, period):
        return [{"end_period_ts": start + 60, "yes_bid": {"close": 40}, "yes_ask": {"close": 46},
                 "price": {"close": 43}, "volume": 2, "open_interest": 1}]


class FakeCandles:
    name = "coinbase"

    def candles(self, symbol, start_ms, end_ms, granularity_s=60):
        out, t = [], start_ms - start_ms % 60_000
        while t <= end_ms and len(out) < 300:
            out.append(Candle(symbol, t, 1, 2, 0.5, 1.5, 10))
            t += 60_000
        return out


def test_backfill_downloads_settled_markets_and_candles(db):
    client = BackfillClient()
    bf = Backfill(db, client, threading.Event(), ["BTC"], {"BTC": "KXBTC15M"}, days=1,
                  crypto=FakeCandles(), pause_s=0)
    bf.run_cycle()
    assert db.query_one("SELECT COUNT(*) AS n FROM markets WHERE result IN ('yes','no')")["n"] == 3
    candles = db.query("SELECT * FROM market_candles ORDER BY ticker")
    assert len(candles) == 3
    assert candles[2]["yes_ask_close"] == 0.46        # legacy cents via the historical tier
    assert candles[0]["price_close"] == 0.42
    n_crypto = db.query_one("SELECT COUNT(*) AS n FROM crypto_candles")["n"]
    assert 1430 <= n_crypto <= 1441                    # one day of completed minutes
    calls = client.calls
    bf.run_cycle()                                      # second cycle: nothing re-downloaded
    assert client.calls == calls
    assert db.query_one("SELECT COUNT(*) AS n FROM crypto_candles")["n"] - n_crypto <= 2
    prog = json.loads(db.get_control("backfill"))
    assert prog["phase"] == "up to date" and prog["cycles"] == 2


def test_parse_candles_skips_rows_without_time():
    assert parse_candles("T", [{"price": {"close_dollars": "0.1"}}]) == []


# --- data quality views ----------------------------------------------------------------

def test_data_quality_rule_and_proxy_checks(db, settings):
    bf = Backfill(db, BackfillClient(), threading.Event(), ["BTC"], {"BTC": "KXBTC15M"}, days=1,
                  crypto=FakeCandles(), pause_s=0)
    bf.settled_markets("BTC")
    m = db.query_one("SELECT close_ms FROM markets WHERE ticker='KXBTC15M-0'")
    db.insert("index_ticks", {"symbol": "BTC", "received_ms": m["close_ms"] - 10_000, "value": 101.1, "n_sources": 4})
    q = state.data_quality(db, settings)
    assert q["settled"]["total"] == 3 and q["settled"]["by_symbol"]["BTC"] == {"yes": 1, "no": 2}
    assert q["rule_check"] == {"checked": 3, "agree": 3}
    assert q["proxy_check"]["n"] == 1 and q["proxy_check"]["agree"] == 1
    assert abs(q["proxy_check"]["mean_abs_pct"] - 0.099) < 0.01
    assert state.outcome_from_value("less", None, 50.0, 49.0) == "yes"
    assert state.outcome_from_value("between", 1.0, 2.0, 3.0) == "no"
    assert state.outcome_from_value("weird", 1.0, 2.0, 3.0) is None
    feeds = {f["key"]: f for f in state.feeds(db, settings)}
    assert feeds["feed:kalshi_ws"]["state"] == "off"
