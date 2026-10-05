import json
import socket
import threading
import time

import pytest

from kalshi_agent.stream.coinbase import CoinbaseStream
from kalshi_agent.stream.kalshi_ws import KalshiStream, LocalBook
from kalshi_agent.stream.ws import WebSocket, WebSocketClosed, WebSocketError
from wsserver import WSServer, frame, read_client_frame


def test_websocket_roundtrip_fragments_ping_and_close():
    def script(conn, srv):
        op, data = read_client_frame(conn)
        srv.received.append(data.decode())
        conn.sendall(frame(b'{"a": 1}'))
        conn.sendall(frame(b"hel", fin=False) + frame(b"lo", op=0))   # fragmented message
        conn.sendall(frame(b"pp", op=9))                              # ping, expect pong
        op, data = read_client_frame(conn)
        srv.received.append((op, data))
        big = json.dumps({"x": "y" * 70000}).encode()
        conn.sendall(frame(big))
        conn.sendall(frame(b"\x03\xe8", op=8))                        # close
        time.sleep(0.2)

    srv = WSServer(script)
    ws = WebSocket(srv.url, headers={"X-Test": "1"}, read_timeout=2).connect()
    ws.send("hello server")
    assert json.loads(ws.recv()) == {"a": 1}
    assert ws.recv() == "hello"
    assert len(json.loads(ws.recv())["x"]) == 70000
    with pytest.raises(WebSocketClosed):
        ws.recv()
    assert srv.received[0] == "hello server"
    assert srv.received[1] == (0xA, b"pp")
    assert srv.requests[0]["x-test"] == "1"
    srv.close()


def test_websocket_partial_frame_survives_timeout():
    def script(conn, srv):
        msg = frame(b'{"split": true}')
        conn.sendall(msg[:5])
        time.sleep(0.8)
        conn.sendall(msg[5:])
        time.sleep(0.5)

    srv = WSServer(script)
    ws = WebSocket(srv.url, read_timeout=0.2).connect()
    with pytest.raises(socket.timeout):
        ws.recv()                       # only half a frame has arrived
    msg = None
    for _ in range(50):                 # keep reading; slow machines may time out more than once
        try:
            msg = ws.recv()
            break
        except socket.timeout:
            continue
    assert json.loads(msg) == {"split": True}
    ws.close()
    srv.close()


def test_websocket_rejects_bad_handshake():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)

    def serve():
        c, _ = s.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        c.close()
    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(WebSocketError):
        WebSocket(f"ws://127.0.0.1:{s.getsockname()[1]}/").connect()
    s.close()


def _tick(product, price, t="2026-10-05T12:00:00.123456Z"):
    return json.dumps({"type": "ticker", "product_id": product, "price": str(price), "best_bid": str(price - 1),
                       "best_ask": str(price + 1), "volume_24h": "100", "time": t}).encode()


def test_coinbase_stream_subscribes_and_stores_one_row_per_second(db):
    def script(conn, srv):
        op, data = read_client_frame(conn)
        srv.received.append(json.loads(data))
        for i in range(20):
            conn.sendall(frame(_tick("BTC-USD", 60000 + i)))
        conn.sendall(frame(_tick("ETH-USD", 2500)))
        conn.sendall(frame(_tick("DOGE-USD", 1)))      # not subscribed: ignored
        conn.sendall(frame(b"not json"))
        time.sleep(3)

    srv = WSServer(script)
    stop = threading.Event()
    feed = CoinbaseStream(db, stop, ["BTC", "ETH"])
    feed.URL = srv.url
    t = threading.Thread(target=lambda: _run_briefly(feed, stop, 2.0))
    t.start()
    t.join(10)
    sub = srv.received[0]
    assert sub["type"] == "subscribe" and set(sub["product_ids"]) == {"BTC-USD", "ETH-USD"}
    rows = db.query("SELECT symbol, price, ts_ms, received_ms, provider FROM crypto_prices ORDER BY id")
    btc = [r for r in rows if r["symbol"] == "BTC"]
    assert 1 <= len(btc) <= 3                       # throttled, not 20 rows
    assert btc[-1]["price"] == 60019                # latest tick kept
    assert all(r["provider"] == "coinbase_ws" for r in rows)
    assert all(r["received_ms"] >= r["ts_ms"] - 10**12 for r in rows)
    assert {r["symbol"] for r in rows} == {"BTC", "ETH"}
    assert feed.fresh_price("BTC").price == 60019
    hb = json.loads(db.query_one("SELECT info_json FROM heartbeats WHERE component='feed:coinbase_ws'")["info_json"])
    assert hb["connects"] == 1
    srv.close()


