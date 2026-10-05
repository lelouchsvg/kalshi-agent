"""Parsed Kalshi objects.

Kalshi now sends prices as fixed-point dollar strings (fields ending `_dollars`,
e.g. "0.5600") and quantities as fixed-point strings (fields ending `_fp`).
Legacy integer-cent fields are read only as a fallback.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def price(d: dict[str, Any], name: str) -> float | None:
    """Read `<name>_dollars`, falling back to legacy integer cents `<name>`."""
    v = to_float(d.get(f"{name}_dollars"))
    if v is not None:
        return v
    cents = to_float(d.get(name))
    return cents / 100.0 if cents is not None else None


def qty(d: dict[str, Any], name: str) -> float | None:
    v = to_float(d.get(f"{name}_fp"))
    return v if v is not None else to_float(d.get(name))


def iso_to_ms(value: Any) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


@dataclass
class Market:
    ticker: str
    event_ticker: str | None
    status: str | None
    title: str | None
    open_ms: int | None
    close_ms: int | None
    expiration_ms: int | None
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    last_price: float | None
    volume: float | None
    open_interest: float | None
    strike_type: str | None
    floor_strike: float | None
    cap_strike: float | None
    result: str | None
    rules_primary: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round(self.yes_ask - self.yes_bid, 4)

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return (self.yes_bid + self.yes_ask) / 2

    def seconds_to_close(self, now_ms: int) -> float | None:
        return None if self.close_ms is None else (self.close_ms - now_ms) / 1000.0

    @property
    def is_tradeable_status(self) -> bool:
        return (self.status or "").lower() in ("open", "active")

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Market":
        return cls(
            ticker=d["ticker"],
            event_ticker=d.get("event_ticker"),
            status=d.get("status"),
            title=d.get("title") or d.get("yes_sub_title"),
            open_ms=iso_to_ms(d.get("open_time")),
            close_ms=iso_to_ms(d.get("close_time")),
            expiration_ms=iso_to_ms(d.get("expected_expiration_time") or d.get("latest_expiration_time")),
            yes_bid=price(d, "yes_bid"),
            yes_ask=price(d, "yes_ask"),
            no_bid=price(d, "no_bid"),
            no_ask=price(d, "no_ask"),
            last_price=price(d, "last_price"),
            volume=qty(d, "volume"),
            open_interest=qty(d, "open_interest"),
            strike_type=d.get("strike_type"),
            floor_strike=to_float(d.get("floor_strike")),
            cap_strike=to_float(d.get("cap_strike")),
            result=d.get("result") or None,
            rules_primary=d.get("rules_primary"),
            raw=d,
        )


@dataclass
class Orderbook:
    """Kalshi books are bids only: YES bids and NO bids.
    A NO bid at p is equivalent to a YES ask at (1 - p)."""
    ticker: str
    yes: list[tuple[float, float]]   # (price, qty), YES bids
    no: list[tuple[float, float]]    # (price, qty), NO bids

    @property
    def best_yes_bid(self) -> float | None:
        return max((p for p, _ in self.yes), default=None)

    @property
    def best_no_bid(self) -> float | None:
        return max((p for p, _ in self.no), default=None)

    @property
    def best_yes_ask(self) -> float | None:
        b = self.best_no_bid
        return None if b is None else round(1.0 - b, 4)

    @property
    def best_no_ask(self) -> float | None:
        b = self.best_yes_bid
        return None if b is None else round(1.0 - b, 4)

    @property
    def yes_depth(self) -> float:
        return sum(q for _, q in self.yes)

    @property
    def no_depth(self) -> float:
        return sum(q for _, q in self.no)

    @property
    def imbalance(self) -> float | None:
        total = self.yes_depth + self.no_depth
        return None if total == 0 else (self.yes_depth - self.no_depth) / total

    def yes_ask_levels(self) -> list[tuple[float, float]]:
        """Asks for buying YES, cheapest first (derived from NO bids)."""
        return sorted(((round(1.0 - p, 4), q) for p, q in self.no), key=lambda x: x[0])

    def no_ask_levels(self) -> list[tuple[float, float]]:
        return sorted(((round(1.0 - p, 4), q) for p, q in self.yes), key=lambda x: x[0])

    @classmethod
    def from_api(cls, ticker: str, d: dict[str, Any]) -> "Orderbook":
        fp = d.get("orderbook_fp")
        if fp is not None:
            yes = [(float(p), float(q)) for p, q in (fp.get("yes_dollars") or [])]
            no = [(float(p), float(q)) for p, q in (fp.get("no_dollars") or [])]
        else:  # legacy integer-cent format
            ob = d.get("orderbook") or {}
            yes = [(p / 100.0, float(q)) for p, q in (ob.get("yes") or [])]
            no = [(p / 100.0, float(q)) for p, q in (ob.get("no") or [])]
        return cls(ticker=ticker, yes=yes, no=no)
