"""Read-only views of system state, shared by the dashboard and the CLI.

Every number here is computed from rows in the database. Nothing is estimated
or invented; empty tables produce empty results and say so.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from . import __version__
from .config import Settings
from .db import Database, now_ms
from .modes import TradingMode
from .safety import LIVE_TRADING_UNLOCKED, kill_status

PHASE = 5
PHASE_NOTE = ("Paper trading with fake money against live Kalshi prices. The agent buys only when an approved "
              "model shows an edge after fees and every risk check passes; otherwise it passes. "
              "Real-money trading is locked.")


def _json(v: str | None, default: Any = None) -> Any:
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def status(db: Database, s: Settings) -> dict[str, Any]:
    ks = kill_status(db, s.kill_switch, s.kill_file)
    hb = {r["component"]: r["ts_ms"] for r in db.query("SELECT component, ts_ms FROM heartbeats")}
    collector_age = (now_ms() - hb["collector"]) / 1000 if "collector" in hb else None
    mode_state = _json(db.get_control("mode_state"), {})
    health = _json(db.get_control("last_health"), None)
    return {
        "version": __version__,
        "phase": PHASE,
        "phase_note": PHASE_NOTE,
        "online": collector_age is not None and collector_age < s.max_heartbeat_age_s,
        "collector_last_seen_s": collector_age,
        "collector_paused": db.get_control("collector", "running") == "paused",
        "requested_mode": s.trading_mode.value,
        "effective_mode": mode_state.get("effective", s.trading_mode.value
                                         if s.trading_mode not in (TradingMode.LIVE, TradingMode.DEMO) else "WATCH"),
        "mode_blocked_by": mode_state.get("blocked_by", []),
        "live_trading_unlocked": LIVE_TRADING_UNLOCKED,
        "kill_switch": {"engaged": ks.killed, "sources": ks.sources},
        "kalshi_env": s.kalshi_env,
        "credentials_configured": s.has_kalshi_credentials,
        "model_version": s.model_version,
        "health": health,
        "server_time_ms": now_ms(),
        "timezone": time.tzname[0],
        "settings": s.public_view(),
        "pid": os.getpid(),
    }


def markets(db: Database, s: Settings) -> list[dict[str, Any]]:
    now = now_ms()
    rows = db.query("""
        SELECT m.ticker, m.symbol, m.title, m.status, m.open_ms, m.close_ms, m.floor_strike,
               m.cap_strike, m.strike_type, m.result,
               ms.yes_bid, ms.yes_ask, ms.no_bid, ms.no_ask, ms.last_price, ms.spread, ms.volume,
               ms.open_interest, ms.ts_ms AS snap_ms,
               ob.imbalance, ob.yes_depth, ob.no_depth
        FROM markets m
        LEFT JOIN market_snapshots ms ON ms.id = (
            SELECT id FROM market_snapshots WHERE ticker = m.ticker ORDER BY ts_ms DESC LIMIT 1)
        LEFT JOIN orderbook_snapshots ob ON ob.id = (
            SELECT id FROM orderbook_snapshots WHERE ticker = m.ticker ORDER BY ts_ms DESC LIMIT 1)
        WHERE m.close_ms > ? OR m.close_ms IS NULL
        ORDER BY m.close_ms ASC
        LIMIT 60""", (now - 5 * 60_000,))
    prices = latest_crypto(db, s.symbols)
    for r in rows:
        r["seconds_to_close"] = (r["close_ms"] - now) / 1000 if r["close_ms"] else None
        r["underlying"] = prices.get(r["symbol"])
        r["data_age_s"] = (now - r["snap_ms"]) / 1000 if r["snap_ms"] else None
        sig = db.query_one("SELECT p_yes, edge, action, reason_code, explanation FROM signals "
                           "WHERE ticker=? ORDER BY ts_ms DESC LIMIT 1", (r["ticker"],))
        r["model_p_yes"] = sig["p_yes"] if sig else None
        r["edge"] = sig["edge"] if sig else None
        r["signal"] = sig["action"] if sig else "PASS"
        r["signal_reason"] = sig["explanation"] if sig else "Not evaluated yet"
        r["signal_code"] = sig["reason_code"] if sig else None
        r["spread_ok"] = r["spread"] is not None and r["spread"] <= s.risk.max_spread
    return rows


def latest_crypto(db: Database, symbols: list[str] | None = None) -> dict[str, dict[str, Any]]:
    symbols = symbols or [r["symbol"] for r in db.query("SELECT DISTINCT symbol FROM markets WHERE symbol IS NOT NULL")]
    out = {}
    for sym in symbols:
        r = db.query_one("SELECT price, ts_ms, provider FROM crypto_prices WHERE symbol=? "
                         "ORDER BY ts_ms DESC LIMIT 1", (sym,))
        if r:
            ix = db.query_one("SELECT value, avg60, n_sources, received_ms FROM index_ticks WHERE symbol=? "
                              "ORDER BY received_ms DESC LIMIT 1", (sym,))
            out[sym] = {**r, "index": ix}
    return out


def crypto_series(db: Database, symbol: str, minutes: int = 60) -> list[list[float]]:
    rows = db.query("SELECT ts_ms, price FROM crypto_prices WHERE symbol=? AND ts_ms>=? ORDER BY ts_ms",
                    (symbol, now_ms() - minutes * 60_000))
    step = max(1, len(rows) // 300)
    return [[r["ts_ms"], r["price"]] for r in rows[::step]]


def performance(db: Database, mode: str = "PAPER") -> dict[str, Any]:
    trades = db.query("SELECT * FROM trades WHERE mode=? AND pnl IS NOT NULL ORDER BY exit_ms", (mode,))
    curve = [[r["ts_ms"], r["equity"]] for r in db.query(
        "SELECT ts_ms, equity FROM pnl WHERE mode=? ORDER BY ts_ms", (mode,))]
    out: dict[str, Any] = {"mode": mode, "label": f"{mode} results" if mode != "LIVE" else "LIVE results",
                           "n_trades": len(trades), "equity_curve": curve}
    if not trades:
        out.update({"has_data": False, "message": f"No {mode.lower()} trades yet."})
        return out
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    now = now_ms()
    day, week = now - 86_400_000, now - 7 * 86_400_000
    cum, peak, mdd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    out.update({
        "has_data": True,
        "total_pnl": sum(pnls),
        "daily_pnl": sum(t["pnl"] for t in trades if (t["exit_ms"] or 0) >= day),
        "weekly_pnl": sum(t["pnl"] for t in trades if (t["exit_ms"] or 0) >= week),
        "win_rate": len(wins) / len(pnls),
        "avg_win": sum(wins) / len(wins) if wins else None,
        "avg_loss": sum(losses) / len(losses) if losses else None,
        "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        "max_drawdown": mdd,
        "expectancy": sum(pnls) / len(pnls),
        "fees": sum(t["fees"] or 0 for t in trades),
        "avg_edge": _mean([t["edge_at_entry"] for t in trades]),
    })
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def recent_trades(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM trades ORDER BY entry_ms DESC LIMIT ?", (limit,))


def recent_signals(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM signals ORDER BY ts_ms DESC LIMIT ?", (limit,))


def research(db: Database) -> dict[str, Any]:
    return {
        "models": db.query("SELECT version, created_ms, model_type, status, metrics_json FROM model_versions "
                           "ORDER BY created_ms DESC LIMIT 20"),
        "experiments": db.query("SELECT id, created_ms, hypothesis, author, decision, decision_reason "
                                "FROM experiments ORDER BY created_ms DESC LIMIT 20"),
        "backtests": db.query("SELECT id, created_ms, model_version, split, status, metrics_json "
                              "FROM backtest_runs ORDER BY created_ms DESC LIMIT 20"),
    }


def events(db: Database, limit: int = 40) -> list[dict[str, Any]]:
    sys = db.query("SELECT ts_ms, component AS source, level, message FROM system_events "
                   "ORDER BY ts_ms DESC LIMIT ?", (limit,))
    risk = db.query("SELECT ts_ms, 'risk' AS source, severity AS level, message FROM risk_events "
                    "ORDER BY ts_ms DESC LIMIT ?", (limit,))
    return sorted(sys + risk, key=lambda r: r["ts_ms"], reverse=True)[:limit]


def costs(s: Settings) -> dict[str, Any]:
    items = s.costs
    return {"items": items, "total_monthly_usd": round(sum(float(i.get("monthly_usd", 0)) for i in items), 2)}


def data_stats(db: Database) -> dict[str, Any]:
    out = {}
    for t in ("markets", "market_snapshots", "orderbook_snapshots", "crypto_prices", "index_ticks",
              "kalshi_trades", "market_candles", "crypto_candles", "signals", "trades"):
        out[t] = db.query_one(f"SELECT COUNT(*) AS n FROM {t}")["n"]
    try:
        out["db_mb"] = round(sum(p.stat().st_size for p in db.path.parent.glob(db.path.name + "*")) / 1e6, 1)
    except (OSError, ValueError):
        out["db_mb"] = None
    first = db.query_one("SELECT MIN(ts_ms) AS t FROM market_snapshots")["t"]
    out["collecting_since_ms"] = first
    out["settled_markets"] = db.query_one(
        "SELECT COUNT(*) AS n FROM markets WHERE result IN ('yes','no')")["n"]
    return out


def trade_gates(db: Database, s: Settings) -> list[dict[str, Any]]:
    """The eight conditions every trade needs (spec section 38), evaluated now from the
    paper trader's latest decisions."""
    from .risk import account_state
    from .trainer import active_model
    health = _json(db.get_control("last_health"), {}) or {}
    checks = {c["name"]: c["status"] for c in health.get("checks", [])}
    data_ok = checks.get("market_data_freshness") == "ok" and checks.get("crypto_data_freshness") == "ok"
    model = active_model(db, s.model_version)
    recent = db.query("""SELECT s.* FROM signals s JOIN (SELECT ticker, MAX(ts_ms) AS mx FROM signals
                         WHERE ts_ms > ? GROUP BY ticker) t ON s.ticker = t.ticker AND s.ts_ms = t.mx""",
                      (now_ms() - 120_000,))
    edges = [r["edge"] for r in recent if r["edge"] is not None]
    best = max(edges) if edges else None
    spread_row = db.query_one("""SELECT MIN(spread) AS s FROM market_snapshots
                                 WHERE ts_ms > ? AND spread IS NOT NULL""", (now_ms() - 60_000,))
    spread_ok = bool(spread_row and spread_row["s"] is not None and spread_row["s"] <= s.risk.max_spread)
    ks = kill_status(db, s.kill_switch, s.kill_file)
    health_ok = bool(health) and health.get("overall") != "critical" and not ks.killed
    hb = db.query_one("SELECT ts_ms FROM heartbeats WHERE component='paper'")
    engine_ok = bool(hb and now_ms() - hb["ts_ms"] < 60_000)
    st = account_state(db, "PAPER", s.paper_starting_balance)
    r = s.risk
    risk_block = ("daily loss limit hit" if st.realized_today <= -r.max_daily_loss else
                  "drawdown limit hit" if st.drawdown >= r.max_drawdown else
                  "losing streak pause" if st.consecutive_losses >= r.max_consecutive_losses else
                  "max open positions" if len(st.open_tickers) >= r.max_open_positions else None)
    return [
        {"gate": "Reliable data", "ok": data_ok, "why": "market and crypto data fresh" if data_ok else "data missing or stale"},
        {"gate": "Valid model prediction", "ok": bool(model),
         "why": f"{model[0]} approved" if model else "no approved model yet"},
        {"gate": "Positive expected value", "ok": best is not None and best > 0,
         "why": f"best edge now {best * 100:+.1f}¢ after fees" if best is not None else "no prediction yet"},
        {"gate": "Sufficient edge", "ok": best is not None and best >= r.min_edge,
         "why": f"needs ≥ {r.min_edge * 100:.0f}¢ after fees"},
        {"gate": "Acceptable spread", "ok": spread_ok,
         "why": f"tightest spread within {r.max_spread * 100:.0f}¢" if spread_ok else "no market with an acceptable spread"},
        {"gate": "Execution conditions", "ok": engine_ok,
         "why": "paper engine running on live books" if engine_ok else "paper engine not running"},
        {"gate": "Risk approval", "ok": risk_block is None, "why": risk_block or "within all limits"},
        {"gate": "System health", "ok": health_ok, "why": "healthy" if health_ok else
         ("kill switch engaged" if ks.killed else "health check failing or not run yet")},
    ]


