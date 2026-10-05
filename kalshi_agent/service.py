"""The always-on collector.

Phase 5: paper trades with fake money against live order books (never real orders).
Phase 2: streams real-time crypto prices, computes a stand-in for the settlement
index, records Kalshi markets, order books and public trades, checks the Mac's
clock, and downloads recent history in the background. It contains no trading
logic that can reach Kalshi's order endpoints, so it can never place a real order.

Threads:
  main loop      discovery, REST snapshots, trades, clock, health, maintenance
  coinbase_ws    real-time BTC/ETH/SOL prices (free, no account)
  index_proxy    median of four exchanges + 60-second average (settlement stand-in)
  backfill       settled markets and one-minute candles from the past N days
  kalshi_ws      real-time Kalshi books; only when an API key is configured
  trainer        retrains and validates the model every few hours
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time

from .backfill import Backfill
from .clock import ClockSync, measure
from .config import Settings, load_settings
from .crypto.index import IndexProxy
from .crypto.provider import get_provider
from .db import Database, now_ms, open_db
from .discovery import discover, record_trades, refresh_markets
from .health import run_health
from .kalshi.client import KalshiAPIError, KalshiClient, build_client
from .logging_setup import setup_logging
from .safety import evaluate_mode, kill_status
from .demo import DemoMirror
from .paper import PaperTrader
from .stream.coinbase import CoinbaseStream
from .trainer import Trainer

log = logging.getLogger("collector")


class Collector:
    def __init__(self, settings: Settings, db: Database, client: KalshiClient | None = None,
                 provider=None):
        self.s = settings
        self.db = db
        self.client = client or build_client(settings)
        self.provider = provider or get_provider(settings.crypto_provider)
        self.series_map = dict(settings.series)
        self.active: list[str] = []
        self.tradeable: list[str] = []
        self._next = {k: 0.0 for k in ("discovery", "snapshot", "crypto", "trades", "clock",
                                       "health", "maintenance", "decide")}
        self.paper = PaperTrader(db, settings)
        self.demo = DemoMirror(db, settings, health_ok=lambda: self._health_ok)
        self._stop_event = threading.Event()
        self._health_ok = False
        self.clock = ClockSync(db)
        self.coinbase: CoinbaseStream | None = None
        self.threads: list[threading.Thread] = []

    def stop(self, *_):
        self._stop_event.set()

    def paused(self) -> bool:
        return self.db.get_control("collector", "running") == "paused"

    # -- background feeds ---------------------------------------------------------
    def start_feeds(self) -> None:
        if self.s.coinbase_stream:
            self.coinbase = CoinbaseStream(self.db, self._stop_event, self.s.symbols,
                                           clock_offset_ms=self.clock.current, paused_fn=self.paused)
            self.threads.append(self.coinbase)
        self.threads.append(IndexProxy(
            self.db, self._stop_event, self.s.symbols, self.s.index_interval_s,
            coinbase_live=self.coinbase.fresh_price if self.coinbase else None, paused_fn=self.paused))
        if self.s.backfill_days > 0:
            slow_client = KalshiClient(self.s.kalshi_base_url, requests_per_second=2)
            self.threads.append(Backfill(self.db, slow_client, self._stop_event, self.s.symbols,
                                         self.series_map, days=self.s.backfill_days,
                                         paused_fn=self.paused))
        self.threads.append(Trainer(self.db, self.s, self._stop_event, paused_fn=self.paused))
        kalshi_note = self._kalshi_stream()
        self.db.heartbeat("feed:kalshi_ws", {"connected": False, "note": kalshi_note})
        for t in self.threads:
            t.start()

    def _kalshi_stream(self) -> str:
        if not self.s.kalshi_stream:
            return "turned off in settings"
        if not self.s.has_kalshi_credentials:
            return "needs a Kalshi API key (optional); polling Kalshi every few seconds instead"
        try:
            from .kalshi.auth import KalshiSigner
            from .stream.kalshi_ws import KalshiStream
            signer = KalshiSigner(self.s.kalshi_api_key_id, self.s.kalshi_private_key_path)
        except Exception as exc:
            self.db.log_event("collector", "warning", f"Kalshi stream not started: {exc}")
            return f"could not load the API key: {exc}"[:200]
        self.threads.append(KalshiStream(self.db, self._stop_event, self.s.kalshi_ws_url, signer,
                                         tickers_fn=lambda: list(self.tradeable),
                                         depth=self.s.orderbook_depth, paused_fn=self.paused))
        return "starting"

    # -- main loop tasks ----------------------------------------------------------
    def update_mode(self) -> None:
        ks = kill_status(self.db, self.s.kill_switch, self.s.kill_file)
        gate = evaluate_mode(self.s.trading_mode, config_mode=self.s.trading_mode,
                             kalshi_env=self.s.kalshi_env, has_credentials=self.s.has_kalshi_credentials,
                             db=self.db, killed=ks.killed, health_ok=self._health_ok)
        state = {"requested": self.s.trading_mode.value, "effective": gate.effective_mode.value,
                 "blocked_by": gate.reasons, "killed": ks.killed, "kill_sources": ks.sources}
        if self.db.get_control("mode_state") != json.dumps(state):
            if gate.reasons:
                self.db.log_risk("critical", "MODE_DOWNGRADED",
                                 f"{self.s.trading_mode.value} requested but blocked; running WATCH",
                                 details=state)
            self.db.set_control("mode_state", json.dumps(state), "collector")

    def poll_crypto(self) -> None:
        """REST prices, used only when the stream has nothing fresh for a symbol."""
        for sym in self.s.symbols:
            if self.coinbase and self.coinbase.fresh_price(sym):
                continue
            try:
                tick = self.provider.latest(sym)
                self.db.insert("crypto_prices", {
                    "symbol": sym, "ts_ms": tick.ts_ms, "received_ms": tick.received_ms,
                    "price": tick.price, "bid": tick.bid, "ask": tick.ask,
                    "volume_24h": tick.volume_24h, "provider": tick.provider})
            except Exception as exc:
                log.warning("Crypto price %s failed: %s", sym, exc)

    def poll_trades(self) -> int:
        added = 0
        for ticker in self.tradeable:
            last = self.db.query_one("SELECT MAX(ts_ms) AS t FROM kalshi_trades WHERE ticker=?", (ticker,))
            try:
                added += record_trades(self.client, self.db, ticker, last["t"] if last else None)
            except KalshiAPIError as exc:
                log.warning("Trades %s failed: %s", ticker, exc)
        self.db.heartbeat("trades", {"added": added, "markets": len(self.tradeable)})
        return added

    def check_clock(self) -> None:
        try:
            reading = measure(kalshi_base_url=self.s.kalshi_base_url)
        except Exception as exc:
            log.warning("Clock check failed: %s", exc)
            return
        self.clock.update(reading)
        if abs(reading.offset_ms) > self.s.max_clock_offset_ms:
            self.db.log_event("clock", "warning",
                              f"This Mac's clock is {reading.offset_ms / 1000:+.1f}s off the exchange clock")

    def maintenance(self) -> None:
        """Keep the database small on a laptop: drop full order-book ladders (not the
        summary columns) after N days."""
        cutoff = now_ms() - self.s.orderbook_levels_keep_days * 86_400_000
        cur = self.db.execute("UPDATE orderbook_snapshots SET levels_json=NULL "
                              "WHERE ts_ms < ? AND levels_json IS NOT NULL", (cutoff,))
        if cur.rowcount:
            log.info("Trimmed %d old order-book ladders", cur.rowcount)

    def step(self) -> None:
        t = time.monotonic()
        if t >= self._next["discovery"]:
            self._next["discovery"] = t + self.s.discovery_interval_s
            found = discover(self.client, self.db, self.s.symbols, self.series_map)
            now = now_ms()
            self.active = [m.ticker for ms in found.values() for m in ms
                           if m.close_ms is None or m.close_ms > now - 60_000]
            self.tradeable = [m.ticker for ms in found.values() for m in ms
                              if m.is_tradeable_status and (m.close_ms is None or m.close_ms > now)]
            log.info("Discovery: %s", {k: len(v) for k, v in found.items()})
        if t >= self._next["snapshot"]:
            self._next["snapshot"] = t + self.s.snapshot_interval_s
            # include markets that just closed so we capture their result and settlement value
            recent = [r["ticker"] for r in self.db.query(
                "SELECT ticker FROM markets WHERE close_ms BETWEEN ? AND ? AND (result IS NULL OR result='')",
                (now_ms() - 60 * 60_000, now_ms()))]
            refresh_markets(self.client, self.db, sorted(set(self.active + recent))[:50], self.s.orderbook_depth)
        if t >= self._next["crypto"]:
            self._next["crypto"] = t + self.s.crypto_interval_s
            self.poll_crypto()
        if t >= self._next["trades"]:
            self._next["trades"] = t + self.s.trades_interval_s
            self.poll_trades()
        if t >= self._next["clock"]:
            self._next["clock"] = t + self.s.clock_interval_s
            self.check_clock()
        if t >= self._next["health"]:
            self._next["health"] = t + self.s.health_interval_s
            ks = kill_status(self.db, self.s.kill_switch, self.s.kill_file)
            report = run_health(self.db, self.s, api_probe=self.client.get_exchange_status,
                                crypto_probe=lambda: self.provider.latest("BTC"), killed=ks.killed)
            self._health_ok = report.trading_allowed
            self.update_mode()
        if t >= self._next["decide"]:
            self._next["decide"] = t + self.s.decision_interval_s
            try:
                self.paper.step()
            except Exception as exc:     # a paper-trading bug must never stop data collection
                log.exception("Paper trader error")
                self.db.log_event("paper", "error", f"Paper trader error: {exc}")
            try:
                self.demo.step()
            except Exception as exc:     # same for the demo mirror
                log.exception("Demo mirror error")
                self.db.log_event("demo", "error", f"Demo mirror error: {exc}"[:300])
        if t >= self._next["maintenance"]:
            self._next["maintenance"] = t + 3600
            self.maintenance()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.db.log_event("collector", "info", "Collector started",
                          {"mode": self.s.trading_mode.value, "env": self.s.kalshi_env})
        self.start_feeds()
        failures = 0
        while not self._stop_event.is_set():
            self.db.heartbeat("collector", {"paused": self.paused(), "active_markets": len(self.active)})
            if self.db.get_control("collector_restart") == "requested":
                # new API key saved on the dashboard: exit cleanly, run_forever.sh restarts us
                self.db.set_control("collector_restart", "done", "collector")
                self.db.log_event("collector", "info", "Restarting to pick up the new Kalshi API key")
                self.stop()
                break
            if self.paused():
                self._stop_event.wait(2)
                continue
            try:
                self.step()
                failures = 0
            except KalshiAPIError as exc:
                failures += 1
                log.error("Kalshi API problem (%d in a row): %s", failures, exc)
                if failures in (3, 10):
                    self.db.log_event("collector", "critical", f"Kalshi API failing: {exc}")
                self._stop_event.wait(min(60, 2 ** failures))
            except Exception as exc:  # keep running; the supervisor restarts us if we die anyway
                failures += 1
                log.exception("Collector error")
                self.db.log_event("collector", "error", f"Unexpected error: {exc}")
                self._stop_event.wait(min(60, 2 ** failures))
            self._stop_event.wait(0.5)
        deadline = time.monotonic() + 4          # stop.sh force-kills after 10 s
        for th in self.threads:
            th.join(timeout=max(0.1, deadline - time.monotonic()))
        self.db.log_event("collector", "info", "Collector stopped")


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_dir, "collector")
    db = open_db(settings.db_path)
    Collector(settings, db).run()


if __name__ == "__main__":
    main()
