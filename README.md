# Kalshi 15-minute crypto research & trading agent

A statistically disciplined research and execution system for Kalshi's 15-minute
BTC / ETH / SOL markets. Default mode is **PAPER**. Default action is **PASS**.
LIVE trading is locked in code.

New here? Read [START_HERE.md](START_HERE.md).

## Status: Phase 1 of 9

| Phase | What | State |
|---|---|---|
| 1 | Config, logging, database, Kalshi client, market discovery, dashboard, health, kill switch | **built, 40 tests passing** |
| 2 | WebSocket streams (order book, ticker, CF Benchmarks index), timestamp sync | next |
| 3 | Features, logistic-regression baseline, calibration, Brier/log-loss | needs collected data |
| 4 | Event-driven backtester, walk-forward | |
| 5 | Signal engine, risk engine, realistic paper execution, P&L | |
| 6 | LLM research agent (hypotheses → experiments → promote/reject) | |
| 7 | Kalshi demo exchange | locked (`DEMO_TRADING_UNLOCKED = False`) |
| 8 | Micro live | locked (`LIVE_TRADING_UNLOCKED = False`) |
| 9 | Autonomous operation | |

## Layout
```
kalshi_agent/
  config.py          settings.yaml + .env (secrets only from env)
  modes.py           WATCH / PAPER / BACKTEST / DEMO / LIVE
  safety.py          kill switch (4 sources) + 6 live gates + order permits
  kalshi/            auth (RSA-PSS / Ed25519), KalshiClient, parsed models
  crypto/provider.py CryptoDataProvider: Coinbase, Kraken (CF Benchmarks via WS in Phase 2)
  discovery.py       find open 15-minute markets, snapshots, order books
  fees.py            Kalshi fee model + executable EV after fees/slippage/fill odds
  health.py          API, feeds, freshness, heartbeat, disk/mem/cpu, model, kill
  service.py         always-on collector (systemd)
  state.py           read-only views for dashboard/CLI (real rows only)
  dashboard/         FastAPI + single-page dashboard
  cli.py             ./kalshi status | markets | health | kill | ...
  db/schema.sql      all tables (SQLite now, Postgres-ready)
deploy/              setup_vps.sh, systemd units, auto-update (test-gated), backups
docs/                Kalshi API notes, architecture decisions
tests/
```

## Safety model
- Orders need an `OrderPermit` that only `safety.authorize_order` can mint, and only for
  DEMO/LIVE after every gate passes. PAPER/WATCH/BACKTEST can never obtain one.
- LIVE gates: code unlock constant, config file says LIVE (env alone can't), a confirmation
  phrase in the server env, a model with status `promoted_live`, prod credentials, healthy
  and not killed. Any failure → WATCH, logged as a risk event.
- Kill switch: dashboard button, `./kalshi kill`, `kill_switch: true` in settings.yaml,
  `KILL_SWITCH=1` env, or a `data/KILL` file. Any one stops trading.
- Performance figures are computed only from recorded trades and are always labelled
  PAPER / DEMO / LIVE. Empty means empty.

## Commands (run on the server, or ask Claude)
`./kalshi status`, `markets`, `health`, `signals`, `trades`, `performance`, `research`,
`costs`, `start`, `stop`, `kill`, `unkill`, `discover`.
