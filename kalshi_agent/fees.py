"""Kalshi trading fees and executable expected value.

Fee model (Kalshi fee schedule, as published and summarized 2026-10):
  taker fee = ceil_to_cent(M * 0.07 * C * P * (1 - P))
  maker fee = ceil_to_cent(Mm * 0.0175 * C * P * (1 - P)), Mm = 0 on most series
P is the contract price in dollars, C the number of contracts, M the series multiplier.
Rounding up to a whole cent is the conservative (pessimistic) choice; the live
`series_info.fee_multiplier` value overrides M when Kalshi reports one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TAKER_RATE = 0.07
MAKER_RATE = 0.0175


def ceil_cent(x: float) -> float:
    return math.ceil(round(x * 100, 9)) / 100.0


def taker_fee(contracts: float, price: float, multiplier: float = 1.0) -> float:
    if contracts <= 0:
        return 0.0
    if not 0 <= price <= 1:
        raise ValueError("price must be in dollars between 0 and 1")
    return ceil_cent(multiplier * TAKER_RATE * contracts * price * (1 - price))


def maker_fee(contracts: float, price: float, multiplier: float = 0.0) -> float:
    if contracts <= 0 or multiplier == 0:
        return 0.0
    return ceil_cent(multiplier * MAKER_RATE * contracts * price * (1 - price))


@dataclass
class EVResult:
    outcome: str            # "yes" or "no"
    p_win: float
    entry_price: float      # dollars per contract we pay
    contracts: float
    cost: float             # entry_price * contracts
    fees: float
    slippage: float         # expected extra cost from execution
    expected_payout: float  # p_win * $1 * contracts
    ev: float               # expected profit in dollars
    edge_per_contract: float

    @property
    def positive(self) -> bool:
        return self.ev > 0


def executable_ev(p_yes: float, outcome: str, ask_price: float, contracts: float,
                  slippage_per_contract: float = 0.0, fee_multiplier: float = 1.0,
                  fill_probability: float = 1.0) -> EVResult:
    """EV of BUYING `outcome` at the current ask (taker), after fees and slippage.

    EV = P(win) * payout - cost - fees - expected execution costs, scaled by the
    chance we actually get filled (an unfilled order earns 0, costs 0).
    """
    if not 0 <= p_yes <= 1:
        raise ValueError("p_yes must be between 0 and 1")
    outcome = outcome.lower()
    p_win = p_yes if outcome == "yes" else 1 - p_yes
    fill_price = min(ask_price + slippage_per_contract, 0.99)
    cost = fill_price * contracts
    fees = taker_fee(contracts, fill_price, fee_multiplier)
    payout = p_win * 1.0 * contracts
    ev_if_filled = payout - cost - fees
    ev = ev_if_filled * fill_probability
    return EVResult(outcome, p_win, fill_price, contracts, cost, fees,
                    slippage_per_contract * contracts, payout, ev,
                    ev_if_filled / contracts if contracts else 0.0)
