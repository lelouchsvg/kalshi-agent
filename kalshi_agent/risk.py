"""Risk engine (Phase 5). Every trade, paper included, must pass every check here.
The answer to any doubt is no."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .db import Database


def start_of_today_ms() -> int:
    lt = time.localtime()
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) * 1000)


@dataclass
class AccountState:
    mode: str
    starting_balance: float
    realized: float = 0.0
    realized_today: float = 0.0
    fees: float = 0.0
    open_cost: float = 0.0
    open_contracts: float = 0.0
    open_tickers: dict[str, float] = field(default_factory=dict)
    peak_equity: float = 0.0
    consecutive_losses: int = 0

    @property
    def equity(self) -> float:
        return self.starting_balance + self.realized

    @property
    def cash(self) -> float:
        return self.equity - self.open_cost

    @property
    def drawdown(self) -> float:
        return max(0.0, self.peak_equity - self.equity)


def account_state(db: Database, mode: str, starting_balance: float) -> AccountState:
    st = AccountState(mode, starting_balance, peak_equity=starting_balance)
    today = start_of_today_ms()
    cum = 0.0
    for t in db.query("SELECT pnl, fees, exit_ms FROM trades WHERE mode=? AND pnl IS NOT NULL ORDER BY exit_ms",
                      (mode,)):
        cum += t["pnl"]
        st.peak_equity = max(st.peak_equity, starting_balance + cum)
        st.fees += t["fees"] or 0
        if (t["exit_ms"] or 0) >= today:
            st.realized_today += t["pnl"]
            st.consecutive_losses = st.consecutive_losses + 1 if t["pnl"] <= 0 else 0
    st.realized = cum
    for t in db.query("SELECT ticker, count, entry_price, fees FROM trades WHERE mode=? AND pnl IS NULL", (mode,)):
        st.open_cost += t["count"] * t["entry_price"] + (t["fees"] or 0)
        st.open_contracts += t["count"]
        st.open_tickers[t["ticker"]] = st.open_tickers.get(t["ticker"], 0) + t["count"]
    return st


@dataclass
class RiskVerdict:
    ok: bool
    code: str
    message: str


def check(st: AccountState, limits, *, ticker: str, price: float, contracts: int,
          seconds_to_close: float, data_age_s: float, min_seconds_to_close: float = 60,
          max_data_age_s: float = 20) -> RiskVerdict:
    cost = price * contracts
    rules = [
        (contracts <= 0, "SIZE_ZERO", "Position size works out to zero contracts."),
        (data_age_s > max_data_age_s, "STALE_DATA", f"Market data is {data_age_s:.0f}s old."),
        (seconds_to_close < min_seconds_to_close, "TOO_CLOSE_TO_EXPIRY",
         f"Only {seconds_to_close:.0f}s left; too late to enter."),
        (ticker in st.open_tickers, "ALREADY_IN_MARKET", "Already holding a position in this market."),
        (contracts > limits.max_order_size, "ORDER_TOO_BIG", "Order larger than max_order_size."),
        (contracts > limits.max_position_size, "POSITION_TOO_BIG", "Position larger than max_position_size."),
        (cost > limits.max_market_exposure, "MARKET_EXPOSURE", "Too many dollars at risk in one market."),
        (len(st.open_tickers) >= limits.max_open_positions, "MAX_OPEN_POSITIONS",
         f"Already {len(st.open_tickers)} open positions (limit {limits.max_open_positions})."),
        (st.open_contracts + contracts > limits.max_contract_exposure, "CONTRACT_EXPOSURE",
         "Total open contracts would exceed max_contract_exposure."),
        (st.realized_today <= -limits.max_daily_loss, "DAILY_LOSS_LIMIT",
         f"Daily loss limit hit (${-st.realized_today:.2f}). No new trades until tomorrow."),
        (st.drawdown >= limits.max_drawdown, "MAX_DRAWDOWN",
         f"Drawdown ${st.drawdown:.2f} reached the ${limits.max_drawdown:.0f} limit."),
        (st.consecutive_losses >= limits.max_consecutive_losses, "LOSING_STREAK",
         f"{st.consecutive_losses} losses in a row today. Pausing until tomorrow."),
        (cost > st.cash, "INSUFFICIENT_CASH", "Not enough paper cash."),
    ]
    for failed, code, msg in rules:
        if failed:
            return RiskVerdict(False, code, msg)
    return RiskVerdict(True, "OK", "All risk checks passed.")
