"""Market discovery and REST snapshots for Kalshi 15-minute crypto markets."""
from __future__ import annotations

import json
import logging
import re

from .db import Database, now_ms
from .kalshi.client import KalshiAPIError, KalshiClient
from .kalshi.models import Market, iso_to_ms, price, qty

log = logging.getLogger(__name__)

_SYMBOL_ALIASES = {"BTC": ("BTC", "BITCOIN"), "ETH": ("ETH", "ETHEREUM"), "SOL": ("SOL", "SOLANA")}


def detect_15m_series(client: KalshiClient, symbols: list[str]) -> dict[str, str]:
    """Find 15-minute crypto series from Kalshi's series list (fallback when the
    configured ticker returns nothing)."""
    found: dict[str, str] = {}
    try:
        series = client.list_series(category="Crypto")
    except KalshiAPIError as exc:
        log.warning("Could not list crypto series: %s", exc)
        return found
    for s in series:
        ticker = str(s.get("ticker", "")).upper()
        text = f"{ticker} {s.get('title', '')}".upper()
        is_15m = ticker.endswith("15M") or "15 MIN" in text or str(s.get("frequency", "")).lower() in (
            "fifteen_min", "15min", "15_min")
        if not is_15m:
            continue
        for sym in symbols:
            if any(re.search(rf"\b{a}\b", text) or ticker.startswith(f"KX{a}") for a in _SYMBOL_ALIASES.get(sym, (sym,))):
                found.setdefault(sym, ticker)
    return found


def store_series_info(client: KalshiClient, db: Database, symbol: str, series_ticker: str) -> None:
    try:
        s = client.get_series(series_ticker)
    except KalshiAPIError as exc:
        log.warning("Series %s lookup failed: %s", series_ticker, exc)
        return
    if not s:
        return
    mult = s.get("fee_multiplier")
    db.upsert("series_info", {
        "series_ticker": series_ticker, "symbol": symbol, "title": s.get("title"),
        "frequency": s.get("frequency"), "fee_type": s.get("fee_type"),
        "fee_multiplier": float(mult) if mult not in (None, "") else None,
        "updated_ms": now_ms(), "raw_json": json.dumps(s)}, "series_ticker")


def upsert_market(db: Database, m: Market, symbol: str, series_ticker: str) -> None:
    ts = now_ms()
    existing = db.query_one("SELECT first_seen_ms FROM markets WHERE ticker=?", (m.ticker,))
    db.upsert("markets", {
        "ticker": m.ticker, "event_ticker": m.event_ticker, "series_ticker": series_ticker,
        "symbol": symbol, "title": m.title, "status": m.status, "open_ms": m.open_ms,
        "close_ms": m.close_ms, "expiration_ms": m.expiration_ms, "strike_type": m.strike_type,
        "floor_strike": m.floor_strike, "cap_strike": m.cap_strike, "result": m.result,
        "rules_primary": m.rules_primary, "expiration_value": m.expiration_value,
        "first_seen_ms": existing["first_seen_ms"] if existing else ts,
        "updated_ms": ts, "raw_json": json.dumps(m.raw)}, "ticker")


def record_snapshot(db: Database, m: Market, source: str = "rest") -> None:
    ts = now_ms()
    db.insert("market_snapshots", {
        "ticker": m.ticker, "ts_ms": ts, "status": m.status, "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask, "no_bid": m.no_bid, "no_ask": m.no_ask, "last_price": m.last_price,
        "spread": m.spread, "volume": m.volume, "open_interest": m.open_interest,
        "seconds_to_close": m.seconds_to_close(ts), "source": source})


def discover(client: KalshiClient, db: Database, symbols: list[str],
             series_map: dict[str, str]) -> dict[str, list[Market]]:
    """Fetch open 15-minute markets per symbol, store them, return them."""
    result: dict[str, list[Market]] = {}
    missing = []
    for sym in symbols:
        series = series_map.get(sym)
        markets: list[Market] = []
        if series:
            try:
                markets = client.get_markets(series_ticker=series, status="open")
            except KalshiAPIError as exc:
                log.warning("Market lookup for %s (%s) failed: %s", sym, series, exc)
        if not markets:
            missing.append(sym)
        result[sym] = markets
    if missing:
        detected = detect_15m_series(client, missing)
        for sym, series in detected.items():
            if series != series_map.get(sym):
                db.log_event("discovery", "warning",
                             f"Configured series for {sym} returned no markets; using detected {series}")
                series_map[sym] = series
                try:
                    result[sym] = client.get_markets(series_ticker=series, status="open")
                except KalshiAPIError as exc:
                    log.warning("Detected series %s failed: %s", series, exc)
    for sym, markets in result.items():
        for m in markets:
            upsert_market(db, m, sym, series_map.get(sym, ""))
            record_snapshot(db, m)
    total = sum(len(v) for v in result.values())
    db.heartbeat("discovery", {"markets": total, "by_symbol": {k: len(v) for k, v in result.items()}})
    return result


def snapshot_orderbook(client: KalshiClient, db: Database, ticker: str, depth: int = 10) -> None:
    ob = client.get_orderbook(ticker, depth=depth)
    db.insert("orderbook_snapshots", {
        "ticker": ticker, "ts_ms": now_ms(), "best_yes_bid": ob.best_yes_bid,
        "best_yes_ask": ob.best_yes_ask, "yes_depth": ob.yes_depth, "no_depth": ob.no_depth,
        "imbalance": ob.imbalance, "levels_json": json.dumps({"yes": ob.yes, "no": ob.no})})


def refresh_markets(client: KalshiClient, db: Database, tickers: list[str], depth: int = 10) -> int:
    """Re-read live markets and their order books."""
    if not tickers:
        return 0
    markets = client.get_markets(tickers=tickers, max_pages=1)
    for m in markets:
        db.execute("UPDATE markets SET status=?, result=?, expiration_value=COALESCE(?, expiration_value), "
                   "updated_ms=? WHERE ticker=?",
                   (m.status, m.result, m.expiration_value, now_ms(), m.ticker))
        record_snapshot(db, m)
        if m.is_tradeable_status:
            try:
                snapshot_orderbook(client, db, m.ticker, depth)
            except KalshiAPIError as exc:
                log.warning("Orderbook %s failed: %s", m.ticker, exc)
    db.heartbeat("snapshots", {"markets": len(markets)})
    return len(markets)


def record_trades(client: KalshiClient, db: Database, ticker: str, since_ms: int | None = None) -> int:
    """Store public trades printed on a market since `since_ms` (duplicates ignored)."""
    trades = client.get_trades(ticker, min_ts=since_ms // 1000 if since_ms else None, max_pages=3)
    received = now_ms()
    rows = []
    for t in trades:
        tid = t.get("trade_id")
        ts = iso_to_ms(t.get("created_time"))
        if not tid or ts is None:
            continue
        rows.append({"trade_id": str(tid), "ticker": t.get("ticker") or ticker, "ts_ms": ts,
                     "received_ms": received, "yes_price": price(t, "yes_price"),
                     "count": qty(t, "count"), "taker_side": t.get("taker_side")})
    return db.insert_many("kalshi_trades", rows, or_ignore=True)
