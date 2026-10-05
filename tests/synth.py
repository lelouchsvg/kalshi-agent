"""Synthetic history for tests: a random-walk price and 15-minute markets whose
prices react either fully (efficient) or sluggishly (exploitable) to the price."""
import math
import random

from kalshi_agent.db import now_ms
from kalshi_agent.model import phi

MIN = 60_000


def make_history(db, *, days=2, sluggish=True, seed=7, symbol="BTC", start_ms=None, vol=0.0008):
    rnd = random.Random(seed)
    if start_ms is None:
        start_ms = (now_ms() - (days + 1) * 86_400_000) // (15 * MIN) * (15 * MIN)
    start = start_ms
    n = days * 1440 + 60
    price, closes = 60000.0, []
    candles = []
    for i in range(n):
        price *= math.exp(rnd.gauss(0, vol))
        closes.append(price)
        candles.append({"symbol": symbol, "provider": "coinbase", "start_ms": start + i * MIN, "open": price,
                        "high": price, "low": price, "close": price, "volume": 1.0})
    db.insert_many("crypto_candles", candles)
    sigma = vol / math.sqrt(60)
    m_rows, c_rows = [], []
    for k in range(4, n // 15 - 1):
        o = start + k * 15 * MIN
        c = o + 15 * MIN
        strike = closes[k * 15 - 1]                 # price when the window opened
        final = closes[k * 15 + 14]
        result = "yes" if final >= strike else "no"
        ticker = f"KX{symbol}15M-{k}"
        m_rows.append({"ticker": ticker, "series_ticker": f"KX{symbol}15M", "symbol": symbol, "title": "t",
                       "status": "settled", "open_ms": o, "close_ms": c, "strike_type": "greater_or_equal",
                       "floor_strike": strike, "result": result, "expiration_value": final,
                       "first_seen_ms": o, "updated_ms": c})
        for j in range(1, 15):
            t = o + j * MIN
            spot = closes[k * 15 + j - 1]           # last candle closed by t
            tau = (c - t) / 1000
            fair = phi(math.log(spot / strike) / (sigma * math.sqrt(tau)))
            mid = 0.5 + 0.35 * (fair - 0.5) if sluggish else fair
            mid = min(0.97, max(0.03, mid + rnd.gauss(0, 0.005)))
            c_rows.append({"ticker": ticker, "end_ms": t, "yes_bid_close": round(mid - 0.01, 2),
                           "yes_ask_close": round(mid + 0.01, 2), "price_close": round(mid, 2),
                           "volume": 10.0, "open_interest": 5.0})
    db.insert_many("markets", m_rows)
    db.insert_many("market_candles", c_rows)
    return start, n
