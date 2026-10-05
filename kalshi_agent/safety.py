"""Kill switch and trading-mode gates.

Order submission to Kalshi requires an `OrderPermit`, which only `authorize_order`
can create. LIVE requires ALL of these gates; any failure means no order:

  G1  LIVE_TRADING_UNLOCKED code constant is True (it is False in this phase, so
      LIVE is impossible until a reviewed code change flips it)
  G2  settings.yaml says trading_mode: LIVE  (an env var alone is never enough)
  G3  env KALSHI_LIVE_CONFIRM equals LIVE_CONFIRM_PHRASE
  G4  a model version with status 'promoted_live' exists (passed validation)
  G5  Kalshi environment is "prod" with credentials present
  G6  kill switch not engaged and health says trading is allowed

DEMO requires DEMO_TRADING_UNLOCKED (False until Phase 7) and the demo host.
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .modes import TradingMode

LIVE_TRADING_UNLOCKED = False   # Phase 8+. Do not change without validated results.
DEMO_TRADING_UNLOCKED = False   # Phase 7.
LIVE_CONFIRM_PHRASE = "I-UNDERSTAND-THIS-USES-REAL-MONEY"

_PERMIT_SEAL = secrets.token_hex(16)


# ---------------------------------------------------------------- kill switch
@dataclass
class KillStatus:
    killed: bool
    sources: list[str] = field(default_factory=list)


def kill_status(db: Database | None, config_kill: bool, kill_file: Path,
                env: dict[str, str] | None = None) -> KillStatus:
    env = env if env is not None else dict(os.environ)
    sources = []
    if config_kill:
        sources.append("config")
    if kill_file.exists():
        sources.append("file")
    if str(env.get("KILL_SWITCH", "")).strip().lower() in ("1", "true", "yes", "on"):
        sources.append("env")
    if db is not None:
        try:
            if db.get_control("kill_switch", "off") == "on":
                sources.append("dashboard")
        except Exception:
            sources.append("db_unreadable")  # cannot confirm state -> treat as killed
    return KillStatus(killed=bool(sources), sources=sources)


def engage_kill(db: Database, by: str, reason: str) -> None:
    db.set_control("kill_switch", "on", by)
    db.log_risk("critical", "KILL_SWITCH_ENGAGED", f"Kill switch engaged by {by}: {reason}")
    db.log_event("safety", "critical", f"Kill switch engaged by {by}", {"reason": reason})


def release_kill(db: Database, by: str) -> None:
    """Releases only the dashboard/database kill. Config, file and env kills must be
    cleared at their source, on purpose."""
    db.set_control("kill_switch", "off", by)
    db.log_risk("warning", "KILL_SWITCH_RELEASED", f"Dashboard kill switch released by {by}")


# ---------------------------------------------------------------- order gates
@dataclass(frozen=True)
class OrderPermit:
    mode: TradingMode
    base_url: str
    seal: str


@dataclass
class GateResult:
    allowed: bool
    effective_mode: TradingMode
    checks: dict[str, bool]
    reasons: list[str]


def _has_promoted_model(db: Database | None) -> bool:
    if db is None:
        return False
    try:
        row = db.query_one("SELECT 1 AS ok FROM model_versions WHERE status='promoted_live' LIMIT 1")
        return bool(row)
    except Exception:
        return False


def evaluate_mode(requested: TradingMode, *, config_mode: TradingMode, kalshi_env: str,
                  has_credentials: bool, db: Database | None, killed: bool,
                  health_ok: bool, env: dict[str, str] | None = None) -> GateResult:
    """Work out which mode actually runs. Anything that fails a gate drops to WATCH."""
    env = env if env is not None else dict(os.environ)
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    if requested is TradingMode.LIVE:
        checks = {
            "G1_code_unlocked": LIVE_TRADING_UNLOCKED,
            "G2_config_file_says_live": config_mode is TradingMode.LIVE,
            "G3_confirm_phrase": env.get("KALSHI_LIVE_CONFIRM") == LIVE_CONFIRM_PHRASE,
            "G4_validated_model": _has_promoted_model(db),
            "G5_prod_with_credentials": kalshi_env == "prod" and has_credentials,
            "G6_not_killed_and_healthy": (not killed) and health_ok,
        }
    elif requested is TradingMode.DEMO:
        checks = {
            "demo_code_unlocked": DEMO_TRADING_UNLOCKED,
            "demo_environment": kalshi_env == "demo",
            "credentials": has_credentials,
            "not_killed_and_healthy": (not killed) and health_ok,
        }
    else:
        return GateResult(True, requested, {}, [])

    reasons = [name for name, ok in checks.items() if not ok]
    if reasons:
        return GateResult(False, TradingMode.WATCH, checks, reasons)
    return GateResult(True, requested, checks, [])


def authorize_order(gate: GateResult, base_url: str) -> OrderPermit:
    """Only path to an OrderPermit. Raises unless the gate passed for an order-sending mode."""
    mode = gate.effective_mode
    if not gate.allowed or not mode.sends_real_orders:
        raise PermissionError(f"Orders not permitted in mode {mode.value}: {gate.reasons}")
    if mode is TradingMode.LIVE and (not LIVE_TRADING_UNLOCKED or "demo" in base_url):
        raise PermissionError("LIVE trading is locked")
    if mode is TradingMode.DEMO and "demo" not in base_url:
        raise PermissionError("DEMO orders may only go to the Kalshi demo exchange")
    return OrderPermit(mode=mode, base_url=base_url, seal=_PERMIT_SEAL)


def verify_permit(permit: object, base_url: str) -> None:
    if not isinstance(permit, OrderPermit) or permit.seal != _PERMIT_SEAL:
        raise PermissionError("Invalid order permit")
    if permit.base_url != base_url:
        raise PermissionError("Order permit was issued for a different exchange")
    if permit.mode is TradingMode.LIVE and not LIVE_TRADING_UNLOCKED:
        raise PermissionError("LIVE trading is locked")
    if permit.mode is TradingMode.DEMO and not DEMO_TRADING_UNLOCKED:
        raise PermissionError("DEMO trading is locked")
