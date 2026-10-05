"""Build training/backtest examples from history, strictly point-in-time.

One example = one settled market at one decision moment t (each completed Kalshi
one-minute candle). Inputs available at t: that candle's bid/ask, and Coinbase
one-minute candles that had fully closed by t. Label: the market's official result.

Markets are put in an "above the strike" frame so one model serves both
"above" and "below" questions: for a "below" market, P(yes) = 1 - P(above).
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

from .db import Database
from .model import FeatureRow, compute_features

ABOVE = ("greater", "greater_or_equal", "above")
BELOW = ("less", "less_or_equal", "below")


def frame(strike_type: str | None, floor: float | None, cap: float | None):
    """(strike, flip) for the 'above' frame, or None when unsupported (e.g. ranges)."""
    st = (strike_type or "").lower()
    if st in ABOVE and floor is not None:
        return floor, False
    if st in BELOW and cap is not None:
        return cap, True
    return None


def above_quotes(yes_bid: float | None, yes_ask: float | None, flip: bool):
    """Bid/ask for 'settles above' given the market's YES bid/ask."""
    if not flip:
        return yes_bid, yes_ask
    if yes_bid is None or yes_ask is None:
        return None, None
    return round(1 - yes_ask, 4), round(1 - yes_bid, 4)


@dataclass
class Example:
    ticker: str
    symbol: str
    close_ms: int
    t_ms: int
    flip: bool
    features: FeatureRow
    label_above: int
    yes_bid: float
    yes_ask: float
    result: str


class CandleIndex:
    """Coinbase one-minute closes for one symbol, searchable by time."""

    def __init__(self, db: Database, symbol: str, provider: str = "coinbase"):
        rows = db.query("SELECT start_ms, close FROM crypto_candles WHERE symbol=? AND provider=? "
                        "ORDER BY start_ms", (symbol, provider))
        self.starts = [r["start_ms"] for r in rows]
        self.closes = [r["close"] for r in rows]

    def closes_asof(self, t_ms: int, n: int = 31, max_gap_ms: int = 180_000) -> list[float]:
        """Closes of the last n candles that had fully closed by t_ms (empty if stale)."""
        i = bisect.bisect_right(self.starts, t_ms - 60_000)
        if i == 0 or t_ms - 60_000 - self.starts[i - 1] > max_gap_ms:
            return []
        return self.closes[max(0, i - n):i]


def build_examples(db: Database, symbols: list[str], since_ms: int = 0) -> list[Example]:
    out: list[Example] = []
    for sym in symbols:
        idx = CandleIndex(db, sym)
        if not idx.starts:
            continue
        markets = db.query("""SELECT ticker, open_ms, close_ms, result, strike_type, floor_strike, cap_strike
                              FROM markets WHERE symbol=? AND result IN ('yes','no') AND close_ms > ?
                              AND open_ms IS NOT NULL ORDER BY close_ms""", (sym, since_ms))
        for m in markets:
            fr = frame(m["strike_type"], m["floor_strike"], m["cap_strike"])
            if fr is None:
                continue
            strike, flip = fr
            label_yes = 1 if m["result"] == "yes" else 0
            window = m["close_ms"] - m["open_ms"]
            for c in db.query("SELECT end_ms, yes_bid_close, yes_ask_close FROM market_candles "
                              "WHERE ticker=? AND end_ms > ? AND end_ms <= ? ORDER BY end_ms",
                              (m["ticker"], m["open_ms"] + 60_000, m["close_ms"] - 60_000)):
                closes = idx.closes_asof(c["end_ms"])
                if len(closes) < 11:
                    continue
                bid, ask = above_quotes(c["yes_bid_close"], c["yes_ask_close"], flip)
                f = compute_features(spot=closes[-1], strike=strike, tau_s=(m["close_ms"] - c["end_ms"]) / 1000,
                                     closes_1m=closes, yes_bid=bid, yes_ask=ask, window_s=window / 1000)
                if f is None:
                    continue
                out.append(Example(m["ticker"], sym, m["close_ms"], c["end_ms"], flip, f,
                                   (1 - label_yes) if flip else label_yes,
                                   c["yes_bid_close"], c["yes_ask_close"], m["result"]))
    out.sort(key=lambda e: (e.close_ms, e.t_ms))
    return out


def split_by_market(examples: list[Example], test_frac: float = 0.3, embargo_ms: int = 900_000):
    """Chronological split on market close time: the test set is strictly later
    than everything in the training set, as it would be in real use. Markets in the
    15 minutes after the cut are dropped (an embargo), because they were trading at
    the same time as the last training markets and would leak information."""
    closes = sorted({e.close_ms for e in examples})
    if len(closes) < 10:
        return examples, []
    cut = closes[int(len(closes) * (1 - test_frac))]
    return ([e for e in examples if e.close_ms < cut],
            [e for e in examples if e.close_ms >= cut + embargo_ms])
