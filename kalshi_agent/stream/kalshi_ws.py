"""Kalshi WebSocket stream (order book, ticker, trades). Needs a Kalshi API key.

Kalshi requires a signed handshake even for public market data, so this feed only
runs once API credentials are stored on the Mac (in .env, never in chat). Without
them the collector polls Kalshi's REST API instead, which is slower but complete.

Message field names follow Kalshi's fixed-point format (`*_dollars`, `*_fp`), with
the older cent-based names accepted as a fallback. This parser has been exercised
only against recorded-format test messages, not the live exchange; the dashboard
marks the feed as unverified until it has run against real data.
"""
from __future__ import annotations

import json
import threading
import time

from ..db import Database, now_ms
from ..kalshi.auth import KalshiSigner
from ..kalshi.models import Orderbook, to_float
from .base import StreamFeed
from .ws import WebSocketError

WS_PATH = "/trade-api/ws/v2"


def _price(d: dict, name: str) -> float | None:
    v = to_float(d.get(f"{name}_dollars"))
    if v is not None:
        return v
    cents = to_float(d.get(name))
    return cents / 100 if cents is not None else None


def _qty(d: dict, name: str) -> float | None:
    v = to_float(d.get(f"{name}_fp"))
    return v if v is not None else to_float(d.get(name))


def _levels(msg: dict, side: str) -> dict[float, float]:
    for key, cents in ((f"{side}_dollars_fp", False), (f"{side}_dollars", False), (side, True)):
        if key in msg and msg[key] is not None:
            out = {}
            for p, q in msg[key]:
                p, q = to_float(p), to_float(q)
                if p is None or q is None:
                    continue
                out[round(p / 100 if cents else p, 4)] = q
            return out
    return {}


class LocalBook:
    """Order book rebuilt from a snapshot plus deltas (bids on both sides)."""

    def __init__(self):
        self.yes: dict[float, float] = {}
        self.no: dict[float, float] = {}

    def snapshot(self, msg: dict) -> None:
        self.yes, self.no = _levels(msg, "yes"), _levels(msg, "no")

    def delta(self, msg: dict) -> None:
        p = _price(msg, "price")
        dq = _qty(msg, "delta")
        side = self.yes if msg.get("side") == "yes" else self.no
        if p is None or dq is None:
            return
        p = round(p, 4)
        q = side.get(p, 0.0) + dq
        if q <= 1e-9:
            side.pop(p, None)
        else:
            side[p] = q

    def to_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        top = lambda d: sorted(d.items(), key=lambda kv: -kv[0])[:depth]  # noqa: E731
        return Orderbook(ticker, top(self.yes), top(self.no))


class KalshiStream(StreamFeed):
    name_ = "kalshi_ws"

    def __init__(self, db: Database, stop_event: threading.Event, ws_url: str, signer: KalshiSigner,
                 tickers_fn, depth: int = 10, **kw):
        super().__init__(db, stop_event, **kw)
        self.ws_url = ws_url
        self.signer = signer
        self.tickers_fn = tickers_fn
        self.depth = depth
        self.books: dict[str, LocalBook] = {}
        self.subscribed: frozenset[str] = frozenset()
        self._last_ticker_row: dict[str, int] = {}
        self._seq: dict[int, int] = {}
        self._dirty: set[str] = set()
        self._last_book_save = 0.0
        self.stats.status_note = "unverified against live Kalshi data until it has run"

    def url(self) -> str:
        return self.ws_url

    def headers(self) -> dict[str, str]:
        return self.signer.headers("GET", WS_PATH)

    def on_open(self, ws) -> None:
        self.subscribed = frozenset(self.tickers_fn())
        self.books, self._seq = {}, {}
        if not self.subscribed:
            raise WebSocketError("no open markets to subscribe to yet")
        ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
            "channels": ["orderbook_delta", "ticker", "trade"],
            "market_tickers": sorted(self.subscribed)}}))

    def _check_seq(self, msg: dict) -> None:
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is None or seq is None:
            return
        last = self._seq.get(sid)
        if last is not None and seq != last + 1:
            raise WebSocketError(f"sequence gap on sid {sid}: {last} -> {seq}; resyncing")
        self._seq[sid] = seq

    def on_message(self, env: dict) -> None:
        kind = env.get("type")
        msg = env.get("msg") or {}
        if kind == "error":
            self.stats.last_error = json.dumps(msg)[:200]
            return
        ticker = msg.get("market_ticker")
        if kind in ("orderbook_snapshot", "orderbook_delta"):
            self._check_seq(env)
            book = self.books.setdefault(ticker, LocalBook())
            (book.snapshot if kind == "orderbook_snapshot" else book.delta)(msg)
            self._dirty.add(ticker)
            self.stats.message()
        elif kind == "ticker":
            ts = to_float(msg.get("ts"))
            received = now_ms()
            self.stats.message(latency_ms=received - ts * 1000 if ts else None)
            # Busy markets send many ticker updates a second; one row per market per
            # second is plenty and keeps the database small on a laptop.
            if received - self._last_ticker_row.get(ticker, 0) < 1000:
                return
            self._last_ticker_row[ticker] = received
            yb, ya = _price(msg, "yes_bid"), _price(msg, "yes_ask")
            self.db.insert("market_snapshots", {
                "ticker": ticker, "ts_ms": received, "status": None, "yes_bid": yb, "yes_ask": ya,
                "no_bid": None if ya is None else round(1 - ya, 4),
                "no_ask": None if yb is None else round(1 - yb, 4),
                "last_price": _price(msg, "price"),
                "spread": None if yb is None or ya is None else round(ya - yb, 4),
                "volume": _qty(msg, "volume"), "open_interest": _qty(msg, "open_interest"),
                "seconds_to_close": None, "source": "ws"})
        elif kind == "trade":
            ts = to_float(msg.get("ts"))
            tid = msg.get("trade_id")
            if tid and ts:
                self.db.insert_many("kalshi_trades", [{
                    "trade_id": str(tid), "ticker": ticker, "ts_ms": int(ts * 1000), "received_ms": now_ms(),
                    "yes_price": _price(msg, "yes_price"), "count": _qty(msg, "count"),
                    "taker_side": msg.get("taker_side")}], or_ignore=True)
            self.stats.message()

    def on_idle(self) -> None:
        if time.monotonic() - self._last_book_save >= 2.0 and self._dirty:
            self._last_book_save = time.monotonic()
            dirty, self._dirty = self._dirty, set()
            for t in dirty:
                ob = self.books[t].to_orderbook(t, self.depth)
                self.db.insert("orderbook_snapshots", {
                    "ticker": t, "ts_ms": now_ms(), "best_yes_bid": ob.best_yes_bid,
                    "best_yes_ask": ob.best_yes_ask, "yes_depth": ob.yes_depth, "no_depth": ob.no_depth,
                    "imbalance": ob.imbalance, "levels_json": json.dumps({"yes": ob.yes, "no": ob.no})})
        # new 15-minute markets open every quarter hour: reconnect to pick them up
        if frozenset(self.tickers_fn()) - self.subscribed:
            raise WebSocketError("market list changed; resubscribing")
