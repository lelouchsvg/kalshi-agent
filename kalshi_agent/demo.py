"""Phase 7: mirror paper trades onto Kalshi's DEMO exchange (fake money).

Paper trading keeps running exactly as before on real prices. When it opens a paper
position, this sends a small real order for the same market to demo-api.kalshi.co
using a separate demo account, so the whole order path (signing, order format,
fills, fees, settlement, reconciliation) is exercised before real money is ever
considered. It can only talk to the demo host: the client is built from the demo
URL, the permit is issued for that URL, and safety.verify_permit rejects anything else.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from .config import KALSHI_BASE_URLS
from .db import Database, now_ms
from .kalshi.client import KalshiAPIError, KalshiClient
from .kalshi.models import to_float
from .modes import TradingMode
from .risk import start_of_today_ms
from .safety import DEMO_TRADING_UNLOCKED, authorize_order, evaluate_mode, kill_status

log = logging.getLogger("demo")
MODE = "DEMO"
DEMO_URL = KALSHI_BASE_URLS["demo"]


def _num(d: dict, *names: str) -> float | None:
    for n in names:
        v = to_float(d.get(n))
        if v is not None:
            return v
    return None


def parse_order_result(resp: dict) -> dict[str, Any]:
    """Pull what we need out of an order response, tolerating field-name variants."""
    o = resp.get("order") if isinstance(resp.get("order"), dict) else resp
    filled = _num(o, "fill_count_fp", "fill_count", "filled_count") or 0.0
    cost = _num(o, "taker_fill_cost_dollars", "fill_cost_dollars")
    if cost is None:
        cents = _num(o, "taker_fill_cost", "fill_cost")
        cost = cents / 100 if cents is not None else None
    fees = _num(o, "taker_fees_dollars", "fees_dollars")
    if fees is None:
        cents = _num(o, "taker_fees", "fees")
        fees = cents / 100 if cents is not None else 0.0
    return {"order_id": o.get("order_id") or o.get("id"), "status": str(o.get("status") or "").lower(),
            "filled": filled, "avg_price": (cost / filled) if cost and filled else None, "fees": fees}


class DemoMirror:
    def __init__(self, db: Database, settings, health_ok: Callable[[], bool] = lambda: True,
                 client: KalshiClient | None = None):
        self.db = db
        self.s = settings
        self.health_ok = health_ok
        self._client = client
        self._last_sync = 0
        self.note = ""

    # -- setup -----------------------------------------------------------------
    def why_off(self) -> str | None:
        if not DEMO_TRADING_UNLOCKED:
            return "locked in code"
        if not self.s.demo_enabled:
            return "turned off in settings"
        if not self.s.has_demo_credentials:
            return "needs a Kalshi demo account key (add it on the dashboard)"
        return None

    @property
    def client(self) -> KalshiClient:
        if self._client is None:
            from .kalshi.auth import KalshiSigner
            signer = KalshiSigner(self.s.kalshi_demo_api_key_id, self.s.kalshi_demo_private_key_path)
            self._client = KalshiClient(DEMO_URL, signer=signer, requests_per_second=2)
        return self._client

    def permit(self):
        ks = kill_status(self.db, self.s.kill_switch, self.s.kill_file)
        gate = evaluate_mode(TradingMode.DEMO, config_mode=TradingMode.DEMO, kalshi_env="demo",
                             has_credentials=self.s.has_demo_credentials, db=self.db,
                             killed=ks.killed, health_ok=self.health_ok())
        return authorize_order(gate, self.client.base_url)   # raises if any gate fails

    # -- the loop --------------------------------------------------------------
    def step(self) -> dict[str, Any]:
        off = self.why_off()
        if off:
            self.db.heartbeat("demo", {"active": False, "note": off})
            return {"active": False, "note": off}
        out = {"active": True, "sent": 0, "settled": 0}
        try:
            out["settled"] = self.settle()
            for t in self.pending_paper_trades():
                out["sent"] += self.mirror(t)
            if now_ms() - self._last_sync > 300_000:
                self.sync_account()
        except PermissionError as exc:
            self.note = f"blocked by a safety gate: {exc}"
        except KalshiAPIError as exc:
            self.note = f"demo exchange error: {exc}"[:200]
            log.warning("Demo exchange error: %s", exc)
        self.db.heartbeat("demo", {"active": True, "note": self.note, **out,
                                   "balance": self._balance()})
        return out

    def pending_paper_trades(self) -> list[dict]:
        return self.db.query("""SELECT t.* FROM trades t WHERE t.mode='PAPER' AND t.pnl IS NULL
                                AND t.entry_ms > ? AND NOT EXISTS (SELECT 1 FROM orders o
                                    WHERE o.client_order_id = 'demo-p' || t.id)
                                ORDER BY t.entry_ms""", (now_ms() - 120_000,))

    def orders_today(self) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM orders WHERE mode=? AND created_ms >= ?",
                                 (MODE, start_of_today_ms()))["n"]

    def _record_skip(self, t: dict, reason: str) -> int:
        now = now_ms()
        self.db.insert("orders", {"client_order_id": f"demo-p{t['id']}", "mode": MODE, "ticker": t["ticker"],
                                  "side": t["side"], "action": "buy", "count": 0, "limit_price": 0,
                                  "status": "skipped", "created_ms": now, "updated_ms": now,
                                  "reject_reason": reason})
        self.db.log_event("demo", "info", f"DEMO skipped {t['ticker']}: {reason}")
        return 0

    def mirror(self, t: dict) -> int:
        if self.orders_today() >= self.s.demo_max_orders_per_day:
            return self._record_skip(t, "daily demo order limit reached")
        try:
            book = self.client.get_orderbook(t["ticker"], depth=5)
        except KalshiAPIError as exc:
            if exc.status == 404:
                return self._record_skip(t, "this market doesn't exist on the demo exchange")
            raise
        ask = book.best_yes_ask if t["side"] == "yes" else (
            None if book.best_yes_bid is None else round(1 - book.best_yes_bid, 4))
        limit = round(min(0.99, t["entry_price"] + self.s.demo_max_price_gap), 2)
        if ask is None:
            return self._record_skip(t, "no sellers on the demo exchange")
        if ask > limit:
            return self._record_skip(t, f"demo price {ask * 100:.0f}¢ is too far from paper's "
                                        f"{t['entry_price'] * 100:.0f}¢")
        count = min(int(t["count"]), self.s.demo_max_contracts)
        permit = self.permit()
        now = now_ms()
        cid = f"demo-p{t['id']}"
        oid = self.db.insert("orders", {"client_order_id": cid, "mode": MODE, "ticker": t["ticker"],
                                        "side": t["side"], "action": "buy", "count": count,
                                        "limit_price": limit, "status": "pending", "created_ms": now,
                                        "updated_ms": now})
        try:
            resp = self.client.create_order(permit, ticker=t["ticker"], outcome=t["side"], count=count,
                                            limit_price=limit, client_order_id=cid)
        except KalshiAPIError as exc:
            self.db.execute("UPDATE orders SET status='rejected', reject_reason=?, updated_ms=? WHERE id=?",
                            (str(exc)[:300], now_ms(), oid))
            self.db.log_event("demo", "warning", f"DEMO order rejected for {t['ticker']}: {exc}"[:300])
            return 0
        r = parse_order_result(resp)
        filled = int(r["filled"])
        status = "filled" if filled >= count else "partial" if filled else "canceled"
        self.db.execute("UPDATE orders SET exchange_order_id=?, status=?, updated_ms=? WHERE id=?",
                        (r["order_id"], status, now_ms(), oid))
        if not filled:
            self.db.log_event("demo", "info", f"DEMO order for {t['ticker']} got no fill "
                              f"(exchange said {r['status'] or 'nothing'})",
                              {"response": json.dumps(resp)[:1500]})
            return 1
        price = r["avg_price"] or limit
        self.db.insert("fills", {"order_id": oid, "mode": MODE, "ts_ms": now_ms(), "count": filled,
                                 "price": price, "fee": r["fees"], "is_taker": 1})
        self.db.upsert("positions", {"mode": MODE, "ticker": t["ticker"], "side": t["side"], "count": filled,
                                     "avg_price": price, "fees": r["fees"], "opened_ms": now,
                                     "updated_ms": now}, ("mode", "ticker", "side"))
        self.db.insert("trades", {
            "mode": MODE, "ticker": t["ticker"], "symbol": t["symbol"], "side": t["side"], "count": filled,
            "entry_ms": now, "entry_price": price, "fees": r["fees"], "p_yes_at_entry": t["p_yes_at_entry"],
            "market_price_at_entry": t["market_price_at_entry"], "edge_at_entry": t["edge_at_entry"],
            "model_version": t["model_version"],
            "reason": f"Mirror of paper trade #{t['id']} (paper paid {t['entry_price'] * 100:.0f}¢)",
            "result": "open"})
        self.db.log_event("demo", "info", f"DEMO bought {filled} {t['side'].upper()} {t['ticker']} at "
                          f"{price * 100:.0f}¢ (paper paid {t['entry_price'] * 100:.0f}¢)")
        return 1

    def settle(self) -> int:
        closed = 0
        for t in self.db.query("""SELECT t.id, t.ticker, t.side, t.count, t.entry_price, t.fees, m.close_ms
                                  FROM trades t LEFT JOIN markets m ON m.ticker = t.ticker
                                  WHERE t.mode=? AND t.pnl IS NULL""", (MODE,)):
            if t["close_ms"] and t["close_ms"] > now_ms():
                continue
            result = (self.client.get_market(t["ticker"]).result or "").lower()
            if result not in ("yes", "no"):
                continue
            won = t["side"] == result
            exit_price = 1.0 if won else 0.0
            pnl = t["count"] * (exit_price - t["entry_price"]) - (t["fees"] or 0)
            self.db.execute("UPDATE trades SET exit_ms=?, exit_price=?, pnl=?, result=? WHERE id=?",
                            (now_ms(), exit_price, pnl, "win" if won else "loss", t["id"]))
            self.db.execute("DELETE FROM positions WHERE mode=? AND ticker=?", (MODE, t["ticker"]))
            self.db.log_event("demo", "info", f"DEMO trade settled {result.upper()}: "
                              f"{'won' if won else 'lost'} ${pnl:+.2f}")
            closed += 1
        return closed

    def sync_account(self) -> None:
        """Balance for the dashboard, plus a check that Kalshi's positions match ours."""
        self._last_sync = now_ms()
        bal = self.client.get_balance()
        cents = _num(bal, "balance")
        dollars = _num(bal, "balance_dollars")
        balance = dollars if dollars is not None else (cents / 100 if cents is not None else None)
        self.db.set_control("demo_balance", json.dumps({"balance": balance, "ts_ms": now_ms()}), "demo")
        if balance is not None:
            self.db.insert("pnl", {"mode": MODE, "ts_ms": now_ms(), "equity": balance, "realized": 0,
                                   "unrealized": 0, "fees": 0})
        ours = {r["ticker"] for r in self.db.query(
            "SELECT t.ticker FROM trades t JOIN markets m ON m.ticker = t.ticker "
            "WHERE t.mode=? AND t.pnl IS NULL AND m.close_ms > ?", (MODE, now_ms()))}
        pos = self.client.get_positions(count_filter="position")
        theirs = {p.get("ticker") for p in pos.get("market_positions") or []
                  if (_num(p, "position_fp", "position") or 0) != 0}
        missing = ours - theirs
        if missing:
            self.db.log_event("demo", "warning",
                              f"Demo account doesn't show positions we recorded: {', '.join(sorted(missing))}")

    def _balance(self) -> float | None:
        try:
            return json.loads(self.db.get_control("demo_balance") or "{}").get("balance")
        except ValueError:
            return None
