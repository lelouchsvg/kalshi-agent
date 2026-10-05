# Kalshi API: what was verified (2026-10-05)

Source: docs.kalshi.com (pages fetched on this date). Re-verify before Phase 2 and Phase 7.

**Base URLs:** prod `https://api.elections.kalshi.com/trade-api/v2` (also `external-api.kalshi.com`),
demo `https://demo-api.kalshi.co/trade-api/v2`. WebSocket `wss://api.elections.kalshi.com/trade-api/ws/v2`,
demo `wss://demo-api.kalshi.co/trade-api/ws/v2`. WebSocket requires auth headers on the handshake.

**Auth:** headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`.
Sign `timestamp + METHOD + path` with the query string removed. RSA: PSS, SHA-256, MGF1-SHA256,
salt = digest length. Ed25519 also accepted.

**Prices changed to fixed-point:** prices are dollar strings in `*_dollars` fields (`"0.5600"`,
up to 4 decimals), quantities are strings in `*_fp` fields (`"10.00"`). Markets carry
`price_ranges` / `price_level_structure` (tick bands); snap order prices to the band step.
Legacy integer-cent fields are only a fallback in our parser.

**Markets:** `GET /markets` (filters: `series_ticker` with `mve_filter=exclude`, `status`,
`tickers`, close/settle time ranges; cursor pagination). Fields used: `yes_bid_dollars`,
`yes_ask_dollars`, `no_*`, `last_price_dollars`, `volume_fp`, `open_interest_fp`,
`open_time`, `close_time`, `floor_strike`, `cap_strike`, `strike_type`, `result`, `status`.

**Order book:** `GET /markets/{ticker}/orderbook?depth=N` → `orderbook_fp.yes_dollars` /
`no_dollars` as `[price, qty]` pairs. Bids only; YES ask = 1 − best NO bid.

**Orders (V2):** `POST /portfolio/events/orders` with `ticker`, `side` (`bid`/`ask` on the YES
leg), `count`, `price` (strings), `time_in_force` (`fill_or_kill` | `good_till_canceled` |
`immediate_or_cancel`), `self_trade_prevention_type`, optional `client_order_id`, `post_only`,
`reduce_only`, `cancel_order_on_pause`. Cancel: `DELETE /portfolio/events/orders/{order_id}`.
Legacy `/portfolio/orders` create is being deprecated.

**Portfolio:** `/portfolio/balance`, `/portfolio/positions`, `/portfolio/fills`, `/portfolio/settlements`.

**Historical:** `/historical/cutoff`, `/historical/markets`, `/historical/markets/{t}/candlesticks`,
`/historical/trades`. Settled markets move to the historical tier after a cutoff.
Candlesticks: `GET /series/{s}/markets/{t}/candlesticks`, `period_interval` 1, 60 or 1440 minutes.

**Rate limits:** token buckets; Basic tier 200 read / 100 write tokens per second, most
requests cost 10 tokens. 429 on excess. We cap ourselves at 5 requests/second.

**Fees:** taker ≈ `ceil_cent(0.07 × C × P × (1−P))` × series multiplier; makers free on most
series. Fees are reported to six decimals; we round up to the cent to stay conservative.
The series endpoint's fee fields are stored in `series_info` and should override defaults.

**SDK:** official packages are `kalshi_python_sync` / `kalshi_python_async` (old `kalshi-python`
deprecated). Version 3.2.0 on PyPI lacked the V2 order endpoints, so we use a thin
hand-written client.

**15-minute crypto markets:** series `KXBTC15M`, `KXETH15M`, `KXSOL15M` (from third-party
guides; the collector also auto-detects 15-minute crypto series if these return nothing).
Settlement reported as the 60-second average of the CF Benchmarks real-time index before
close, compared with the strike. **To confirm against each market's `rules_primary` text once
the server is collecting** (this build environment's network could not reach Kalshi directly).
