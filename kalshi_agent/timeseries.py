"""Point-in-time ("as of") reads. The single rule that prevents look-ahead bias:

    A feature computed for decision time T may only use rows that had *arrived*
    on this machine by T.

So these helpers filter on the arrival clock (`received_ms`, or `ts_ms` for rows we
stamp on arrival), never on the source's own timestamp, and candles count only once
their period has fully ended. Phase 3 features and the Phase 4 backtester read data
exclusively through this module.
"""
from __future__ import annotations

from typing import Any

from .db import Database

# table -> (key column, arrival-time column)
ARRIVAL = {
    "crypto_prices": ("symbol", "received_ms"),
    "index_ticks": ("symbol", "received_ms"),
    "market_snapshots": ("ticker", "ts_ms"),
    "orderbook_snapshots": ("ticker", "ts_ms"),
    "kalshi_trades": ("ticker", "received_ms"),
}


def asof(db: Database, table: str, key: str, t_ms: int, max_age_ms: int | None = None) -> dict[str, Any] | None:
    """Latest row for `key` that had arrived by `t_ms` (optionally no older than max_age_ms)."""
    key_col, rx = ARRIVAL[table]
    lo = t_ms - max_age_ms if max_age_ms is not None else -1
    return db.query_one(f"SELECT * FROM {table} WHERE {key_col}=? AND {rx}<=? AND {rx}>=? "
                        f"ORDER BY {rx} DESC LIMIT 1", (key, t_ms, lo))


def window(db: Database, table: str, key: str, start_ms: int, t_ms: int) -> list[dict[str, Any]]:
    """Rows for `key` that arrived in (start_ms, t_ms], oldest first."""
    key_col, rx = ARRIVAL[table]
    return db.query(f"SELECT * FROM {table} WHERE {key_col}=? AND {rx}>? AND {rx}<=? ORDER BY {rx}",
                    (key, start_ms, t_ms))


def crypto_candles_asof(db: Database, symbol: str, t_ms: int, n: int,
                        provider: str = "coinbase") -> list[dict[str, Any]]:
    """The last n one-minute candles that had fully closed by t_ms, oldest first."""
    rows = db.query("SELECT * FROM crypto_candles WHERE symbol=? AND provider=? AND start_ms + 60000 <= ? "
                    "ORDER BY start_ms DESC LIMIT ?", (symbol, provider, t_ms, n))
    return rows[::-1]


def market_candles_asof(db: Database, ticker: str, t_ms: int) -> list[dict[str, Any]]:
    """Kalshi one-minute candles for a market that had closed by t_ms, oldest first."""
    return db.query("SELECT * FROM market_candles WHERE ticker=? AND end_ms <= ? ORDER BY end_ms",
                    (ticker, t_ms))


def settlement_proxy(db: Database, symbol: str, close_ms: int) -> float | None:
    """Our estimate of the settlement value: average of the index proxy over the
    60 seconds before close. Only meaningful after close_ms."""
    row = db.query_one("SELECT AVG(value) AS v, COUNT(*) AS n FROM index_ticks WHERE symbol=? "
                       "AND received_ms > ? AND received_ms <= ?", (symbol, close_ms - 60_000, close_ms))
    return row["v"] if row and row["n"] else None
