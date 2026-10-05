"""Clock check. Every row is stamped with this Mac's clock, so we measure how far it
is from an exchange's clock. A Mac that drifts makes timestamps (and therefore
"what did we know at decision time") unreliable, so health flags large offsets.

offset_ms = exchange_time - local_time, estimated at the midpoint of the round trip
(the same idea NTP uses). Several samples are taken and the one with the shortest
round trip wins, because it has the least network noise.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from typing import Callable

import requests

from .db import Database, now_ms


@dataclass
class ClockReading:
    offset_ms: float
    rtt_ms: float
    source: str
    measured_ms: int
    kalshi_offset_ms: float | None = None   # from Kalshi's HTTP Date header (1-second resolution)


def _coinbase_sample(session: requests.Session) -> tuple[float, float]:
    t0 = time.time() * 1000
    r = session.get("https://api.exchange.coinbase.com/time", timeout=5)
    t1 = time.time() * 1000
    r.raise_for_status()
    server = float(r.json()["epoch"]) * 1000
    return server - (t0 + t1) / 2, t1 - t0


def _kalshi_sample(session: requests.Session, base_url: str) -> float | None:
    t0 = time.time() * 1000
    r = session.get(f"{base_url}/exchange/status", timeout=5)
    t1 = time.time() * 1000
    date = r.headers.get("Date")
    if not date:
        return None
    # Date has 1-second resolution and is truncated, so add half a second on average.
    server = parsedate_to_datetime(date).timestamp() * 1000 + 500
    return server - (t0 + t1) / 2


def measure(session: requests.Session | None = None, kalshi_base_url: str | None = None,
            samples: int = 5, sampler: Callable[[], tuple[float, float]] | None = None) -> ClockReading:
    session = session or requests.Session()
    sampler = sampler or (lambda: _coinbase_sample(session))
    best = min((sampler() for _ in range(samples)), key=lambda x: x[1])
    kalshi = None
    if kalshi_base_url:
        try:
            kalshi = _kalshi_sample(session, kalshi_base_url)
        except Exception:
            kalshi = None
    return ClockReading(round(best[0], 1), round(best[1], 1), "coinbase", now_ms(), kalshi)


class ClockSync:
    def __init__(self, db: Database):
        self.db = db
        self.offset_ms = 0.0
        saved = db.get_control("clock")
        if saved:
            try:
                self.offset_ms = float(json.loads(saved)["offset_ms"])
            except (ValueError, KeyError, TypeError):
                pass

    def update(self, reading: ClockReading) -> None:
        self.offset_ms = reading.offset_ms
        self.db.set_control("clock", json.dumps(asdict(reading)), "clock")

    def current(self) -> float:
        return self.offset_ms