def model_info(db: Database, s: Settings) -> dict[str, Any]:
    from .trainer import active_model
    found = active_model(db, s.model_version)
    latest = db.query_one("SELECT version, created_ms, status, metrics_json, notes FROM model_versions "
                          "ORDER BY created_ms DESC LIMIT 1")
    active = None
    if found:
        row = db.query_one("SELECT version, created_ms, status, metrics_json, notes, train_start_ms, test_end_ms "
                           "FROM model_versions WHERE version=?", (found[0],))
        active = {**row, "metrics": _json(row.pop("metrics_json"), {})}
    if latest:
        latest["metrics"] = _json(latest.pop("metrics_json"), {})
    return {"active": active, "latest": latest, "trainer": _json(db.get_control("trainer"), None)}


def open_positions(db: Database, mode: str = "PAPER") -> list[dict[str, Any]]:
    rows = db.query("""SELECT t.ticker, t.symbol, t.side, t.count, t.entry_price, t.fees, t.entry_ms,
                              t.p_yes_at_entry, t.edge_at_entry, m.close_ms
                       FROM trades t LEFT JOIN markets m ON m.ticker = t.ticker
                       WHERE t.mode=? AND t.pnl IS NULL ORDER BY t.entry_ms DESC""", (mode,))
    for r in rows:
        ob = db.query_one("SELECT best_yes_bid, best_yes_ask FROM orderbook_snapshots WHERE ticker=? "
                          "ORDER BY ts_ms DESC LIMIT 1", (r["ticker"],))
        bid = None
        if ob:
            bid = ob["best_yes_bid"] if r["side"] == "yes" else (
                None if ob["best_yes_ask"] is None else round(1 - ob["best_yes_ask"], 4))
        r["mark"] = bid
        r["unrealized"] = None if bid is None else r["count"] * (bid - r["entry_price"]) - (r["fees"] or 0)
    return rows


