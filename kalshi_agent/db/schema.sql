-- Kalshi agent schema v1.
-- Conventions (kept portable to PostgreSQL):
--   * every time is UTC epoch milliseconds in an INTEGER column ending in _ms
--   * prices are dollars (0..1 per contract) as REAL; quantities as REAL (Kalshi uses fixed-point)
--   * `mode` columns record WATCH/PAPER/BACKTEST/DEMO/LIVE so results never get mixed
--   * raw JSON payloads are kept in TEXT columns for reproducibility
-- Postgres migration: INTEGER PRIMARY KEY AUTOINCREMENT -> BIGSERIAL PRIMARY KEY, TEXT JSON -> JSONB.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS markets (
    ticker TEXT PRIMARY KEY,
    event_ticker TEXT,
    series_ticker TEXT,
    symbol TEXT,
    title TEXT,
    status TEXT,
    open_ms INTEGER,
    close_ms INTEGER,
    expiration_ms INTEGER,
    strike_type TEXT,
    floor_strike REAL,
    cap_strike REAL,
    result TEXT,
    rules_primary TEXT,
    first_seen_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_markets_symbol_close ON markets(symbol, close_ms);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,          -- when WE observed it (no look-ahead: features may only use rows with ts_ms <= decision time)
    status TEXT,
    yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    last_price REAL,
    spread REAL,
    volume REAL,
    open_interest REAL,
    seconds_to_close REAL,
    source TEXT                      -- rest | ws
);
CREATE INDEX IF NOT EXISTS idx_snap_ticker_ts ON market_snapshots(ticker, ts_ms);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    best_yes_bid REAL, best_yes_ask REAL,
    yes_depth REAL, no_depth REAL,   -- total contracts on visible levels
    imbalance REAL,                  -- (yes_depth - no_depth) / (yes_depth + no_depth)
    levels_json TEXT                 -- {"yes": [[price, qty], ...], "no": [...]} bids only, as Kalshi sends
);
CREATE INDEX IF NOT EXISTS idx_ob_ticker_ts ON orderbook_snapshots(ticker, ts_ms);

CREATE TABLE IF NOT EXISTS crypto_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,          -- provider timestamp of the price
    received_ms INTEGER NOT NULL,    -- when we received it
    price REAL NOT NULL,
    bid REAL, ask REAL, volume_24h REAL,
    provider TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_crypto_symbol_ts ON crypto_prices(symbol, ts_ms);

CREATE TABLE IF NOT EXISTS features (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    feature_set TEXT NOT NULL,
    values_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_features_ticker_ts ON features(ticker, ts_ms);

CREATE TABLE IF NOT EXISTS model_versions (
    version TEXT PRIMARY KEY,        -- MODEL_V001 ...
    created_ms INTEGER NOT NULL,
    model_type TEXT,
    features_json TEXT,
    hyperparams_json TEXT,
    train_start_ms INTEGER, train_end_ms INTEGER,
    valid_start_ms INTEGER, valid_end_ms INTEGER,
    test_start_ms INTEGER, test_end_ms INTEGER,
    dataset_hash TEXT,
    metrics_json TEXT,               -- brier, log loss, calibration error on held-out data
    status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | rejected | paper | demo | promoted_live | retired
    artifact_path TEXT,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    p_yes REAL NOT NULL CHECK (p_yes >= 0 AND p_yes <= 1),
    feature_id INTEGER
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    mode TEXT NOT NULL,
    model_version TEXT,
    side TEXT,                       -- yes | no | null
    p_yes REAL,
    market_price REAL,
    edge REAL,
    ev REAL,
    action TEXT NOT NULL,            -- BUY | PASS
    reason_code TEXT NOT NULL,       -- machine-readable
    explanation TEXT NOT NULL,       -- human-readable
    gates_json TEXT                  -- each of the trade gates and whether it passed
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts_ms);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_order_id TEXT UNIQUE NOT NULL,
    exchange_order_id TEXT,
    signal_id INTEGER,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    action TEXT NOT NULL,
    count REAL NOT NULL,
    limit_price REAL NOT NULL,
    status TEXT NOT NULL,            -- pending | resting | filled | partial | canceled | rejected
    created_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    reject_reason TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL,
    mode TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    count REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    is_taker INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    count REAL NOT NULL,
    avg_price REAL NOT NULL,
    fees REAL NOT NULL DEFAULT 0,
    opened_ms INTEGER NOT NULL,
    updated_ms INTEGER NOT NULL,
    UNIQUE (mode, ticker, side)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- one row per closed/settled position
    mode TEXT NOT NULL,
    ticker TEXT NOT NULL,
    symbol TEXT,
    side TEXT NOT NULL,
    count REAL NOT NULL,
    entry_ms INTEGER NOT NULL,
    exit_ms INTEGER,
    entry_price REAL NOT NULL,
    exit_price REAL,                 -- 1.0 / 0.0 at settlement
    fees REAL NOT NULL DEFAULT 0,
    pnl REAL,
    p_yes_at_entry REAL,
    market_price_at_entry REAL,
    edge_at_entry REAL,
    model_version TEXT,
    signal_id INTEGER,
    reason TEXT,
    result TEXT                      -- win | loss | open
);
CREATE INDEX IF NOT EXISTS idx_trades_mode_exit ON trades(mode, exit_ms);

