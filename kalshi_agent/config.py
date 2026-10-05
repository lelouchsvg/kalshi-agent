"""Configuration loading.

Non-secret settings come from config/settings.yaml. Secrets come only from the
environment (loaded from a .env file on the server, which is never committed).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .modes import TradingMode

ROOT = Path(os.environ.get("KALSHI_AGENT_HOME", Path(__file__).resolve().parent.parent))

KALSHI_BASE_URLS = {
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
}
KALSHI_WS_URLS = {
    "prod": "wss://api.elections.kalshi.com/trade-api/ws/v2",
    "demo": "wss://demo-api.kalshi.co/trade-api/ws/v2",
}


@dataclass
class RiskLimits:
    max_position_size: int = 5
    max_order_size: int = 5
    max_contract_exposure: int = 20
    max_daily_loss: float = 25.0
    max_drawdown: float = 50.0
    max_open_positions: int = 3
    max_consecutive_losses: int = 5
    max_market_exposure: float = 10.0
    max_slippage: float = 0.02
    min_edge: float = 0.04
    max_spread: float = 0.06


@dataclass
class Settings:
    trading_mode: TradingMode = TradingMode.PAPER
    kill_switch: bool = False
    kalshi_env: str = "prod"
    symbols: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    series: dict[str, str] = field(default_factory=lambda: {
        "BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M"})
    risk: RiskLimits = field(default_factory=RiskLimits)
    paper_starting_balance: float = 1000.0
    model_version: str = "NONE"
    discovery_interval_s: int = 60
    snapshot_interval_s: int = 10
    crypto_interval_s: int = 5
    health_interval_s: int = 30
    orderbook_depth: int = 10
    trades_interval_s: int = 15
    index_interval_s: int = 5
    clock_interval_s: int = 600
    max_requests_per_second: float = 5
    coinbase_stream: bool = True
    kalshi_stream: bool = True          # only runs when API credentials exist
    backfill_days: int = 14
    orderbook_levels_keep_days: int = 14
    max_clock_offset_ms: float = 1000
    max_data_age_s: int = 60
    max_heartbeat_age_s: int = 90
    max_disk_pct: float = 90
    max_mem_pct: float = 90
    crypto_provider: str = "coinbase"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8080
    costs: list[dict[str, Any]] = field(default_factory=list)
    db_path: Path = ROOT / "data" / "kalshi_agent.db"
    log_dir: Path = ROOT / "logs"
    kill_file: Path = ROOT / "data" / "KILL"

    # Secrets: read from env only. Never logged, never shown on the dashboard.
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    dashboard_password: str | None = None

    @property
    def kalshi_base_url(self) -> str:
        return KALSHI_BASE_URLS[self.kalshi_env]

    @property
    def kalshi_ws_url(self) -> str:
        return KALSHI_WS_URLS[self.kalshi_env]

    @property
    def has_kalshi_credentials(self) -> bool:
        return bool(self.kalshi_api_key_id and self.kalshi_private_key_path)

    def public_view(self) -> dict[str, Any]:
        """Settings safe to show on the dashboard (no secrets)."""
        return {
            "trading_mode": self.trading_mode.value,
            "kill_switch_config": self.kill_switch,
            "kalshi_env": self.kalshi_env,
            "symbols": self.symbols,
            "series": self.series,
            "risk": vars(self.risk),
            "paper_starting_balance": self.paper_starting_balance,
            "model_version": self.model_version,
            "crypto_provider": self.crypto_provider,
            "credentials_configured": self.has_kalshi_credentials,
        }


def load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_settings(config_path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    if env is None:
        load_dotenv(ROOT / ".env")
        env = dict(os.environ)
    config_path = config_path or Path(env.get("KALSHI_AGENT_CONFIG", ROOT / "config" / "settings.yaml"))
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text()) or {}

    s = Settings()
    s.trading_mode = TradingMode.parse(env.get("TRADING_MODE") or raw.get("trading_mode"))
    s.kill_switch = bool(raw.get("kill_switch", False))
    s.kalshi_env = str(raw.get("kalshi_env", "prod")).lower()
    if s.kalshi_env not in KALSHI_BASE_URLS:
        s.kalshi_env = "prod"
    s.symbols = [str(x).upper() for x in raw.get("symbols", s.symbols)]
    s.series = {str(k).upper(): str(v) for k, v in (raw.get("series") or s.series).items()}
    risk = raw.get("risk") or {}
    s.risk = RiskLimits(**{k: v for k, v in risk.items() if k in RiskLimits.__dataclass_fields__})
    s.paper_starting_balance = float((raw.get("paper") or {}).get("starting_balance", 1000.0))
    s.model_version = str((raw.get("model") or {}).get("version", "NONE"))
    col = raw.get("collection") or {}
    for key in ("discovery_interval_s", "snapshot_interval_s", "crypto_interval_s",
                "health_interval_s", "orderbook_depth", "trades_interval_s", "index_interval_s",
                "clock_interval_s", "backfill_days", "orderbook_levels_keep_days"):
        if key in col:
            setattr(s, key, int(col[key]))
    if "max_requests_per_second" in col:
        s.max_requests_per_second = float(col["max_requests_per_second"])
    for key in ("coinbase_stream", "kalshi_stream"):
        if key in col:
            setattr(s, key, bool(col[key]))
    h = raw.get("health") or {}
    for key in ("max_data_age_s", "max_heartbeat_age_s"):
        if key in h:
            setattr(s, key, int(h[key]))
    for key in ("max_disk_pct", "max_mem_pct", "max_clock_offset_ms"):
        if key in h:
            setattr(s, key, float(h[key]))
    s.crypto_provider = str(raw.get("crypto_provider", "coinbase")).lower()
    d = raw.get("dashboard") or {}
    s.dashboard_host = str(d.get("host", s.dashboard_host))
    s.dashboard_port = int(d.get("port", s.dashboard_port))
    s.costs = list(raw.get("costs") or [])
    if env.get("KALSHI_AGENT_DB"):
        s.db_path = Path(env["KALSHI_AGENT_DB"])

    s.kalshi_api_key_id = env.get("KALSHI_API_KEY_ID") or None
    s.kalshi_private_key_path = env.get("KALSHI_PRIVATE_KEY_PATH") or None
    s.dashboard_password = env.get("DASHBOARD_PASSWORD") or None
    return s