# What has to be true before real money is even discussed. LIVE stays locked in code
# regardless; this only tells you whether the evidence is there.
READINESS = {"min_trades": 200, "min_days": 14, "min_profit_factor": 1.2}


def live_readiness(db: Database, s: Settings) -> dict[str, Any]:
    trades = db.query("SELECT pnl, entry_ms, exit_ms, p_yes_at_entry, market_price_at_entry, side, result "
                      "FROM trades WHERE mode='PAPER' AND pnl IS NOT NULL ORDER BY exit_ms")
    n = len(trades)
    pnl = sum(t["pnl"] for t in trades)
    wins = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    losses = -sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = wins / losses if losses > 0 else (None if not n else float("inf"))
    days = (now_ms() - trades[0]["entry_ms"]) / 86_400_000 if trades else 0.0
    cum = peak = mdd = 0.0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    # live calibration: did the model's probabilities beat the market's on markets that settled?
    rows = db.query("""SELECT s.p_yes, s.market_price, m.result FROM signals s JOIN markets m ON m.ticker = s.ticker
                       WHERE s.mode='PAPER' AND s.p_yes IS NOT NULL AND s.market_price IS NOT NULL
                         AND m.result IN ('yes','no') ORDER BY s.ts_ms DESC LIMIT 20000""")
    y = [1 if r["result"] == "yes" else 0 for r in rows]
    mb = sum((r["p_yes"] - yi) ** 2 for r, yi in zip(rows, y)) / len(y) if y else None
    kb = sum((r["market_price"] - yi) ** 2 for r, yi in zip(rows, y)) / len(y) if y else None
    R = READINESS
    criteria = [
        {"name": f"At least {R['min_trades']} settled paper trades", "ok": n >= R["min_trades"],
         "progress": min(1.0, n / R["min_trades"]), "value": f"{n}"},
        {"name": f"At least {R['min_days']} days of paper trading", "ok": days >= R["min_days"],
         "progress": min(1.0, days / R["min_days"]), "value": f"{days:.1f} days"},
        {"name": "Profitable after fees", "ok": n > 0 and pnl > 0, "progress": 1.0 if pnl > 0 else 0.0,
         "value": f"${pnl:+.2f}" if n else "no trades yet"},
        {"name": f"Profit factor at least {R['min_profit_factor']}", "ok": pf is not None and pf >= R["min_profit_factor"],
         "progress": 0.0 if pf is None else min(1.0, pf / R["min_profit_factor"]),
         "value": "—" if pf is None else ("∞" if pf == float("inf") else f"{pf:.2f}")},
        {"name": "Model beat the market's prices on settled markets", "ok": mb is not None and mb < kb,
         "progress": 1.0 if (mb is not None and mb < kb) else 0.0,
         "value": f"model {mb:.4f} vs market {kb:.4f} (lower is better)" if mb is not None else "not measured yet"},
        {"name": f"Worst drawdown under ${s.risk.max_drawdown:.0f}", "ok": n > 0 and mdd < s.risk.max_drawdown,
         "progress": 1.0 if n and mdd < s.risk.max_drawdown else 0.0, "value": f"${mdd:.2f}" if n else "—"},
    ]
    return {"criteria": criteria, "all_met": all(c["ok"] for c in criteria),
            "live_locked": True, "note": "Even when every box is green, real-money trading stays locked in code "
            "until you and Claude review the results and unlock it on purpose, starting with tiny sizes."}


