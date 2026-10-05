import json

import pytest

from kalshi_agent import safety
from kalshi_agent.config import KALSHI_BASE_URLS
from kalshi_agent.db import now_ms
from kalshi_agent.demo import DemoMirror, parse_order_result
from kalshi_agent.kalshi.client import KalshiAPIError, KalshiClient


class FakeDemo(KalshiClient):
    """The real client (permit checks, order body) with the network replaced by a fake demo exchange."""

    def __init__(self, base=KALSHI_BASE_URLS["demo"]):
        super().__init__(base, requests_per_second=1000)
        self.calls, self.no_bid, self.yes_bid, self.result, self.fill = [], 0.55, 0.40, None, "2.00"
        self.missing = set()

    def _request(self, method, path, *, params=None, json_body=None, auth=False):
        self.calls.append((method, path, json_body))
        ticker = path.split("/")[2] if path.startswith("/markets/") else None
        if ticker in self.missing:
            raise KalshiAPIError(404, "not found", path)
        if path.endswith("/orderbook"):
            return {"orderbook_fp": {"yes_dollars": [[f"{self.yes_bid:.2f}", "10"]],
                                     "no_dollars": [[f"{self.no_bid:.2f}", "10"]]}}
        if path == "/portfolio/events/orders":
            return {"order": {"order_id": "ord-1", "status": "executed", "fill_count_fp": self.fill,
                              "taker_fill_cost_dollars": f"{float(self.fill) * 0.45:.4f}",
                              "taker_fees_dollars": "0.02"}}
        if path.startswith("/markets/"):
            return {"market": {"ticker": ticker, "status": "finalized" if self.result else "active",
                               "result": self.result or ""}}
        if path == "/portfolio/balance":
            return {"balance": 100000}
        if path == "/portfolio/positions":
            return {"market_positions": [{"ticker": "KXBTC15M-T1", "position_fp": "2.00"}]}
        raise AssertionError(f"unexpected call {method} {path}")


@pytest.fixture
def mirror(settings, db):
    settings.kalshi_demo_api_key_id, settings.kalshi_demo_private_key_path = "demo-key", "/x"
    fake = FakeDemo()
    db.upsert("markets", {"ticker": "KXBTC15M-T1", "symbol": "BTC", "series_ticker": "KXBTC15M",
                          "close_ms": now_ms() + 600_000, "status": "active",
                          "first_seen_ms": now_ms(), "updated_ms": now_ms()}, "ticker")
    return DemoMirror(db, settings, client=fake), fake


def paper_trade(db, ticker="KXBTC15M-T1", side="yes", price=0.44, count=5):
    return db.insert("trades", {"mode": "PAPER", "ticker": ticker, "symbol": "BTC", "side": side, "count": count,
                                "entry_ms": now_ms(), "entry_price": price, "fees": 0.07, "p_yes_at_entry": 0.6,
                                "market_price_at_entry": 0.44, "edge_at_entry": 0.08, "model_version": "MODEL_V001",
                                "reason": "test", "result": "open"})


def test_mirrors_paper_trade_on_demo_and_settles(mirror, db):
    m, fake = mirror
    pid = paper_trade(db)
    out = m.step()
    assert out["active"] and out["sent"] == 1
    order_call = [c for c in fake.calls if c[1] == "/portfolio/events/orders"][0]
    body = order_call[2]
    assert body["side"] == "bid" and body["count"] == "2.00"            # capped at demo max_contracts
    assert body["price"] == "0.4600" and body["client_order_id"] == f"demo-p{pid}"
    assert body["time_in_force"] == "immediate_or_cancel"
    t = db.query_one("SELECT * FROM trades WHERE mode='DEMO'")
    assert t["count"] == 2 and abs(t["entry_price"] - 0.45) < 1e-9 and t["fees"] == 0.02
    assert db.query_one("SELECT status FROM orders WHERE mode='DEMO'")["status"] == "filled"
    assert json.loads(db.get_control("demo_balance"))["balance"] == 1000.0
    # same paper trade is never sent twice
    assert m.step()["sent"] == 0
    # market settles YES on the demo exchange
    db.execute("UPDATE markets SET close_ms=? WHERE ticker='KXBTC15M-T1'", (now_ms() - 1000,))
    fake.result = "yes"
    assert m.step()["settled"] == 1
    t = db.query_one("SELECT * FROM trades WHERE mode='DEMO'")
    assert t["result"] == "win" and abs(t["pnl"] - (2 * (1 - 0.45) - 0.02)) < 1e-9


def test_skips_when_demo_price_is_far_or_market_missing(mirror, db):
    m, fake = mirror
    fake.no_bid = 0.40          # YES ask 60¢ vs paper 44¢
    paper_trade(db)
    m.step()
    o = db.query_one("SELECT * FROM orders WHERE mode='DEMO'")
    assert o["status"] == "skipped" and "too far" in o["reject_reason"]
    fake.missing.add("KXBTC15M-T2")
    paper_trade(db, ticker="KXBTC15M-T2")
    m.step()
    assert "doesn't exist" in db.query_one("SELECT reject_reason FROM orders WHERE ticker='KXBTC15M-T2'")[
        "reject_reason"]
    assert not [c for c in fake.calls if c[1] == "/portfolio/events/orders"]


def test_no_side_and_no_fill(mirror, db):
    m, fake = mirror
    fake.fill = "0"
    paper_trade(db, side="no", price=0.58)         # NO ask = 1 - yes bid 0.40 = 0.60 <= 0.60 limit
    m.step()
    body = [c for c in fake.calls if c[1] == "/portfolio/events/orders"][0][2]
    assert body["side"] == "ask" and body["price"] == "0.4000"   # buying NO at 60¢ = YES ask at 40¢
    assert db.query_one("SELECT status FROM orders WHERE mode='DEMO'")["status"] == "canceled"
    assert db.query_one("SELECT COUNT(*) AS n FROM trades WHERE mode='DEMO'")["n"] == 0


def test_gates_block_demo_orders(mirror, db, settings):
    m, fake = mirror
    safety.engage_kill(db, "test", "stop")
    paper_trade(db)
    m.step()
    assert not [c for c in fake.calls if c[1] == "/portfolio/events/orders"]
    assert "safety gate" in m.note
    settings.kalshi_demo_api_key_id = None
    assert m.step() == {"active": False, "note": "needs a Kalshi demo account key (add it on the dashboard)"}


def test_mirror_cannot_reach_the_real_exchange(settings, db):
    settings.kalshi_demo_api_key_id, settings.kalshi_demo_private_key_path = "demo-key", "/x"
    m = DemoMirror(db, settings, client=FakeDemo(KALSHI_BASE_URLS["prod"]))
    with pytest.raises(PermissionError):
        m.permit()


def test_parse_order_result_variants():
    r = parse_order_result({"order": {"order_id": "a", "status": "executed", "fill_count": 3,
                                      "taker_fill_cost": 135, "taker_fees": 6}})
    assert r["filled"] == 3 and abs(r["avg_price"] - 0.45) < 1e-9 and r["fees"] == 0.06
    assert parse_order_result({"order_id": "b"})["filled"] == 0
