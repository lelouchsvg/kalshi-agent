"""Paper trading engine (Phase 5). Simulates trades against REAL live Kalshi order
books with fake money. It never talks to Kalshi's order endpoints: it has no client
and cannot obtain an OrderPermit.

Each cycle, for every open 15-minute market:
  live inputs (order book <= 20 s old, Coinbase price <= 15 s old, last 30 minutes of
  one-minute closes) -> model probability -> decide() (same rule as the backtest)
  -> risk.check() -> simulated taker fill at the real best ask, limited to the size
  actually resting there, with Kalshi's taker fee and a slippage allowance.
Positions are held to settlement and closed at $1 or $0 from Kalshi's official result.
"""
from __future__ import annotations

import json
import logging
import uuid

from . import risk
from .dataset import above_quotes, frame
from .db import Database, now_ms
from .fees import taker_fee
from .model import compute_features
from .safety import kill_status
from .strategy import Decision, decide, size
from .trainer import active_model

log = logging.getLogger("paper")
MODE = "PAPER"


def live_closes(db: Database, symbol: str, now: int, minutes: int = 31) -> list[float]:
    """Closing price of each completed minute, from the stream (fallback: candles)."""
    rows = db.query("SELECT ts_ms, price FROM crypto_prices WHERE symbol=? AND received_ms > ? "
                    "AND received_ms <= ? ORDER BY ts_ms", (symbol, now - (minutes + 2) * 60_000, now))
    buckets: dict[int, float] = {}
    for r in rows:
        b = r["ts_ms"] // 60_000
        if (b + 1) * 60_000 <= now:
            buckets[b] = r["price"]
    closes = [buckets[b] for b in sorted(buckets)][-minutes:]
    if len(closes) >= 11:
        return closes
    cand = db.query("SELECT close FROM crypto_candles WHERE symbol=? AND start_ms + 60000 <= ? "
                    "AND start_ms > ? ORDER BY start_ms", (symbol, now, now - (minutes + 3) * 60_000))
    return [c["close"] for c in cand][-minutes:]