def outcome_from_value(strike_type: str | None, floor: float | None, cap: float | None,
                       value: float | None) -> str | None:
    """What a market resolves to for a given settlement value (None if we can't tell)."""
    if value is None:
        return None
    st = (strike_type or "").lower()
    if st in ("greater", "greater_or_equal", "above") and floor is not None:
        return "yes" if (value > floor or (st == "greater_or_equal" and value == floor)) else "no"
    if st in ("less", "less_or_equal", "below") and cap is not None:
        return "yes" if (value < cap or (st == "less_or_equal" and value == cap)) else "no"
    if st == "between" and floor is not None and cap is not None:
        return "yes" if floor <= value <= cap else "no"
    return None


def feeds(db: Database, s: Settings) -> list[dict[str, Any]]:
    now = now_ms()
    hb = {r["component"]: r for r in db.query("SELECT component, ts_ms, info_json FROM heartbeats")}
    labels = [
        ("feed:coinbase_ws", "Crypto prices, real-time", "Coinbase WebSocket"),
        ("feed:index_proxy", "Settlement index stand-in", "median of 4 exchanges, every few seconds"),
        ("snapshots", "Kalshi prices & order books", f"polled every {s.snapshot_interval_s}s"),
        ("trades", "Kalshi public trades", f"polled every {s.trades_interval_s}s"),
        ("feed:kalshi_ws", "Kalshi real-time stream", "optional, needs an API key"),
    ]
    out = []
    for key, label, how in labels:
        row = hb.get(key)
        info = _json(row["info_json"], {}) if row else {}
        last = info.get("last_msg_ms") or (row["ts_ms"] if row and not key.startswith("feed:") else None)
        age = (now - last) / 1000 if last else None
        live = age is not None and age < max(30, 3 * s.snapshot_interval_s)
        state = "live" if live else ("off" if key == "feed:kalshi_ws" and not info.get("connected")
                                     and not s.has_kalshi_credentials else "down" if row else "waiting")
        out.append({"key": key, "label": label, "how": how, "state": state, "age_s": age,
                    "msgs_per_min": info.get("msgs_per_min"), "latency_ms": info.get("latency_ms_median"),
                    "note": info.get("note") or info.get("last_error") or "",
                    "errors": info.get("source_errors") or {}})
    return out


