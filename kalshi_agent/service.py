"""The always-on collector service (runs under systemd on the server).

Phase 1: discovers markets, records market/order-book snapshots and crypto prices,
runs health checks. It contains no trading logic, so it can never place an order.
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time

from .config import Settings, load_settings
from .crypto.provider import get_provider
from .db import Database, now_ms, open_db
from .discovery import discover, refresh_markets
from .health import run_health
from .kalshi.client import KalshiAPIError, build_client
from .logging_setup import setup_logging
from .safety import evaluate_mode, kill_status

log = logging.getLogger("collector")


class Collector:
    def __init__(self, settings: Settings, db: Database):
        self.s = settings
        self.db = db
        self.client = build_client(settings)
        self.provider = get_provider(settings.crypto_provider)
        self.series_map = dict(settings.series)
        self.active: list[str] = []
        self._next = {"discovery": 0.0, "snapshot": 0.0, "crypto": 0.0, "health": 0.0}
        self._stop_event = threading.Event()
        self._health_ok = False

    def stop(self, *_):
        self._stop_event.set()

    def paused(self) -> bool:
        return self.db.get_control("collector", "running") == "paused"

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

    def step(self) -> None:
        t = time.monotonic()
        if t >= self._next["discovery"]:
            self._next["discovery"] = t + self.s.discovery_interval_s
            found = discover(self.client, self.db, self.s.symbols, self.series_map)
            now = now_ms()
            self.active = [m.ticker for ms in found.values() for m in ms
                           if m.close_ms is None or m.close_ms > now - 60_000]
            log.info("Discovery: %s", {k: len(v) for k, v in found.items()})
        if t >= self._next["snapshot"]:
            self._next["snapshot"] = t + self.s.snapshot_interval_s
            # include markets that just closed so we capture their result
            recent = [r["ticker"] for r in self.db.query(
                "SELECT ticker FROM markets WHERE close_ms BETWEEN ? AND ? AND (result IS NULL OR result='')",
                (now_ms() - 30 * 60_000, now_ms()))]
            refresh_markets(self.client, self.db, sorted(set(self.active + recent))[:50], self.s.orderbook_depth)
        if t >= self._next["crypto"]:
            self._next["crypto"] = t + self.s.crypto_interval_s
            for sym in self.s.symbols:
                try:
                    tick = self.provider.latest(sym)
                    self.db.insert("crypto_prices", {
                        "symbol": sym, "ts_ms": tick.ts_ms, "received_ms": tick.received_ms,
                        "price": tick.price, "bid": tick.bid, "ask": tick.ask,
                        "volume_24h": tick.volume_24h, "provider": tick.provider})
                except Exception as exc:
                    log.warning("Crypto price %s failed: %s", sym, exc)
        if t >= self._next["health"]:
            self._next["health"] = t + self.s.health_interval_s
            ks = kill_status(self.db, self.s.kill_switch, self.s.kill_file)
            report = run_health(self.db, self.s, api_probe=self.client.get_exchange_status,
                                crypto_probe=lambda: self.provider.latest("BTC"), killed=ks.killed)
            self._health_ok = report.trading_allowed
            self.update_mode()

    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.db.log_event("collector", "info", "Collector started",
                          {"mode": self.s.trading_mode.value, "env": self.s.kalshi_env})
        failures = 0
        while not self._stop_event.is_set():
            self.db.heartbeat("collector", {"paused": self.paused(), "active_markets": len(self.active)})
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
            except Exception as exc:  # keep running; systemd restarts us if we die anyway
                failures += 1
                log.exception("Collector error")
                self.db.log_event("collector", "error", f"Unexpected error: {exc}")
                self._stop_event.wait(min(60, 2 ** failures))
            self._stop_event.wait(0.5)
        self.db.log_event("collector", "info", "Collector stopped")


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_dir, "collector")
    db = open_db(settings.db_path)
    Collector(settings, db).run()


if __name__ == "__main__":
    main()
