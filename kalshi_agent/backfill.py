"""History backfill: settled 15-minute markets and one-minute price history.

Instead of waiting weeks for the live collector to see enough markets, this pulls
recent history straight from Kalshi (settled markets, their results and one-minute
candles) and from Coinbase (one-minute BTC/ETH/SOL candles). It runs slowly in the
background with its own small request budget so live collection is never starved.

All of it is public, read-only data. Nothing here can trade.
"""
from __future__ import annotations

import json
import logging
import threading

from .crypto.provider import CoinbaseProvider
from .db import Database, now_ms
from .discovery import upsert_market
from .kalshi.client import KalshiAPIError, KalshiClient
from .kalshi.models import to_float

log = logging.getLogger("backfill")

DAY_MS = 86_400_000


def _ohlc_close(c: dict, key: str) -> float | None:
    """Candle sub-objects carry `close_dollars` (fixed point) or legacy `close` in cents."""
    d = c.get(key) or {}
    v = to_float(d.get("close_dollars"))
    if v is not None:
        return v
    cents = to_float(d.get("close"))
    return cents / 100 if cents is not None else None


def parse_candles(ticker: str, candles: list[dict]) -> list[dict]:
    rows = []
    for c in candles:
        end = c.get("end_period_ts")
        if end is None:
            continue
        rows.append({
            "ticker": ticker, "end_ms": int(end) * 1000,
            "yes_bid_close": _ohlc_close(c, "yes_bid"), "yes_ask_close": _ohlc_close(c, "yes_ask"),
            "price_close": _ohlc_close(c, "price"),
            "volume": to_float(c.get("volume_fp", c.get("volume"))),
            "open_interest": to_float(c.get("open_interest_fp", c.get("open_interest")))})
    return rows


class Backfill(threading.Thread):
    def __init__(self, db: Database, client: KalshiClient, stop_event: threading.Event,
                 symbols: list[str], series_map: dict[str, str], days: int = 14,
                 crypto: CoinbaseProvider | None = None, pause_s: float = 0.3,
                 paused_fn=lambda: False):
        super().__init__(daemon=True, name="backfill")
        self.db = db
        self.client = client
        self.stop_event = stop_event
        self.symbols = symbols
        self.series_map = series_map
        self.days = days
        self.crypto = crypto or CoinbaseProvider()
        self.pause_s = pause_s
        self.paused_fn = paused_fn
        self.progress: dict = {"phase": "waiting", "markets_found": 0, "markets_done": 0,
                               "crypto_minutes": 0, "errors": 0, "days": days}

    def _save(self, **kw) -> None:
        self.progress.update(kw, updated_ms=now_ms())
        self.db.set_control("backfill", json.dumps(self.progress), "backfill")

    # -- settled markets ---------------------------------------------------------
    def settled_markets(self, symbol: str) -> int:
        series = self.series_map.get(symbol)
        if not series:
            return 0
        since = (now_ms() - self.days * DAY_MS) // 1000
        markets = []
        try:
            markets = self.client.get_markets(series_ticker=series, status="settled",
                                              min_close_ts=since, limit=1000, max_pages=10)
        except KalshiAPIError as exc:
            log.warning("Settled markets for %s failed: %s", series, exc)
            self.progress["errors"] += 1
        for m in markets:
            upsert_market(self.db, m, symbol, series)
        return len(markets)

    def market_candles(self, limit: int = 2000) -> int:
        todo = self.db.query("""
            SELECT m.ticker, m.series_ticker, m.open_ms, m.close_ms FROM markets m
            LEFT JOIN backfill_log b ON b.ticker = m.ticker
            WHERE m.result IN ('yes','no') AND b.ticker IS NULL AND m.open_ms IS NOT NULL
              AND m.close_ms IS NOT NULL AND m.close_ms > ?
            ORDER BY m.close_ms DESC LIMIT ?""", (now_ms() - self.days * DAY_MS, limit))
        done = 0
        for r in todo:
            if self.stop_event.is_set():
                break
            start, end = r["open_ms"] // 1000, r["close_ms"] // 1000 + 60
            err, candles = None, []
            try:
                candles = self.client.get_candlesticks(r["series_ticker"], r["ticker"], start, end, 1)
            except KalshiAPIError as exc:
                try:  # older markets move to the historical tier
                    candles = self.client.get_historical_candlesticks(r["ticker"], start, end, 1)
                except KalshiAPIError as exc2:
                    err = f"{exc} / {exc2}"[:300]
                    self.progress["errors"] += 1
            rows = parse_candles(r["ticker"], candles)
            self.db.insert_many("market_candles", rows, or_ignore=True)
            self.db.upsert("backfill_log", {"ticker": r["ticker"], "candles": len(rows),
                                            "fetched_ms": now_ms(), "error": err}, "ticker")
            done += 1
            self.progress["markets_done"] += 1
            if done % 20 == 0:
                self._save()
            self.stop_event.wait(self.pause_s)
        return done

    # -- underlying one-minute candles ------------------------------------------
    def crypto_candles(self, symbol: str) -> int:
        provider = self.crypto.name
        row = self.db.query_one("SELECT MAX(start_ms) AS t FROM crypto_candles WHERE symbol=? AND provider=?",
                                (symbol, provider))
        start = (row["t"] + 60_000) if row and row["t"] else now_ms() - self.days * DAY_MS
        end_limit = now_ms() - 60_000           # only completed minutes
        added = 0
        while start < end_limit and not self.stop_event.is_set():
            end = min(start + 299 * 60_000, end_limit)    # Coinbase returns at most 300 candles
            try:
                candles = self.crypto.candles(symbol, start, end, 60)
            except Exception as exc:
                log.warning("Crypto candles %s failed: %s", symbol, exc)
                self.progress["errors"] += 1
                break
            rows = [{"symbol": symbol, "provider": provider, "start_ms": c.start_ms, "open": c.open,
                     "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
                    for c in candles if c.start_ms + 60_000 <= end_limit]
            self.db.insert_many("crypto_candles", rows, or_ignore=True)
            added += len(rows)
            start = end + 60_000
            self.stop_event.wait(self.pause_s)
        self.progress["crypto_minutes"] += added
        return added

    def run_cycle(self) -> None:
        self._save(phase="finding settled markets")
        found = sum(self.settled_markets(s) for s in self.symbols)
        self._save(markets_found=found, phase="downloading market candles")
        self.market_candles()
        self._save(phase="downloading crypto candles")
        for s in self.symbols:
            self.crypto_candles(s)
        self._save(phase="up to date", cycles=self.progress.get("cycles", 0) + 1,
                   last_complete_ms=now_ms())

    def run(self) -> None:
        self.stop_event.wait(20)       # let live collection start first
        while not self.stop_event.is_set():
            if self.paused_fn():
                self.stop_event.wait(5)
                continue
            try:
                self.run_cycle()
            except Exception as exc:
                log.exception("Backfill cycle failed")
                self.db.log_event("backfill", "error", f"History download failed: {exc}")
                self._save(phase=f"error, retrying: {exc}"[:120])
            self.stop_event.wait(15 * 60)   # top up every 15 minutes