class PaperTrader:
    def __init__(self, db: Database, settings):
        self.db = db
        self.s = settings
        self._last_signal: dict[str, tuple[str, str, int]] = {}
        self._last_equity_ms = 0

    # -- bookkeeping ------------------------------------------------------------
    def record_signal(self, ticker: str, d: Decision, model_version: str | None, gates: dict | None = None,
                      force: bool = False) -> None:
        now = now_ms()
        prev = self._last_signal.get(ticker)
        if not force and prev and prev[0] == d.action and prev[1] == d.reason_code and now - prev[2] < 60_000:
            return
        self._last_signal[ticker] = (d.action, d.reason_code, now)
        self.db.insert("signals", {
            "ticker": ticker, "ts_ms": now, "mode": MODE, "model_version": model_version, "side": d.outcome,
            "p_yes": d.p_yes, "market_price": d.market_price, "edge": d.edge, "ev": d.edge,
            "action": d.action, "reason_code": d.reason_code, "explanation": d.explanation,
            "gates_json": json.dumps(gates) if gates else None})
        if d.p_yes is not None and model_version:
            self.db.insert("predictions", {"ticker": ticker, "ts_ms": now, "model_version": model_version,
                                           "p_yes": min(max(d.p_yes, 0.0), 1.0)})

    def settle(self) -> int:
        closed = 0
        for t in self.db.query("""SELECT t.id, t.count, t.entry_price, t.fees, t.side, m.result, m.symbol
                                  FROM trades t JOIN markets m ON m.ticker = t.ticker
                                  WHERE t.mode=? AND t.pnl IS NULL AND m.result IN ('yes','no')""", (MODE,)):
            won = t["side"] == t["result"]
            exit_price = 1.0 if won else 0.0
            pnl = t["count"] * (exit_price - t["entry_price"]) - (t["fees"] or 0)
            self.db.execute("UPDATE trades SET exit_ms=?, exit_price=?, pnl=?, result=? WHERE id=?",
                            (now_ms(), exit_price, pnl, "win" if won else "loss", t["id"]))
            self.db.execute("DELETE FROM positions WHERE mode=? AND ticker=(SELECT ticker FROM trades WHERE id=?)",
                            (MODE, t["id"]))
            self.db.log_event("paper", "info", f"PAPER trade settled {t['result'].upper()}: "
                              f"{'won' if won else 'lost'} ${pnl:+.2f}")
            closed += 1
        if closed:
            self.write_equity(force=True)
        return closed

    def write_equity(self, force: bool = False) -> None:
        now = now_ms()
        if not force and now - self._last_equity_ms < 300_000:
            return
        self._last_equity_ms = now
        st = risk.account_state(self.db, MODE, self.s.paper_starting_balance)
        unreal = 0.0
        for t in self.db.query("SELECT ticker, side, count, entry_price, fees FROM trades "
                               "WHERE mode=? AND pnl IS NULL", (MODE,)):
            ob = self.db.query_one("SELECT best_yes_bid, best_yes_ask FROM orderbook_snapshots WHERE ticker=? "
                                   "ORDER BY ts_ms DESC LIMIT 1", (t["ticker"],))
            if not ob:
                continue
            bid = ob["best_yes_bid"] if t["side"] == "yes" else (
                None if ob["best_yes_ask"] is None else 1 - ob["best_yes_ask"])
            if bid is not None:
                unreal += t["count"] * (bid - t["entry_price"]) - (t["fees"] or 0)
        self.db.insert("pnl", {"mode": MODE, "ts_ms": now, "equity": st.equity + unreal,
                               "realized": st.realized, "unrealized": unreal, "fees": st.fees})

    # -- the decision loop ------------------------------------------------------------
    def blocked_reason(self) -> tuple[str, str] | None:
        ks = kill_status(self.db, self.s.kill_switch, self.s.kill_file)
        if ks.killed:
            return "KILL_SWITCH", f"Kill switch engaged ({', '.join(ks.sources)})."
        try:
            effective = json.loads(self.db.get_control("mode_state") or "{}").get("effective",
                                                                                 self.s.trading_mode.value)
        except ValueError:
            effective = "WATCH"
        if effective != MODE:
            return "NOT_PAPER_MODE", f"Running in {effective} mode, which doesn't paper trade."
        if self.db.get_control("collector", "running") == "paused":
            return "PAUSED", "Data collection is paused."
        return None

    def open_markets(self, now: int) -> list[dict]:
        marks = ",".join("?" for _ in self.s.symbols)
        return self.db.query(f"""SELECT ticker, series_ticker, symbol, open_ms, close_ms, strike_type,
                                        floor_strike, cap_strike FROM markets
                                 WHERE symbol IN ({marks}) AND lower(status) IN ('active','open')
                                   AND close_ms > ? AND (open_ms IS NULL OR open_ms <= ?)
                                 ORDER BY close_ms""", (*self.s.symbols, now, now))

    def evaluate_market(self, m: dict, model_version: str, model, now: int, st) -> Decision:
        fr = frame(m["strike_type"], m["floor_strike"], m["cap_strike"])
        if fr is None:
            return Decision("PASS", "UNSUPPORTED_MARKET", "Market type not supported by the model.")
        strike, flip = fr
        ob = self.db.query_one("SELECT * FROM orderbook_snapshots WHERE ticker=? ORDER BY ts_ms DESC LIMIT 1",
                               (m["ticker"],))
        if not ob or now - ob["ts_ms"] > 20_000:
            return Decision("PASS", "STALE_DATA", "No fresh order book for this market.")
        spot_row = self.db.query_one("SELECT price, received_ms FROM crypto_prices WHERE symbol=? AND provider "
                                     "LIKE 'coinbase%' ORDER BY received_ms DESC LIMIT 1", (m["symbol"],))
        if not spot_row or now - spot_row["received_ms"] > 15_000:
            return Decision("PASS", "STALE_DATA", "No fresh crypto price.")
        closes = live_closes(self.db, m["symbol"], now)
        tau = (m["close_ms"] - now) / 1000
        window = (m["close_ms"] - m["open_ms"]) / 1000 if m["open_ms"] else 900
        bid, ask = above_quotes(ob["best_yes_bid"], ob["best_yes_ask"], flip)
        f = compute_features(spot=spot_row["price"], strike=strike, tau_s=tau, closes_1m=closes + [spot_row["price"]],
                             yes_bid=bid, yes_ask=ask, window_s=window)
        if f is None:
            return Decision("PASS", "NO_FEATURES", "Not enough clean data to form a prediction "
                            "(needs a two-sided market and 10+ minutes of prices).")
        p_above = model.predict(f.vector())
        p_yes = 1 - p_above if flip else p_above
        mult_row = self.db.query_one("SELECT fee_multiplier FROM series_info WHERE series_ticker=?",
                                     (m["series_ticker"],))
        mult = mult_row["fee_multiplier"] if mult_row and mult_row["fee_multiplier"] else 1.0
        d = decide(p_yes, ob["best_yes_bid"], ob["best_yes_ask"], min_edge=self.s.risk.min_edge,
                   max_spread=self.s.risk.max_spread, slippage=self.s.paper_slippage, fee_multiplier=mult)
        if d.action != "BUY":
            return d
        n = size(d.price, max_order_size=self.s.risk.max_order_size,
                 max_market_exposure=self.s.risk.max_market_exposure)
        # only fill what is actually resting at the best price
        levels = json.loads(ob["levels_json"] or "{}")
        opposite = levels.get("no" if d.outcome == "yes" else "yes") or []
        top = max(opposite, key=lambda lv: lv[0], default=None)
        depth = int(top[1]) if top else 0
        n = min(n, depth)
        v = risk.check(st, self.s.risk, ticker=m["ticker"], price=d.price, contracts=n,
                       seconds_to_close=tau, data_age_s=(now - ob["ts_ms"]) / 1000)
        if not v.ok:
            return Decision("PASS", f"RISK_{v.code}", f"Would buy, but risk check said no: {v.message}",
                            d.p_yes, d.outcome, d.price, d.edge, d.market_price)
        self.fill(m, d, n, mult, model_version)
        return d

    def fill(self, m: dict, d: Decision, n: int, mult: float, model_version: str) -> None:
        now = now_ms()
        fee = taker_fee(n, d.price, mult)
        oid = self.db.insert("orders", {
            "client_order_id": f"paper-{uuid.uuid4().hex[:16]}", "exchange_order_id": None, "mode": MODE,
            "ticker": m["ticker"], "side": d.outcome, "action": "buy", "count": n, "limit_price": d.price,
            "status": "filled", "created_ms": now, "updated_ms": now})
        self.db.insert("fills", {"order_id": oid, "mode": MODE, "ts_ms": now, "count": n, "price": d.price,
                                 "fee": fee, "is_taker": 1})
        self.db.upsert("positions", {"mode": MODE, "ticker": m["ticker"], "side": d.outcome, "count": n,
                                     "avg_price": d.price, "fees": fee, "opened_ms": now, "updated_ms": now},
                       ("mode", "ticker", "side"))
        self.db.insert("trades", {
            "mode": MODE, "ticker": m["ticker"], "symbol": m["symbol"], "side": d.outcome, "count": n,
            "entry_ms": now, "entry_price": d.price, "fees": fee, "p_yes_at_entry": d.p_yes,
            "market_price_at_entry": d.market_price, "edge_at_entry": d.edge, "model_version": model_version,
            "reason": d.explanation, "result": "open"})
        self.db.log_event("paper", "info", f"PAPER buy {n} {d.outcome.upper()} {m['ticker']} at "
                          f"{d.price * 100:.0f}¢ (edge {d.edge * 100:+.1f}¢)")

    def step(self) -> dict:
        now = now_ms()
        self.settle()
        markets = self.open_markets(now)
        blocked = self.blocked_reason()
        found = active_model(self.db, self.s.model_version)
        counts = {"markets": len(markets), "buys": 0}
        for m in markets:
            if blocked:
                d = Decision("PASS", blocked[0], blocked[1])
                version = found[0] if found else None
            elif not found:
                d = Decision("PASS", "NO_MODEL", "No approved model yet; waiting for enough history to train.")
                version = None
            else:
                version, model = found
                st = risk.account_state(self.db, MODE, self.s.paper_starting_balance)
                d = self.evaluate_market(m, version, model, now, st)
            counts["buys"] += d.action == "BUY"
            self.record_signal(m["ticker"], d, version, force=d.action == "BUY")
        self.write_equity()
        self.db.heartbeat("paper", {**counts, "model": found[0] if found else None,
                                    "blocked": blocked[0] if blocked else None})
        return counts
