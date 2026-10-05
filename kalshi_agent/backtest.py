"""Backtester (Phase 4): replays held-out history through the same decision rule the
paper engine uses. Results are SIMULATED and labelled as such everywhere.

Honest-by-construction rules:
  * only markets the model never trained on (strictly later, with an embargo)
  * enter at the ask (taker) plus slippage, pay Kalshi's taker fee
  * at most one entry per market, held to settlement
  * prices come from one-minute candle closes, which can be slightly stale versus a
    real order book; the paper engine trades real live books instead
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .dataset import Example
from .db import Database, now_ms
from .fees import taker_fee
from .model import LogisticModel, brier, calibration, log_loss
from .strategy import decide, size


@dataclass
class BacktestResult:
    n_markets: int = 0
    n_trades: int = 0
    wins: int = 0
    pnl: float = 0.0
    cost: float = 0.0
    fees: float = 0.0
    max_drawdown: float = 0.0
    trades: list[dict] = field(default_factory=list)

    def summary(self) -> dict:
        return {"label": "SIMULATED", "n_markets": self.n_markets, "n_trades": self.n_trades,
                "wins": self.wins, "win_rate": self.wins / self.n_trades if self.n_trades else None,
                "pnl": round(self.pnl, 2), "fees": round(self.fees, 2),
                "roi_on_cost": self.pnl / self.cost if self.cost else None,
                "pnl_per_trade": self.pnl / self.n_trades if self.n_trades else None,
                "max_drawdown": round(self.max_drawdown, 2)}


def p_yes_for(model: LogisticModel, e: Example) -> float:
    p_above = model.predict(e.features.vector())
    return 1 - p_above if e.flip else p_above


def run(model: LogisticModel, examples: list[Example], risk, slippage: float = 0.01,
        fee_multiplier: float = 1.0) -> BacktestResult:
    res = BacktestResult()
    seen, traded = set(), set()
    cum = peak = 0.0
    for e in examples:
        seen.add(e.ticker)
        if e.ticker in traded:
            continue
        d = decide(p_yes_for(model, e), e.yes_bid, e.yes_ask, min_edge=risk.min_edge,
                   max_spread=risk.max_spread, slippage=slippage, fee_multiplier=fee_multiplier)
        if d.action != "BUY":
            continue
        n = size(d.price, max_order_size=risk.max_order_size, max_market_exposure=risk.max_market_exposure)
        if n <= 0:
            continue
        traded.add(e.ticker)
        fee = taker_fee(n, d.price, fee_multiplier)
        won = e.result == d.outcome
        pnl = (n if won else 0) - n * d.price - fee
        res.n_trades += 1
        res.wins += won
        res.pnl += pnl
        res.cost += n * d.price
        res.fees += fee
        cum += pnl
        peak = max(peak, cum)
        res.max_drawdown = max(res.max_drawdown, peak - cum)
        res.trades.append({"ticker": e.ticker, "t_ms": e.t_ms, "outcome": d.outcome, "price": d.price,
                           "contracts": n, "p_yes": d.p_yes, "edge": d.edge, "result": e.result,
                           "pnl": round(pnl, 4)})
    res.n_markets = len(seen)
    return res


def evaluate(model: LogisticModel, examples: list[Example]) -> dict:
    """Probability quality on examples, versus the market's own midpoint."""
    y = [e.label_above for e in examples]
    p = [model.predict(e.features.vector()) for e in examples]
    m = [e.features.market_mid for e in examples]
    return {"n": len(y), "brier": brier(p, y), "log_loss": log_loss(p, y),
            "market_brier": brier(m, y), "market_log_loss": log_loss(m, y),
            "calibration": calibration(p, y), "market_ece": calibration(m, y)["ece"]}


def save_run(db: Database, model_version: str, examples: list[Example], result: BacktestResult,
             config: dict, split: str = "out_of_sample") -> int:
    return db.insert("backtest_runs", {
        "created_ms": now_ms(), "model_version": model_version, "config_json": json.dumps(config),
        "data_start_ms": examples[0].t_ms if examples else None,
        "data_end_ms": examples[-1].t_ms if examples else None, "split": split,
        "metrics_json": json.dumps({**result.summary(), "trades": result.trades[-200:]}), "status": "done"})
