"""The trading decision, shared by the backtester and the paper engine so that what
is tested is exactly what runs. Default answer: PASS.

BUY only when, after Kalshi fees and an allowance for slippage, the model's
probability beats the price we would actually pay by at least `min_edge`, and the
market is liquid enough (spread) and not at an extreme price.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fees import executable_ev


@dataclass
class Decision:
    action: str            # BUY | PASS
    reason_code: str
    explanation: str
    p_yes: float | None = None
    outcome: str | None = None      # yes | no
    price: float | None = None      # what we would pay per contract (incl. slippage)
    edge: float | None = None       # expected profit per contract after fees
    market_price: float | None = None


def decide(p_yes: float, yes_bid: float | None, yes_ask: float | None, *, min_edge: float,
           max_spread: float, slippage: float = 0.01, fee_multiplier: float = 1.0,
           min_price: float = 0.05, max_price: float = 0.95) -> Decision:
    if yes_bid is None or yes_ask is None:
        return Decision("PASS", "NO_QUOTE", "No two-sided market to trade against.", p_yes)
    mid = (yes_bid + yes_ask) / 2
    spread = yes_ask - yes_bid
    if spread > max_spread + 1e-9:
        return Decision("PASS", "SPREAD_TOO_WIDE", f"Spread {spread * 100:.0f}¢ is wider than the "
                        f"{max_spread * 100:.0f}¢ limit.", p_yes, market_price=mid)
    options = []
    for outcome, ask in (("yes", yes_ask), ("no", round(1 - yes_bid, 4))):
        if not (min_price <= ask <= max_price):
            continue
        ev = executable_ev(p_yes, outcome, ask, 1, slippage, fee_multiplier)
        options.append((ev.edge_per_contract, outcome, ev.entry_price))
    if not options:
        return Decision("PASS", "PRICE_EXTREME", "Prices are too close to 0¢ or 100¢ to be worth the risk.",
                        p_yes, market_price=mid)
    edge, outcome, price = max(options)
    if edge < min_edge:
        return Decision("PASS", "EDGE_TOO_SMALL",
                        f"Model says {p_yes * 100:.0f}% YES, market {mid * 100:.0f}¢. Best edge after fees "
                        f"{edge * 100:+.1f}¢ is below the {min_edge * 100:.0f}¢ minimum.",
                        p_yes, outcome, price, edge, mid)
    return Decision("BUY", "EDGE", f"Model says {p_yes * 100:.0f}% YES vs market {mid * 100:.0f}¢: buy "
                    f"{outcome.upper()} at {price * 100:.0f}¢ for {edge * 100:+.1f}¢ expected per contract "
                    f"after fees.", p_yes, outcome, price, edge, mid)


def size(price: float, *, max_order_size: float, max_market_exposure: float) -> int:
    """Contracts to buy: capped by order size and by dollars at risk in one market."""
    return max(0, int(min(max_order_size, max_market_exposure // max(price, 0.01))))
