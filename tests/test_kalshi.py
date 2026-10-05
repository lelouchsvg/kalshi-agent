import base64
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi_agent import safety
from kalshi_agent.kalshi.auth import KalshiSigner, KeyFileError, signing_path
from kalshi_agent.kalshi.client import KalshiClient
from kalshi_agent.kalshi.models import Market, Orderbook
from kalshi_agent.modes import TradingMode

DEMO = "https://demo-api.kalshi.co/trade-api/v2"


def _write_key(tmp_path, key, mode=0o600):
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    os.chmod(p, mode)
    return p


def test_signing_path_strips_query_and_host():
    assert signing_path("https://x.com/trade-api/v2/portfolio/orders?limit=5") == "/trade-api/v2/portfolio/orders"
    assert signing_path("/trade-api/v2/markets?status=open") == "/trade-api/v2/markets"


def test_rsa_pss_signature_verifies(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner("kid", _write_key(tmp_path, key))
    h = signer.headers("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/balance?x=1", 1700000000000)
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        b"1700000000000GET/trade-api/v2/portfolio/balance",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256())
    assert "kid" not in repr(signer) or "…" in repr(signer)


def test_ed25519_signature_verifies(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner("kid", _write_key(tmp_path, key))
    h = signer.headers("POST", "/trade-api/v2/portfolio/events/orders", 1)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
                            b"1POST/trade-api/v2/portfolio/events/orders")


def test_world_readable_key_rejected(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(KeyFileError):
        KalshiSigner("kid", _write_key(tmp_path, key, 0o644))


MARKET = {
    "ticker": "KXBTC15M-26OCT050115-15", "event_ticker": "KXBTC15M-26OCT050115", "status": "active",
    "open_time": "2026-10-05T01:00:00Z", "close_time": "2026-10-05T01:15:00Z",
    "yes_bid_dollars": "0.4800", "yes_ask_dollars": "0.5100", "no_bid_dollars": "0.4900",
    "no_ask_dollars": "0.5200", "last_price_dollars": "0.5000", "volume_fp": "1234.00",
    "open_interest_fp": "800.00", "floor_strike": 62000.5, "strike_type": "greater",
}


def test_market_parses_dollar_fields():
    m = Market.from_api(MARKET)
    assert m.yes_bid == 0.48 and m.yes_ask == 0.51 and m.spread == 0.03
    assert m.volume == 1234.0 and m.floor_strike == 62000.5
    assert m.close_ms - m.open_ms == 15 * 60 * 1000
    assert m.seconds_to_close(m.open_ms) == 900
    assert m.is_tradeable_status


def test_market_legacy_cent_fallback():
    m = Market.from_api({"ticker": "T", "yes_bid": 45, "yes_ask": 47, "volume": 10})
    assert m.yes_bid == 0.45 and m.yes_ask == 0.47 and m.volume == 10


def test_orderbook_bids_only_derives_asks():
    ob = Orderbook.from_api("T", {"orderbook_fp": {
        "yes_dollars": [["0.4500", "100.00"], ["0.4700", "20.00"]],
        "no_dollars": [["0.5000", "50.00"], ["0.5100", "30.00"]]}})
    assert ob.best_yes_bid == 0.47
    assert ob.best_yes_ask == 0.49  # 1 - best NO bid 0.51
    assert ob.best_no_ask == 0.53
    assert ob.yes_ask_levels()[0] == (0.49, 30.0)
    assert ob.imbalance == pytest.approx((120 - 80) / 200)


def test_orderbook_legacy_and_empty():
    ob = Orderbook.from_api("T", {"orderbook": {"yes": [[40, 5]], "no": None}})
    assert ob.best_yes_bid == 0.40 and ob.best_yes_ask is None and ob.imbalance == 1.0
    assert Orderbook.from_api("T", {"orderbook_fp": {}}).imbalance is None


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.content = b"x"
        self.text = str(body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


def test_pagination_and_series_filter():
    sess = FakeSession([FakeResp(200, {"markets": [MARKET], "cursor": "c2"}),
                        FakeResp(200, {"markets": [dict(MARKET, ticker="B")], "cursor": ""})])
    c = KalshiClient(DEMO, session=sess, requests_per_second=1000)
    ms = c.get_markets(series_ticker="KXBTC15M", status="open")
    assert [m.ticker for m in ms] == [MARKET["ticker"], "B"]
    params = sess.calls[0][2]["params"]
    assert params["series_ticker"] == "KXBTC15M" and params["mve_filter"] == "exclude"
    assert "cursor" not in params and sess.calls[1][2]["params"]["cursor"] == "c2"


def test_retries_on_429(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    sess = FakeSession([FakeResp(429, {}), FakeResp(200, {"trading_active": True})])
    c = KalshiClient(DEMO, session=sess, requests_per_second=1000)
    assert c.get_exchange_status()["trading_active"] is True


def test_v2_order_body_for_no_side(tmp_path, monkeypatch):
    monkeypatch.setattr(safety, "DEMO_TRADING_UNLOCKED", True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sess = FakeSession([FakeResp(201, {"order_id": "o1"})])
    c = KalshiClient(DEMO, signer=KalshiSigner("kid", _write_key(tmp_path, key)), session=sess,
                     requests_per_second=1000)
    gate = safety.GateResult(True, TradingMode.DEMO, {}, [])
    permit = safety.authorize_order(gate, DEMO)
    c.create_order(permit, ticker="T", outcome="no", count=3, limit_price=0.40, client_order_id="cid")
    method, url, kw = sess.calls[0]
    assert method == "POST" and url.endswith("/portfolio/events/orders")
    assert kw["json"]["side"] == "ask" and kw["json"]["price"] == "0.6000" and kw["json"]["count"] == "3.00"
    assert "KALSHI-ACCESS-SIGNATURE" in kw["headers"]


def test_demo_permit_cannot_hit_prod(monkeypatch):
    monkeypatch.setattr(safety, "DEMO_TRADING_UNLOCKED", True)
    gate = safety.GateResult(True, TradingMode.DEMO, {}, [])
    with pytest.raises(PermissionError):
        safety.authorize_order(gate, "https://api.elections.kalshi.com/trade-api/v2")
    permit = safety.authorize_order(gate, DEMO)
    with pytest.raises(PermissionError):
        KalshiClient("https://api.elections.kalshi.com/trade-api/v2").create_order(
            permit, ticker="T", outcome="yes", count=1, limit_price=0.5, client_order_id="x")