CREATE TABLE IF NOT EXISTS pnl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mode TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    equity REAL NOT NULL,
    realized REAL NOT NULL,
    unrealized REAL NOT NULL,
    fees REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pnl_mode_ts ON pnl(mode, ts_ms);

CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    model_version TEXT,
    config_json TEXT,
    data_start_ms INTEGER, data_end_ms INTEGER,
    split TEXT,                      -- in_sample | walk_forward | out_of_sample
    metrics_json TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ms INTEGER NOT NULL,
    hypothesis TEXT NOT NULL,
    author TEXT NOT NULL,            -- human | research_agent
    baseline_version TEXT,
    candidate_version TEXT,
    backtest_run_ids TEXT,
    result_json TEXT,
    decision TEXT NOT NULL DEFAULT 'pending',  -- pending | promoted | rejected
    decision_reason TEXT,
    report_md TEXT
);

CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    mode TEXT,
    severity TEXT NOT NULL,          -- info | warning | critical
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS system_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    component TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_sysev_ts ON system_events(ts_ms);

CREATE TABLE IF NOT EXISTS heartbeats (
    component TEXT PRIMARY KEY,
    ts_ms INTEGER NOT NULL,
    info_json TEXT
);

CREATE TABLE IF NOT EXISTS control_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ms INTEGER NOT NULL,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS series_info (
    series_ticker TEXT PRIMARY KEY,
    symbol TEXT,
    title TEXT,
    frequency TEXT,
    fee_type TEXT,
    fee_multiplier REAL,
    updated_ms INTEGER NOT NULL,
    raw_json TEXT
);

-- ---------------------------------------------------------------------------
-- v2 (Phase 2): streaming data, settlement index proxy, history backfill.
-- Columns added to existing tables are applied by db.MIGRATIONS.

CREATE INDEX IF NOT EXISTS idx_crypto_received ON crypto_prices(received_ms);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON market_snapshots(ts_ms);

-- Proxy for the CF Benchmarks settlement index (median of several exchanges).
CREATE TABLE IF NOT EXISTS index_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    received_ms INTEGER NOT NULL,    -- when we computed it (the only clock features may use)
    value REAL NOT NULL,
    avg60 REAL,                      -- average over the previous 60 s, like the settlement rule
    n_sources INTEGER NOT NULL,
    sources_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_index_symbol_rx ON index_ticks(symbol, received_ms);

-- Public trades printed on Kalshi markets.
CREATE TABLE IF NOT EXISTS kalshi_trades (
    trade_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,          -- exchange time of the trade
    received_ms INTEGER NOT NULL,
    yes_price REAL,
    count REAL,
    taker_side TEXT
);
CREATE INDEX IF NOT EXISTS idx_ktrades_ticker_ts ON kalshi_trades(ticker, ts_ms);

-- One-minute Kalshi candles for settled markets (history for training and backtests).
CREATE TABLE IF NOT EXISTS market_candles (
    ticker TEXT NOT NULL,
    end_ms INTEGER NOT NULL,         -- the candle is only known after this moment
    yes_bid_close REAL, yes_ask_close REAL,
    price_close REAL,
    volume REAL, open_interest REAL,
    PRIMARY KEY (ticker, end_ms)
);

-- One-minute underlying candles from an exchange.
CREATE TABLE IF NOT EXISTS crypto_candles (
    symbol TEXT NOT NULL,
    provider TEXT NOT NULL,
    start_ms INTEGER NOT NULL,       -- known only after start_ms + 60 s
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, provider, start_ms)
);

CREATE TABLE IF NOT EXISTS backfill_log (
    ticker TEXT PRIMARY KEY,
    candles INTEGER NOT NULL,
    fetched_ms INTEGER NOT NULL,
    error TEXT
);