def data_quality(db: Database, s: Settings) -> dict[str, Any]:
    clock = _json(db.get_control("clock"), None)
    backfill = _json(db.get_control("backfill"), None)
    settled = db.query("""
        SELECT ticker, symbol, close_ms, result, strike_type, floor_strike, cap_strike, expiration_value
        FROM markets WHERE result IN ('yes','no') ORDER BY close_ms DESC LIMIT 3000""")
    by_symbol: dict[str, dict[str, int]] = {}
    rule_checked = rule_agree = 0
    for m in settled:
        b = by_symbol.setdefault(m["symbol"] or "?", {"yes": 0, "no": 0})
        b[m["result"]] += 1
        implied = outcome_from_value(m["strike_type"], m["floor_strike"], m["cap_strike"], m["expiration_value"])
        if implied:
            rule_checked += 1
            rule_agree += implied == m["result"]
    # How well our free index stand-in matches Kalshi's reported settlement value.
    from .timeseries import settlement_proxy
    diffs, side_agree, n_proxy = [], 0, 0
    for m in settled[:500]:
        if m["expiration_value"] is None or m["close_ms"] is None:
            continue
        proxy = settlement_proxy(db, m["symbol"], m["close_ms"])
        if proxy is None:
            continue
        n_proxy += 1
        diffs.append(abs(proxy - m["expiration_value"]) / m["expiration_value"] * 100)
        side_agree += outcome_from_value(m["strike_type"], m["floor_strike"], m["cap_strike"], proxy) == m["result"]
    with_candles = db.query_one("SELECT COUNT(*) AS n FROM backfill_log WHERE candles > 0")["n"]
    crypto_hist = db.query("SELECT symbol, COUNT(*) AS n, MIN(start_ms) AS first_ms, MAX(start_ms) AS last_ms "
                           "FROM crypto_candles GROUP BY symbol ORDER BY symbol")
    return {
        "clock": clock,
        "backfill": backfill,
        "settled": {"total": len(settled), "by_symbol": by_symbol, "with_candles": with_candles,
                    "first_close_ms": settled[-1]["close_ms"] if settled else None,
                    "last_close_ms": settled[0]["close_ms"] if settled else None},
        "rule_check": {"checked": rule_checked, "agree": rule_agree},
        "proxy_check": {"n": n_proxy, "agree": side_agree,
                        "mean_abs_pct": sum(diffs) / len(diffs) if diffs else None,
                        "max_abs_pct": max(diffs) if diffs else None},
        "crypto_history": crypto_hist,
    }
