"""A free stand-in for the CF Benchmarks settlement index.

Kalshi settles its 15-minute crypto markets on CF Benchmarks' Real-Time Index:
the average of the index over the 60 seconds before close. The index itself is a
paid product (Kalshi streams it only to users with API keys), but it is built from
the order books of a handful of large USD exchanges. We approximate it with the
median mid-price of four of those exchanges, and keep a rolling 60-second average
so features can mirror the settlement rule. How closely the proxy tracks the real
settlement value is measured on every settled market (see state.data_quality).
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
from dataclasses import dataclass
from typing import Callable

import requests

from ..db import Database, now_ms
from ..kalshi.models import to_float

log = logging.getLogger("index")


@dataclass
class Quote:
    source: str
    price: float
    ts_ms: int


def _mid(bid, ask, last) -> float | None:
    b, a = to_float(bid), to_float(ask)
    if b and a and a >= b:
        return (a + b) / 2
    return to_float(last)


class ExchangeQuotes:
    """Public REST tickers from exchanges that feed the CF Benchmarks index."""

    def __init__(self, session: requests.Session | None = None, timeout: float = 4):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, url: str, **params):
        r = self.session.get(url, params=params or None, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def coinbase(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            d = self._get(f"https://api.exchange.coinbase.com/products/{sym}-USD/ticker")
            p = _mid(d.get("bid"), d.get("ask"), d.get("price"))
            if p:
                out[sym] = Quote("coinbase", p, now_ms())
        return out

    def kraken(self, symbols: list[str]) -> dict[str, Quote]:
        pairs = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}
        d = self._get("https://api.kraken.com/0/public/Ticker",
                      pair=",".join(pairs[s] for s in symbols if s in pairs))
        if d.get("error"):
            raise RuntimeError(f"Kraken: {d['error']}")
        out = {}
        for key, t in d.get("result", {}).items():
            k = key.upper()
            sym = "BTC" if "XBT" in k else "ETH" if "ETH" in k else "SOL" if "SOL" in k else None
            p = _mid(t["b"][0], t["a"][0], t["c"][0]) if sym else None
            if p:
                out[sym] = Quote("kraken", p, now_ms())
        return out

    def bitstamp(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            d = self._get(f"https://www.bitstamp.net/api/v2/ticker/{sym.lower()}usd/")
            p = _mid(d.get("bid"), d.get("ask"), d.get("last"))
            ts = to_float(d.get("timestamp"))
            if p:
                out[sym] = Quote("bitstamp", p, int(ts * 1000) if ts else now_ms())
        return out

    def gemini(self, symbols: list[str]) -> dict[str, Quote]:
        out = {}
        for sym in symbols:
            d = self._get(f"https://api.gemini.com/v1/pubticker/{sym.lower()}usd")
            p = _mid(d.get("bid"), d.get("ask"), d.get("last"))
            ts = to_float((d.get("volume") or {}).get("timestamp"))
            if p:
                out[sym] = Quote("gemini", p, int(ts) if ts else now_ms())
        return out


def combine(quotes: list[Quote], max_rel_dev: float = 0.01) -> tuple[float | None, list[Quote]]:
    """Median of the quotes after dropping any more than 1% from the raw median
    (a stale or broken exchange must not move the index)."""
    if not quotes:
        return None, []
    raw = statistics.median(q.price for q in quotes)
    kept = [q for q in quotes if abs(q.price - raw) / raw <= max_rel_dev]
    return (statistics.median(q.price for q in kept) if kept else None), kept


class IndexProxy(threading.Thread):
    SOURCES = ("coinbase", "kraken", "bitstamp", "gemini")

    def __init__(self, db: Database, stop_event: threading.Event, symbols: list[str],
                 interval_s: float = 5, quotes: ExchangeQuotes | None = None,
                 coinbase_live: Callable[[str], object] | None = None,
                 paused_fn: Callable[[], bool] = lambda: False):
        super().__init__(daemon=True, name="index_proxy")
        self.db = db
        self.stop_event = stop_event
        self.symbols = symbols
        self.interval_s = interval_s
        self.quotes = quotes or ExchangeQuotes()
        self.coinbase_live = coinbase_live
        self.paused_fn = paused_fn
        self.source_errors: dict[str, str] = {}

    def gather(self) -> dict[str, list[Quote]]:
        by_sym: dict[str, list[Quote]] = {s: [] for s in self.symbols}
        need_cb_rest = []
        for s in self.symbols:
            tick = self.coinbase_live(s) if self.coinbase_live else None
            if tick is not None:
                bid, ask = getattr(tick, "bid", None), getattr(tick, "ask", None)
                by_sym[s].append(Quote("coinbase", _mid(bid, ask, tick.price), tick.received_ms))
            else:
                need_cb_rest.append(s)
        for src in self.SOURCES:
            syms = need_cb_rest if src == "coinbase" else self.symbols
            if not syms:
                continue
            try:
                for sym, q in getattr(self.quotes, src)(syms).items():
                    by_sym[sym].append(q)
                self.source_errors.pop(src, None)
            except Exception as exc:
                self.source_errors[src] = f"{type(exc).__name__}: {exc}"[:160]
        return by_sym

    def step(self) -> int:
        received = now_ms()
        written = 0
        for sym, quotes in self.gather().items():
            value, kept = combine(quotes)
            if value is None:
                continue
            prev = self.db.query_one(
                "SELECT SUM(value) AS s, COUNT(*) AS n FROM index_ticks WHERE symbol=? AND received_ms > ?",
                (sym, received - 60_000))
            n = (prev["n"] or 0) + 1
            avg60 = ((prev["s"] or 0.0) + value) / n
            self.db.insert("index_ticks", {
                "symbol": sym, "received_ms": received, "value": value, "avg60": avg60,
                "n_sources": len(kept), "sources_json": json.dumps({q.source: q.price for q in kept})})
            written += 1
        self.db.heartbeat("feed:index_proxy", {
            "connected": written > 0, "last_msg_ms": received if written else None,
            "symbols": written, "source_errors": self.source_errors,
            "note": "median of Coinbase, Kraken, Bitstamp, Gemini"})
        return written

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if not self.paused_fn():
                    self.step()
            except Exception as exc:
                log.exception("Index proxy step failed")
                self.db.log_event("index_proxy", "error", f"Index proxy failed: {exc}")
            self.stop_event.wait(self.interval_s)
