"""Health monitor. If anything critical fails, trading is not allowed."""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from typing import Callable

from .db import Database, now_ms

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

OK, WARN, CRIT = "ok", "warning", "critical"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    value: float | None = None


@dataclass
class HealthReport:
    ts_ms: int
    overall: str
    trading_allowed: bool
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"ts_ms": self.ts_ms, "overall": self.overall, "trading_allowed": self.trading_allowed,
                "checks": [asdict(c) for c in self.checks]}


def _age_check(db: Database, name: str, sql: str, max_age_s: float, critical: bool = True) -> Check:
    row = db.query_one(sql)
    ts = row and row.get("ts")
    if not ts:
        return Check(name, CRIT if critical else WARN, "no data yet")
    age = (now_ms() - ts) / 1000
    status = OK if age <= max_age_s else (CRIT if critical else WARN)
    return Check(name, status, f"{age:.0f}s old (limit {max_age_s:.0f}s)", age)


def _clock_check(db: Database, settings) -> Check:
    raw = db.get_control("clock")
    if not raw:
        return Check("clock", WARN, "not measured yet")
    c = json.loads(raw)
    off = float(c["offset_ms"])
    limit = getattr(settings, "max_clock_offset_ms", 1000)
    status = OK if abs(off) <= limit else (WARN if abs(off) <= 5 * limit else CRIT)
    return Check("clock", status, f"Mac clock {off / 1000:+.2f}s vs exchange (limit ±{limit / 1000:.1f}s)", off)


def _feed_checks(db: Database) -> list[Check]:
    """Streaming feeds are helpful, not required (REST polling covers gaps), so they warn."""
    out = []
    for name, label, max_age in (("coinbase_ws", "price_stream", 30), ("index_proxy", "index_proxy", 30)):
        row = db.query_one("SELECT info_json FROM heartbeats WHERE component=?", (f"feed:{name}",))
        if not row:
            continue
        info = json.loads(row["info_json"] or "{}")
        last = info.get("last_msg_ms")
        age = (now_ms() - last) / 1000 if last else None
        if age is not None and age <= max_age:
            out.append(Check(label, OK, f"live, last update {age:.0f}s ago", age))
        else:
            why = info.get("last_error") or "no recent data"
            out.append(Check(label, WARN, f"{why}; using backup polling"[:200], age))
    return out


def run_health(db: Database, settings, *, api_probe: Callable[[], object] | None = None,
               crypto_probe: Callable[[], object] | None = None, killed: bool = False) -> HealthReport:
    checks: list[Check] = []

    try:
        db.query_one("SELECT 1")
        db.heartbeat("health", {})
        checks.append(Check("database", OK, f"{settings.db_path.name} reachable"))
    except Exception as exc:
        checks.append(Check("database", CRIT, f"database error: {exc}"))

    if api_probe is not None:
        try:
            status = api_probe()
            active = status.get("trading_active") if isinstance(status, dict) else None
            detail = "reachable" + ("" if active is None else f", exchange trading_active={active}")
            checks.append(Check("kalshi_api", OK if active is not False else WARN, detail))
        except Exception as exc:
            checks.append(Check("kalshi_api", CRIT, f"unreachable: {exc}"[:200]))

    if crypto_probe is not None:
        try:
            crypto_probe()
            checks.append(Check("crypto_feed", OK, f"{settings.crypto_provider} reachable"))
        except Exception as exc:
            checks.append(Check("crypto_feed", CRIT, f"unreachable: {exc}"[:200]))

    checks.append(_age_check(db, "market_data_freshness",
                             "SELECT MAX(ts_ms) AS ts FROM market_snapshots", settings.max_data_age_s))
    checks.append(_age_check(db, "crypto_data_freshness",
                             "SELECT MAX(received_ms) AS ts FROM crypto_prices", settings.max_data_age_s))
    checks.append(_age_check(db, "collector_heartbeat",
                             "SELECT ts_ms AS ts FROM heartbeats WHERE component='collector'",
                             settings.max_heartbeat_age_s))

    checks.append(_clock_check(db, settings))
    checks.extend(_feed_checks(db))

    model = settings.model_version
    if model in ("", "NONE"):
        checks.append(Check("model", WARN, "no model trained yet: every signal will be PASS"))
    else:
        row = db.query_one("SELECT status FROM model_versions WHERE version=?", (model,))
        checks.append(Check("model", OK if row else CRIT,
                            f"{model} ({row['status']})" if row else f"{model} not found in registry"))

    disk = shutil.disk_usage(settings.db_path.parent if settings.db_path.parent.exists() else "/")
    disk_pct = disk.used / disk.total * 100
    checks.append(Check("disk", OK if disk_pct < settings.max_disk_pct else CRIT,
                        f"{disk_pct:.0f}% used", disk_pct))
    if psutil is not None:
        mem = psutil.virtual_memory().percent
        checks.append(Check("memory", OK if mem < settings.max_mem_pct else CRIT, f"{mem:.0f}% used", mem))
        cpu = psutil.cpu_percent(interval=0.2)
        checks.append(Check("cpu", OK if cpu < 95 else WARN, f"{cpu:.0f}% busy", cpu))

    checks.append(Check("kill_switch", CRIT if killed else OK, "ENGAGED" if killed else "off"))

    statuses = {c.status for c in checks}
    overall = CRIT if CRIT in statuses else WARN if WARN in statuses else OK
    # "model" warning alone still blocks trading, because there is nothing to trade on.
    model_ok = next((c.status == OK for c in checks if c.name == "model"), False)
    report = HealthReport(now_ms(), overall, overall != CRIT and model_ok and not killed, checks)
    db.upsert("control_state", {"key": "last_health", "value": json.dumps(report.to_dict()),
                                "updated_ms": report.ts_ms, "updated_by": "health"}, "key")
    return report
