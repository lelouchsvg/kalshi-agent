"""Model training and the model registry (Phase 3 + 4).

Every few hours, once enough settled markets have been downloaded, this:
  1. builds point-in-time examples from history,
  2. splits them by time (train on older markets, test on newer ones it never saw),
  3. fits the logistic model on the training part only,
  4. measures it on the test part against the market's own prices,
  5. backtests the trading rule on the test part (SIMULATED),
  6. registers the result as MODEL_Vnnn: status 'paper' if it is at least as accurate
     as the market and well calibrated, otherwise 'rejected' with the reason.

Only a 'paper' model may drive paper trading. Nothing here can ever promote a model
to live trading; that status ('promoted_live') is never written by code.
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time

from . import backtest
from .dataset import build_examples, split_by_market
from .db import Database, now_ms
from .model import FEATURES, LogisticModel

log = logging.getLogger("trainer")

MIN_MARKETS = 300           # settled markets needed before the first model
MAX_ECE = 0.05              # calibration error allowed (or no worse than the market's own)
RETRAIN_EVERY_S = 6 * 3600


def active_model(db: Database, setting: str = "AUTO") -> tuple[str, LogisticModel] | None:
    """The model that drives decisions: an explicit version from settings, or (AUTO)
    the newest model whose status allows paper trading."""
    if setting in ("", "NONE"):
        return None
    if setting == "AUTO":
        row = db.query_one("SELECT version, hyperparams_json FROM model_versions WHERE status IN "
                           "('paper','promoted_live') ORDER BY created_ms DESC LIMIT 1")
    else:
        row = db.query_one("SELECT version, hyperparams_json FROM model_versions WHERE version=? "
                           "AND status NOT IN ('rejected','retired')", (setting,))
    if not row:
        return None
    return row["version"], LogisticModel.from_dict(json.loads(row["hyperparams_json"]))


def _next_version(db: Database) -> str:
    n = db.query_one("SELECT COUNT(*) AS n FROM model_versions")["n"]
    return f"MODEL_V{n + 1:03d}"


def train_once(db: Database, settings, min_markets: int = MIN_MARKETS) -> dict:
    t0 = time.monotonic()
    examples = build_examples(db, settings.symbols, since_ms=now_ms() - 60 * 86_400_000)
    n_markets = len({e.ticker for e in examples})
    if n_markets < min_markets:
        status = {"state": "waiting", "markets": n_markets, "needed": min_markets,
                  "message": f"Waiting for data: {n_markets} of {min_markets} usable settled markets."}
        db.set_control("trainer", json.dumps({**status, "updated_ms": now_ms()}), "trainer")
        return status
    train, test = split_by_market(examples)
    model = LogisticModel().fit([e.features.vector() for e in train], [e.label_above for e in train])
    ev_train, ev_test = backtest.evaluate(model, train), backtest.evaluate(model, test)
    bt = backtest.run(model, test, settings.risk, slippage=settings.paper_slippage)

    reasons = []
    if ev_test["brier"] is None or ev_test["brier"] > ev_test["market_brier"]:
        reasons.append("less accurate than the market's own prices on unseen markets")
    ece = ev_test["calibration"]["ece"]
    if ece is None or ece > max(MAX_ECE, ev_test["market_ece"] or 0):
        reasons.append(f"probabilities not well calibrated (error {ece or 1:.3f}, "
                       f"market's own {ev_test['market_ece'] or 0:.3f})")
    status = "paper" if not reasons else "rejected"
    version = _next_version(db)
    tickers = sorted({e.ticker for e in examples})
    metrics = {"train": {k: v for k, v in ev_train.items() if k != "calibration"},
               "test": ev_test, "backtest": bt.summary(), "reasons": reasons,
               "n_markets": n_markets, "n_train_markets": len({e.ticker for e in train}),
               "n_test_markets": len({e.ticker for e in test}), "seconds": round(time.monotonic() - t0, 1)}
    with db.tx() as c:
        if status == "paper":
            c.execute("UPDATE model_versions SET status='retired' WHERE status='paper'")
    db.insert("model_versions", {
        "version": version, "created_ms": now_ms(), "model_type": "logistic_regression",
        "features_json": json.dumps(FEATURES), "hyperparams_json": json.dumps(model.to_dict()),
        "train_start_ms": train[0].t_ms, "train_end_ms": train[-1].t_ms,
        "test_start_ms": test[0].t_ms if test else None, "test_end_ms": test[-1].t_ms if test else None,
        "dataset_hash": hashlib.sha256("|".join(tickers).encode()).hexdigest()[:16],
        "metrics_json": json.dumps(metrics), "status": status,
        "notes": "; ".join(reasons) or "passed: at least as accurate as the market and well calibrated"})
    run_id = backtest.save_run(db, version, test, bt, {"min_edge": settings.risk.min_edge,
                                                       "max_spread": settings.risk.max_spread,
                                                       "slippage": settings.paper_slippage})
    db.insert("experiments", {
        "created_ms": now_ms(), "hypothesis": "Retrain baseline logistic model on latest history",
        "author": "trainer", "candidate_version": version, "backtest_run_ids": str(run_id),
        "result_json": json.dumps({"test_brier": ev_test["brier"], "market_brier": ev_test["market_brier"],
                                   "backtest": bt.summary()}),
        "decision": "promoted" if status == "paper" else "rejected",
        "decision_reason": metrics and ("; ".join(reasons) or "promoted to paper trading")})
    db.log_event("trainer", "info" if status == "paper" else "warning",
                 f"{version} trained on {metrics['n_train_markets']} markets: "
                 + ("approved for paper trading" if status == "paper" else "rejected: " + "; ".join(reasons)))
    out = {"state": "trained", "version": version, "status": status, "metrics": metrics}
    db.set_control("trainer", json.dumps({"state": "trained", "version": version, "status": status,
                                          "updated_ms": now_ms()}), "trainer")
    return out


class Trainer(threading.Thread):
    def __init__(self, db: Database, settings, stop_event: threading.Event, paused_fn=lambda: False):
        super().__init__(daemon=True, name="trainer")
        self.db, self.s, self.stop_event, self.paused_fn = db, settings, stop_event, paused_fn

    def due(self) -> bool:
        last = self.db.query_one("SELECT MAX(created_ms) AS t FROM model_versions")["t"]
        return last is None or now_ms() - last > RETRAIN_EVERY_S * 1000

    def run(self) -> None:
        self.stop_event.wait(60)
        while not self.stop_event.is_set():
            if not self.paused_fn() and self.due():
                try:
                    train_once(self.db, self.s)
                except Exception as exc:
                    log.exception("Training failed")
                    self.db.log_event("trainer", "error", f"Training failed: {exc}")
            self.stop_event.wait(600)
