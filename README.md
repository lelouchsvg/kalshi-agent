# Kalshi 15-minute crypto research & trading agent

A statistically disciplined research and execution system for Kalshi's 15-minute
BTC / ETH / SOL markets. Default mode is **PAPER**. Default action is **PASS**.
LIVE trading is locked in code.

New here? Read [START_HERE.md](START_HERE.md).

## Status: Phase 2 of 9

| Phase | What | State |
|---|---|---|
| 1 | Config, logging, database, Kalshi client, market discovery, dashboard, health, kill switch | built |
| 2 | Coinbase price stream, settlement-index stand-in, Kalshi trades, history backfill, clock check, point-in-time reads, Kalshi stream (with key) | **built, 63 tests passing** |
| 3 | Features, logistic-regression baseline, calibration, Brier/log-loss | next |
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
  crypto/provider.py CryptoDataProvider: Coinbase, Kraken (REST)
  crypto/index.py    settlement-index stand-in: median of Coinbase/Kraken/Bitstamp/Gemini + 60 s average
  stream/            stdlib WebSocket client, Coinbase ticker stream, Kalshi stream (needs API key)
  backfill.py        settled markets + 1-min Kalshi and crypto candles for the last N days
  clock.py           Mac clock vs exchange clock (NTP-style midpoint)
  timeseries.py      point-in-time reads: only rows that had ARRIVED by decision time
  discovery.py       find open 15-minute markets, snapshots, order books
  fees.py            Kalshi fee model + executable EV after fees/slippage/fill odds
  health.py          API, feeds, freshness, heartbeat, disk/mem/cpu, model, kill
  service.py         always-on collector (main loop + feed threads)
  state.py           read-only views for dashboard/CLI (real rows only)
  dashboard/         built-in web server (standard library) + single-page dashboard
  cli.py             ./kalshi status | markets | health | kill | ...
  db/schema.sql      all tables (SQLite now, Postgres-ready)
install_update.sh   installs a new version over ~/kalshi-agent, keeping data/ (backs it up first)
start.sh / stop.sh  local launcher: private Python in .runtime/, tests must pass, auto-restart
Start/Stop Kalshi Agent.command   double-click wrappers for macOS
deploy/              optional: VPS setup if you ever want it running 24/7 without the Mac
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

## Running
On the Mac: double-click `Start Kalshi Agent.command` (see START_HERE.md). Dashboard at http://127.0.0.1:8080.

## Commands (in Terminal, or ask Claude)
`./kalshi status`, `markets`, `health`, `signals`, `trades`, `performance`, `research`,
`costs`, `feeds`, `data`, `start`, `stop`, `kill`, `unkill`, `discover`.

## Data timing rules (no look-ahead)
Every row carries the time it arrived on this machine (`received_ms`, or `ts_ms` for rows we stamp
on arrival) as well as the source's own timestamp where there is one. Features and backtests read
through `timeseries.py`, which filters on arrival time; candles count only after their period ends.
The clock check flags the Mac drifting more than 1 s from an exchange clock.
