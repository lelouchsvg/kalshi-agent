"""Trading modes.

PAPER is the default. LIVE exists as a name only; it is unreachable unless every
gate in `safety.py` passes, and in this phase the code-level gate is hard-wired off.
"""
from __future__ import annotations

from enum import Enum


class TradingMode(str, Enum):
    WATCH = "WATCH"        # collect data, never simulate or send orders
    PAPER = "PAPER"        # simulated orders against live prices (default)
    BACKTEST = "BACKTEST"  # historical replay only
    DEMO = "DEMO"          # Kalshi demo exchange (fake money), later phase
    LIVE = "LIVE"          # real money; locked

    @classmethod
    def parse(cls, value: str | None) -> "TradingMode":
        if value is None or str(value).strip() == "":
            return cls.PAPER
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            # Unknown values fail safe to WATCH, never to anything that trades.
            return cls.WATCH

    @property
    def sends_real_orders(self) -> bool:
        return self in (TradingMode.DEMO, TradingMode.LIVE)

    @property
    def uses_real_money(self) -> bool:
        return self is TradingMode.LIVE


DEFAULT_MODE = TradingMode.PAPER