def _run_briefly(feed, stop, seconds):
    threading.Timer(seconds, stop.set).start()
    try:
        feed.run_once()
    except Exception:
        pass


def test_paused_feed_drops_messages(db):
    def script(conn, srv):
        read_client_frame(conn)
        conn.sendall(frame(_tick("BTC-USD", 1)))
        time.sleep(1.2)

    srv = WSServer(script)
    stop = threading.Event()
    feed = CoinbaseStream(db, stop, ["BTC"], paused_fn=lambda: True)
    feed.URL = srv.url
    _run_briefly(feed, stop, 1.0)
    assert db.query_one("SELECT COUNT(*) AS n FROM crypto_prices")["n"] == 0
    srv.close()


def test_stream_reconnects_after_drop(db):
    def script(conn, srv):
        read_client_frame(conn)
        conn.sendall(frame(_tick("BTC-USD", 5)))
        time.sleep(0.1)          # then hang up

    srv = WSServer(script)
    stop = threading.Event()
    feed = CoinbaseStream(db, stop, ["BTC"])
    feed.URL = srv.url
    feed.stop_event.wait = lambda s: stop.is_set()     # no backoff sleeping in tests
    threading.Timer(3.0, stop.set).start()
    feed.run()
    assert feed.stats.connects >= 2
    srv.close()


# --- Kalshi stream (needs keys in real life; exercised with recorded-format messages) ---

def test_local_book_snapshot_and_deltas():
    b = LocalBook()
    b.snapshot({"yes_dollars_fp": [["0.4500", "100.00"], ["0.4400", "50.00"]], "no_dollars_fp": [["0.5300", "70.00"]]})
    b.delta({"side": "yes", "price_dollars": "0.4500", "delta_fp": "-100.00"})
    b.delta({"side": "no", "price_dollars": "0.5400", "delta_fp": "10.00"})
    ob = b.to_orderbook("T")
    assert ob.best_yes_bid == 0.44
    assert ob.best_yes_ask == round(1 - 0.54, 4)
    legacy = LocalBook()
    legacy.snapshot({"yes": [[45, 100]], "no": [[53, 70]]})
    assert legacy.to_orderbook("T").best_yes_bid == 0.45


class _Signer:
    def headers(self, method, path):
        assert path == "/trade-api/ws/v2"
        return {"KALSHI-ACCESS-KEY": "k", "KALSHI-ACCESS-SIGNATURE": "s", "KALSHI-ACCESS-TIMESTAMP": "1"}


def test_kalshi_stream_messages_and_sequence_gap(db):
    feed = KalshiStream(db, threading.Event(), "ws://x", _Signer(), tickers_fn=lambda: ["T1"])
    feed.on_message({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {
        "market_ticker": "T1", "yes_dollars_fp": [["0.40", "10"]], "no_dollars_fp": [["0.55", "5"]]}})
    feed.on_message({"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {
        "market_ticker": "T1", "side": "yes", "price_dollars": "0.41", "delta_fp": "3"}})
    feed.on_message({"type": "ticker", "msg": {"market_ticker": "T1", "yes_bid_dollars": "0.41",
                                              "yes_ask_dollars": "0.45", "price_dollars": "0.43", "ts": 1}})
    feed.on_message({"type": "ticker", "msg": {"market_ticker": "T1", "yes_bid_dollars": "0.42",
                                              "yes_ask_dollars": "0.45", "ts": 1}})  # same second: not stored
    assert db.query_one("SELECT COUNT(*) AS n FROM market_snapshots WHERE source='ws'")["n"] == 1
    feed.on_message({"type": "trade", "msg": {"market_ticker": "T1", "trade_id": "abc", "yes_price_dollars": "0.43",
                                             "count_fp": "2", "taker_side": "yes", "ts": 1}})
    feed.on_message({"type": "trade", "msg": {"market_ticker": "T1", "trade_id": "abc", "ts": 1}})  # duplicate
    feed.subscribed = frozenset(["T1"])
    feed.on_idle()
    ob = db.query_one("SELECT * FROM orderbook_snapshots")
    assert ob["best_yes_bid"] == 0.41 and ob["best_yes_ask"] == 0.45
    snap = db.query_one("SELECT * FROM market_snapshots WHERE source='ws'")
    assert snap["spread"] == 0.04
    assert db.query_one("SELECT COUNT(*) AS n FROM kalshi_trades")["n"] == 1
    with pytest.raises(WebSocketError):
        feed.on_message({"type": "orderbook_delta", "sid": 1, "seq": 5, "msg": {"market_ticker": "T1"}})
    assert feed.headers()["KALSHI-ACCESS-KEY"] == "k"
