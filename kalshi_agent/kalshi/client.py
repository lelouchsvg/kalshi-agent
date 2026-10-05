"""KalshiClient: the only place in the app that talks to Kalshi's REST API.

Endpoints verified against docs.kalshi.com on 2026-10-05. The official pip SDK
(kalshi_python_sync 3.2.0 at that date) did not yet include the V2 order
endpoints, so this is a thin hand-written client instead.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Iterator

import requests

from ..safety import OrderPermit, verify_permit
from .auth import KalshiSigner
from .models import Market, Orderbook

log = logging.getLogger(__name__)


class KalshiAPIError(RuntimeError):
    def __init__(self, status: int, message: str, path: str):
        super().__init__(f"Kalshi API {status} on {path}: {message}")
        self.status = status
        self.path = path


class RateLimiter:
    """Simple spacing limiter; keeps us far below Kalshi's per-tier token budgets."""

    def __init__(self, per_second: float):
        self.min_interval = 1.0 / max(per_second, 0.1)
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self.min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class KalshiClient:
    def __init__(self, base_url: str, signer: KalshiSigner | None = None,
                 requests_per_second: float = 5, timeout: float = 10,
                 session: requests.Session | None = None, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.signer = signer
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "kalshi-agent/0.1"})
        self.limiter = RateLimiter(requests_per_second)

    @property
    def is_demo(self) -> bool:
        return "demo" in self.base_url

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, *, params: dict | None = None,
                 json_body: dict | None = None, auth: bool = False) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        backoff = 1.0
        for attempt in range(self.max_retries + 1):
            headers = {}
            if auth:
                if self.signer is None:
                    raise KalshiAPIError(401, "API credentials are not configured", path)
                headers.update(self.signer.headers(method, url))
            self.limiter.wait()
            try:
                resp = self.session.request(method, url, params=params, json=json_body,
                                            headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise KalshiAPIError(0, f"network error: {exc}", path) from exc
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.max_retries:
                    raise KalshiAPIError(resp.status_code, resp.text[:300], path)
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise KalshiAPIError(resp.status_code, resp.text[:300], path)
            return resp.json() if resp.content else {}
        raise KalshiAPIError(0, "exhausted retries", path)  # pragma: no cover

    def _paginate(self, path: str, key: str, params: dict, auth: bool = False,
                  max_pages: int = 50) -> Iterator[dict[str, Any]]:
        cursor = None
        for _ in range(max_pages):
            data = self._request("GET", path, params={**params, "cursor": cursor}, auth=auth)
            yield from data.get(key) or []
            cursor = data.get("cursor")
            if not cursor:
                return

    # ------------------------------------------------------------ public data
    def get_exchange_status(self) -> dict[str, Any]:
        return self._request("GET", "/exchange/status")

    def get_series(self, series_ticker: str) -> dict[str, Any]:
        return self._request("GET", f"/series/{series_ticker}").get("series", {})

    def list_series(self, category: str | None = None) -> list[dict[str, Any]]:
        return self._request("GET", "/series", params={"category": category}).get("series") or []

    def get_markets(self, *, series_ticker: str | None = None, event_ticker: str | None = None,
                    status: str | None = None, tickers: list[str] | None = None,
                    min_close_ts: int | None = None, max_close_ts: int | None = None,
                    limit: int = 200, max_pages: int = 10) -> list[Market]:
        params = {
            "series_ticker": series_ticker, "event_ticker": event_ticker, "status": status,
            "tickers": ",".join(tickers) if tickers else None,
            "min_close_ts": min_close_ts, "max_close_ts": max_close_ts, "limit": limit,
            # docs: filtering by series_ticker requires excluding multivariate markets
            "mve_filter": "exclude" if series_ticker else None,
        }
        return [Market.from_api(m) for m in self._paginate("/markets", "markets", params,
                                                           max_pages=max_pages)]

    def get_market(self, ticker: str) -> Market:
        return Market.from_api(self._request("GET", f"/markets/{ticker}")["market"])

    def get_orderbook(self, ticker: str, depth: int = 10) -> Orderbook:
        return Orderbook.from_api(ticker, self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}))

    def get_trades(self, ticker: str, min_ts: int | None = None, max_ts: int | None = None,
                   limit: int = 1000, max_pages: int = 20) -> list[dict[str, Any]]:
        return list(self._paginate("/markets/trades", "trades", {
            "ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": limit}, max_pages=max_pages))

    def get_candlesticks(self, series_ticker: str, ticker: str, start_ts: int, end_ts: int,
                         period_interval: int = 1) -> list[dict[str, Any]]:
        return self._request(
            "GET", f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        ).get("candlesticks") or []

    def get_historical_cutoff(self) -> dict[str, Any]:
        return self._request("GET", "/historical/cutoff")

    def get_historical_markets(self, **params: Any) -> list[dict[str, Any]]:
        return list(self._paginate("/historical/markets", "markets", params))

    # --------------------------------------------------------- authenticated
    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/portfolio/balance", auth=True)

    def get_positions(self, **params: Any) -> dict[str, Any]:
        return self._request("GET", "/portfolio/positions", params=params, auth=True)

    def get_fills(self, **params: Any) -> list[dict[str, Any]]:
        return list(self._paginate("/portfolio/fills", "fills", params, auth=True))

    def get_orders(self, **params: Any) -> list[dict[str, Any]]:
        return list(self._paginate("/portfolio/orders", "orders", params, auth=True))

    # ---------------------------------------------------------------- orders
    def create_order(self, permit: OrderPermit, *, ticker: str, outcome: str, count: float,
                     limit_price: float, client_order_id: str,
                     time_in_force: str = "immediate_or_cancel", post_only: bool = False,
                     reduce_only: bool = False) -> dict[str, Any]:
        """Buy `count` contracts of `outcome` ("yes"/"no") at up to `limit_price` dollars.

        V2 orders are expressed on the YES leg: buying YES is a `bid` at p; buying NO
        at q is an `ask` on YES at (1 - q). Requires a permit from safety.authorize_order.
        """
        verify_permit(permit, self.base_url)
        outcome = outcome.lower()
        if outcome not in ("yes", "no"):
            raise ValueError("outcome must be 'yes' or 'no'")
        if not 0 < limit_price < 1:
            raise ValueError("limit_price must be between 0 and 1 dollars")
        side, yes_price = ("bid", limit_price) if outcome == "yes" else ("ask", 1.0 - limit_price)
        body = {
            "ticker": ticker,
            "side": side,
            "count": f"{count:.2f}",
            "price": f"{yes_price:.4f}",
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "client_order_id": client_order_id,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "cancel_order_on_pause": True,
        }
        log.warning("Submitting %s order to %s: %s", permit.mode.value, self.base_url,
                    {k: body[k] for k in ("ticker", "side", "count", "price")})
        return self._request("POST", "/portfolio/events/orders", json_body=body, auth=True)

    def cancel_order(self, permit: OrderPermit, order_id: str, market_ticker: str | None = None) -> dict[str, Any]:
        verify_permit(permit, self.base_url)
        return self._request("DELETE", f"/portfolio/events/orders/{order_id}",
                             params={"market_ticker": market_ticker}, auth=True)


def build_client(settings) -> KalshiClient:
    signer = None
    if settings.has_kalshi_credentials:
        signer = KalshiSigner(settings.kalshi_api_key_id, settings.kalshi_private_key_path)
    return KalshiClient(settings.kalshi_base_url, signer=signer,
                        requests_per_second=settings.max_requests_per_second)
