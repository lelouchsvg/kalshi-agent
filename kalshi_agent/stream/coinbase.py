"""Coinbase public WebSocket ticker for BTC, ETH and SOL (free, no account needed).

Coinbase sends many ticks per second; we keep the latest one per symbol and store at
most one row per symbol per second, which is plenty for 15-minute markets and keeps
the database small on a laptop.
"""
from __future__ import annotations

import json
import threading
import time

from ..crypto.provider import PriceTick
from ..db import Database, now_ms
from ..kalshi.models import iso_to_ms, to_float
from .base import StreamFeed

PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
PROVIDER = "coinbase_ws"


class CoinbaseStream(StreamFeed):
    name_ = "coinbase_ws"
    URL = "wss://ws-feed.exchange.coinbase.com"

    def __init__(self, db: Database, stop_event: threading.Event, symbols: list[str],
                 clock_offset_ms=lambda: 0, **kw):
        super().__init__(db, stop_event, **kw)
        self.symbols = [s for s in symbols if s in PRODUCTS]
        self.by_product = {PRODUCTS[s]: s for s in self.symbols}
        self.clock_offset_ms = clock_offset_ms
        self.latest: dict[str, PriceTick] = {}
        self._pending: dict[str, PriceTick] = {}
        self._last_flush = 0.0
        self._lock = threading.Lock()

    def url(self) -> str:
        return self.URL

    def on_open(self, ws) -> None:
        ws.send(json.dumps({"type": "subscribe", "product_ids": list(self.by_product),
                            "channels": ["ticker", "heartbeat"]}))

    def on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "error":
            self.stats.last_error = str(msg.get("message") or msg)[:200]
            return
        if kind != "ticker":
            if kind == "heartbeat":
                self.stats.message()
            return
        sym = self.by_product.get(msg.get("product_id"))
        price = to_float(msg.get("price"))
        if sym is None or price is None:
            return
        received = now_ms()
        ts = iso_to_ms(msg.get("time")) or received
        tick = PriceTick(sym, price, ts, received, PROVIDER, bid=to_float(msg.get("best_bid")),
                         ask=to_float(msg.get("best_ask")), volume_24h=to_float(msg.get("volume_24h")))
        # latency on a common clock: our receive time corrected by the measured offset
        self.stats.message(latency_ms=received + self.clock_offset_ms() - ts)
        with self._lock:
            self.latest[sym] = tick
            self._pending[sym] = tick

    def on_idle(self) -> None:
        if time.monotonic() - self._last_flush >= 1.0:
            self.flush()

    def on_close(self) -> None:
        self.flush()

    def flush(self) -> int:
        self._last_flush = time.monotonic()
        with self._lock:
            ticks, self._pending = list(self._pending.values()), {}
        for t in ticks:
            self.db.insert("crypto_prices", {
                "symbol": t.symbol, "ts_ms": t.ts_ms, "received_ms": t.received_ms, "price": t.price,
                "bid": t.bid, "ask": t.ask, "volume_24h": t.volume_24h, "provider": t.provider})
        return len(ticks)

    def fresh_price(self, symbol: str, max_age_ms: int = 15_000) -> PriceTick | None:
        with self._lock:
            t = self.latest.get(symbol)
        return t if t and now_ms() - t.received_ms <= max_age_ms else None
