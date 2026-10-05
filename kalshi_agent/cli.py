"""Plain-English command interface:  ./kalshi <command>

  status       what the system is doing right now
  health       detailed health checks
  markets      live 15-minute markets
  signals      latest signals and why
  trades       recent trades
  performance  P&L by mode (PAPER / DEMO / LIVE kept separate)
  research     models, experiments, backtests
  costs        estimated monthly running cost
  feeds        live data feeds and the clock check
  data         past markets downloaded for learning, and data checks
  start        resume data collection
  stop         pause data collection
  kill         engage the emergency kill switch
  unkill       release the dashboard kill switch
  discover     scan Kalshi once and print what was found
  paper        open paper positions and readiness for real money
  model        the current model and how it scored on unseen markets
  backtest     retrain the model now and backtest it on unseen markets (SIMULATED)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import state
from .config import load_settings
from .db import open_db
from .safety import engage_kill, release_kill


def _t(ms):
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S") if ms else "—"


def _c(v):
    return "—" if v is None else f"{round(v * 100)}¢"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="kalshi", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="status")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args(argv)
    s = load_settings()
    db = open_db(s.db_path)
    cmd = a.command.lower()

    def out(obj, text):
        print(json.dumps(obj, indent=2, default=str) if a.json else text)

    if cmd == "status":
        st = state.status(db, s)
        h = st["health"] or {}
        lines = [
            f"Phase {st['phase']}: {st['phase_note']}",
            f"Mode:         {st['effective_mode']}" + (f" (requested {st['requested_mode']}, blocked by {', '.join(st['mode_blocked_by'])})" if st["mode_blocked_by"] else ""),
            f"Collector:    {'ONLINE' if st['online'] else 'OFFLINE'}{' (paused)' if st['collector_paused'] else ''}",
            f"Health:       {h.get('overall', 'not run yet')}  trading allowed: {h.get('trading_allowed', False)}",
            f"Kill switch:  {'ENGAGED via ' + ', '.join(st['kill_switch']['sources']) if st['kill_switch']['engaged'] else 'off'}",
            f"LIVE trading: {'unlocked' if st['live_trading_unlocked'] else 'locked'}",
            f"Kalshi env:   {st['kalshi_env']}   credentials: {'configured' if st['credentials_configured'] else 'not needed yet'}",
            f"Model:        {st['model_version']}",
        ]
        out(st, "\n".join(lines))
    elif cmd == "health":
        st = state.status(db, s)["health"]
        if not st:
            out({}, "Health check has not run yet (the collector runs it every 30 seconds).")
        else:
            out(st, f"Overall: {st['overall']}\n" + "\n".join(
                f"  [{c['status']:>8}] {c['name']:<24} {c['detail']}" for c in st["checks"]))
    elif cmd == "markets":
        ms = state.markets(db, s)
        text = "\n".join(
            f"{m['symbol']:<4} {m['ticker']:<32} strike {m['floor_strike'] or m['cap_strike'] or '—':<12} "
            f"yes {_c(m['yes_bid'])}/{_c(m['yes_ask'])}  spread {_c(m['spread'])}  "
            f"{int(m['seconds_to_close'] or 0)//60}m left  {m['signal']}" for m in ms) or "No live markets stored yet."
        out(ms, text)
    elif cmd == "signals":
        rows = state.recent_signals(db)
        out(rows, "\n".join(f"{_t(r['ts_ms'])} {r['ticker']} {r['action']} [{r['reason_code']}] {r['explanation']}"
                            for r in rows) or "No signals yet. The signal engine arrives in Phase 5; until then every decision is PASS.")
    elif cmd == "trades":
        rows = state.recent_trades(db)
        out(rows, "\n".join(f"{_t(r['entry_ms'])} [{r['mode']}] {r['ticker']} {r['side']} x{r['count']} "
                            f"@{_c(r['entry_price'])} pnl {r['pnl']}  {r['reason'] or ''}" for r in rows) or "No trades yet.")
    elif cmd in ("performance", "pnl"):
        perf = {m: state.performance(db, m) for m in ("PAPER", "DEMO", "LIVE")}
        lines = []
        for m, pr in perf.items():
            if not pr["has_data"]:
                lines.append(f"{m}: {pr['message']}")
            else:
                lines.append(f"{m}: total ${pr['total_pnl']:.2f} | today ${pr['daily_pnl']:.2f} | "
                             f"{pr['n_trades']} trades | win rate {pr['win_rate']:.1%} | max drawdown ${pr['max_drawdown']:.2f}")
        out(perf, "\n".join(lines))
    elif cmd == "research":
        r = state.research(db)
        out(r, f"Models: {len(r['models'])}  Experiments: {len(r['experiments'])}  Backtests: {len(r['backtests'])}\n"
               "The research agent arrives in Phase 6.")
    elif cmd == "costs":
        c = state.costs(s)
        out(c, "\n".join(f"  ${float(i['monthly_usd']):>6.2f}  {i['item']}" for i in c["items"]) +
            f"\n  ${c['total_monthly_usd']:>6.2f}  estimated total per month")
    elif cmd == "feeds":
        fs = state.feeds(db, s)
        clock = state.data_quality(db, s)["clock"]
        lines = [f"  [{f['state']:>7}] {f['label']:<30} " +
                 (f"{f['age_s']:.0f}s ago" if f["age_s"] is not None else "no data") +
                 (f"  {f['note']}" if f["state"] != "live" and f["note"] else "") for f in fs]
        lines.append(f"Clock: Mac is {clock['offset_ms'] / 1000:+.2f}s vs exchange" if clock else "Clock: not measured yet")
        out({"feeds": fs, "clock": clock}, "\n".join(lines))
    elif cmd == "data":
        q = state.data_quality(db, s)
        st = q["settled"]
        lines = [f"Settled markets saved: {st['total']}  (with minute prices: {st['with_candles']})"]
        lines += [f"  {sym}: YES {b['yes']}  NO {b['no']}" for sym, b in st["by_symbol"].items()]
        rc, pc = q["rule_check"], q["proxy_check"]
        if rc["checked"]:
            lines.append(f"Settlement rule check: {rc['agree']} of {rc['checked']} results re-derived correctly")
        lines.append(f"Index stand-in vs real settlement: off by {pc['mean_abs_pct']:.3f}% on average, same side "
                     f"{pc['agree']}/{pc['n']}" if pc["n"] else "Index stand-in vs real settlement: not measured yet")
        if q["backfill"]:
            lines.append(f"History download: {q['backfill'].get('phase')}")
        out(q, "\n".join(lines))
    elif cmd in ("start", "resume"):
        db.set_control("collector", "running", "cli")
        out({"ok": True}, "Data collection resumed.")
    elif cmd in ("stop", "pause"):
        db.set_control("collector", "paused", "cli")
        out({"ok": True}, "Data collection paused. (Trading is also stopped; nothing trades before Phase 5.)")
    elif cmd == "kill":
        engage_kill(db, "cli", "kill command")
        out({"ok": True}, "KILL SWITCH ENGAGED. No trading of any kind until released.")
    elif cmd == "unkill":
        release_kill(db, "cli")
        out({"ok": True}, "Dashboard/CLI kill switch released. Config, file or env kill switches stay until removed.")
    elif cmd == "discover":
        from .discovery import discover
        from .kalshi.client import build_client
        found = discover(build_client(s), db, s.symbols, dict(s.series))
        out({k: [m.ticker for m in v] for k, v in found.items()},
            "\n".join(f"{k}: {len(v)} open market(s) " + ", ".join(m.ticker for m in v[:3]) for k, v in found.items()))
    elif cmd == "paper":
        pos = state.open_positions(db, "PAPER")
        rd = state.live_readiness(db, s)
        lines = [f"Open paper positions: {len(pos)}"]
        lines += [f"  {p_['ticker']} {p_['side'].upper()} x{p_['count']:.0f} @ {_c(p_['entry_price'])}" for p_ in pos]
        lines.append("Readiness for real money (all must be green; LIVE stays locked in code regardless):")
        lines += [f"  [{'x' if c['ok'] else ' '}] {c['name']}: {c['value']}" for c in rd["criteria"]]
        out({"positions": pos, "readiness": rd}, "\n".join(lines))
    elif cmd == "model":
        mi = state.model_info(db, s)
        act = mi["active"]
        if not act:
            tr = mi["trainer"] or {}
            out(mi, "No approved model yet. " + (tr.get("message") or "Training starts once enough history is downloaded."))
        else:
            t = act["metrics"].get("test", {})
            b = act["metrics"].get("backtest", {})
            out(mi, f"{act['version']} ({act['status']}) trained on {act['metrics'].get('n_train_markets')} markets\n"
                    f"Unseen markets: model Brier {t.get('brier', 0):.4f} vs market {t.get('market_brier', 0):.4f} "
                    f"(lower is better)\nBacktest (SIMULATED): {b.get('n_trades')} trades, P&L ${b.get('pnl', 0):+.2f}")
    elif cmd in ("backtest", "train"):
        from .trainer import train_once
        r = train_once(db, s)
        if r["state"] != "trained":
            out(r, r["message"])
        else:
            m = r["metrics"]
            out(r, f"{r['version']}: {r['status']}. Unseen-market Brier {m['test']['brier']:.4f} vs market "
                   f"{m['test']['market_brier']:.4f}. SIMULATED backtest: {m['backtest']['n_trades']} trades, "
                   f"P&L ${m['backtest']['pnl']:+.2f}." + (f" Reasons: {'; '.join(m['reasons'])}" if m["reasons"] else ""))
    else:
        p.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
