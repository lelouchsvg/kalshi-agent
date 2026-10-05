"""Shared machinery for streaming feeds: a reconnecting reader thread with live stats.

Every feed records two clocks for each message: the source's own timestamp and the
moment we received it. Models may only use what had been *received* by decision
time, so the received clock is what prevents look-ahead.
"""
from __future__ import annotations

import json
import logging
import socket
import statistics
import threading
import time
from collections import deque
from typing import Callable

from ..db import Database, now_ms
from .ws import WebSocket, WebSocketError

log = logging.getLogger("stream")


class FeedStats:
    """Rolling counters for one feed, saved as its heartbeat for the dashboard."""

    def __init__(self, name: str):
        self.name = name
        self.connected = False
        self.connects = 0
        self.last_msg_ms: int | None = None
        self.last_error: str | None = None
        self.status_note = ""
        self._msgs: deque[int] = deque()
        self._lat: deque[tuple[int, float]] = deque()
        self._lock = threading.Lock()

    def message(self, latency_ms: float | None = None) -> None:
        now = now_ms()
        with self._lock:
            self.last_msg_ms = now
            self._msgs.append(now)
            if latency_ms is not None:
                self._lat.append((now, latency_ms))
            cutoff = now - 60_000
            while self._msgs and self._msgs[0] < cutoff:
                self._msgs.popleft()
            while self._lat and self._lat[0][0] < cutoff:
                self._lat.popleft()

    def snapshot(self) -> dict:
        with self._lock:
            lats = [v for _, v in self._lat]
            return {"connected": self.connected, "connects": self.connects,
                    "last_msg_ms": self.last_msg_ms, "msgs_per_min": len(self._msgs),
                    "latency_ms_median": statistics.median(lats) if lats else None,
                    "last_error": self.last_error, "note": self.status_note}

    def save(self, db: Database) -> None:
        db.heartbeat(f"feed:{self.name}", self.snapshot())


class StreamFeed(threading.Thread):
    """Connects, subscribes, reads messages, and reconnects with backoff forever.

    Subclasses implement url(), headers(), on_open(ws) and on_message(dict).
    If no message arrives for `stale_after_s`, the connection is recycled.
    """
    name_ = "feed"
    stale_after_s = 30.0

    def __init__(self, db: Database, stop_event: threading.Event,
                 ws_factory: Callable[..., WebSocket] = WebSocket,
                 paused_fn: Callable[[], bool] = lambda: False):
        super().__init__(daemon=True, name=self.name_)
        self.paused_fn = paused_fn
        self.db = db
        self.stop_event = stop_event
        self.ws_factory = ws_factory
        self.stats = FeedStats(self.name_)
        self._last_save = 0.0

    def url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict[str, str]:
        return {}

    def on_open(self, ws: WebSocket) -> None:
        pass

    def on_message(self, msg: dict) -> None:
        raise NotImplementedError

    def on_idle(self) -> None:
        """Called at least once a second while connected; for periodic flushing."""

    def _maybe_save(self, force: bool = False) -> None:
        if force or time.monotonic() - self._last_save >= 5:
            self._last_save = time.monotonic()
            try:
                self.stats.save(self.db)
            except Exception:  # never let bookkeeping kill the feed
                log.exception("Could not save %s stats", self.name_)

    def run_once(self) -> None:
        ws = self.ws_factory(self.url(), headers=self.headers(), read_timeout=1.0)
        ws.connect()
        self.stats.connected, self.stats.last_error = True, None
        self.stats.connects += 1
        self._maybe_save(force=True)
        try:
            self.on_open(ws)
            last_rx = time.monotonic()
            while not self.stop_event.is_set():
                try:
                    raw = ws.recv()
                except socket.timeout:
                    raw = None
                if raw is not None:
                    last_rx = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and not self.paused_fn():
                        self.on_message(msg)
                elif time.monotonic() - last_rx > self.stale_after_s:
                    raise WebSocketError(f"no data for {self.stale_after_s:.0f}s")
                self.on_idle()
                self._maybe_save()
        finally:
            self.stats.connected = False
            ws.close()
            self._maybe_save(force=True)

    def run(self) -> None:
        failures = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as exc:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.warning("%s stream dropped: %s", self.name_, exc)
            self.stats.connected = False
            self._maybe_save(force=True)
            failures = 0 if time.monotonic() - started > 120 else failures + 1
            if failures == 5:
                self.db.log_event(self.name_, "warning", f"{self.name_} stream keeps dropping: "
                                  f"{self.stats.last_error}")
            self.stop_event.wait(min(60, 2 ** min(failures, 6)))
