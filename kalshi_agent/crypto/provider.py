"""Underlying crypto price providers behind one interface.

Kalshi's 15-minute crypto markets settle on a CF Benchmarks real-time index
(60-second average before close). Kalshi streams that index over its WebSocket
(`cfbenchmarks_value` channel); Phase 2 adds it as the primary provider. Exchange
spot prices from Coinbase/Kraken serve as a free, independent reference.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import requests


@dataclass
class PriceTick:
    symbol: str
    price: float
    ts_ms: int            # provider's timestamp for this price
    received_ms: int      # our clock when it arrived
    provider: str
    bid: float | None = None
    ask: float | None = None
    volume_24h: float | None = None


@dataclass
class Candle:
    symbol: str
    start_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float


class CryptoDataProvider(ABC):
    name = "base"

    @abstractmethod
    def latest(self, symbol: str) -> PriceTick: ...

    @abstractmethod
    def candles(self, symbol: str, start_ms: int, end_ms: int, granularity_s: int = 60) -> list[Candle]: ...


def _now_ms() -> int:
    return int(time.time() * 1000)


class CoinbaseProvider(CryptoDataProvider):
    """Coinbase Exchange public market data (no key, free)."""
    name = "coinbase"
    BASE = "https://api.exchange.coinbase.com"
    PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}

    def __init__(self, session: requests.Session | None = None, timeout: float = 5):
        self.session = session or requests.Session()
        self.timeout = timeout

    def latest(self, symbol: str) -> PriceTick:
        r = self.session.get(f"{self.BASE}/products/{self.PRODUCTS[symbol]}/ticker", timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        ts = d.get("time")
        ts_ms = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000) if ts else _now_ms()
        return PriceTick(symbol, float(d["price"]), ts_ms, _now_ms(), self.name,
                         bid=float(d["bid"]) if d.get("bid") else None,
                         ask=float(d["ask"]) if d.get("ask") else None,
                         volume_24h=float(d["volume"]) if d.get("volume") else None)

    def candles(self, symbol: str, start_ms: int, end_ms: int, granularity_s: int = 60) -> list[Candle]:
        r = self.session.get(
            f"{self.BASE}/products/{self.PRODUCTS[symbol]}/candles",
            params={"granularity": granularity_s,
                    "start": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
                    "end": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat()},
            timeout=self.timeout)
        r.raise_for_status()
        # rows: [time, low, high, open, close, volume], newest first
        return sorted((Candle(symbol, int(t) * 1000, o, h, lo, c, v) for t, lo, h, o, c, v in r.json()),
                      key=lambda c: c.start_ms)


class KrakenProvider(CryptoDataProvider):
    """Kraken public market data (no key, free)."""
    name = "kraken"
    BASE = "https://api.kraken.com/0/public"
    PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD"}

    def __init__(self, session: requests.Session | None = None, timeout: float = 5):
        self.session = session or requests.Session()
        self.timeout = timeout

    def latest(self, symbol: str) -> PriceTick:
        r = self.session.get(f"{self.BASE}/Ticker", params={"pair": self.PAIRS[symbol]}, timeout=self.timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RuntimeError(f"Kraken error: {d['error']}")
        t = next(iter(d["result"].values()))
        now = _now_ms()
        # Kraken's ticker has no timestamp; we record receipt time for both.
        return PriceTick(symbol, float(t["c"][0]), now, now, self.name,
                         bid=float(t["b"][0]), ask=float(t["a"][0]), volume_24h=float(t["v"][1]))

    def candles(self, symbol: str, start_ms: int, end_ms: int, granularity_s: int = 60) -> list[Candle]:
        r = self.session.get(f"{self.BASE}/OHLC", params={
            "pair": self.PAIRS[symbol], "interval": max(granularity_s // 60, 1), "since": start_ms // 1000},
            timeout=self.timeout)
        r.raise_for_status()
        rows = next(v for k, v in r.json()["result"].items() if k != "last")
        return [Candle(symbol, int(t) * 1000, float(o), float(h), float(lo), float(c), float(v))
                for t, o, h, lo, c, _vw, v, _n in rows if int(t) * 1000 <= end_ms]


PROVIDERS = {"coinbase": CoinbaseProvider, "kraken": KrakenProvider}


def get_provider(name: str) -> CryptoDataProvider:
    try:
        return PROVIDERS[name.lower()]()
    except KeyError:
        raise ValueError(f"Unknown crypto provider {name!r}; choose one of {sorted(PROVIDERS)}")
