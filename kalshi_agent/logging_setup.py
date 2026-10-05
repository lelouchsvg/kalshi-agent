"""Logging: human-readable console + rotating JSON-lines file."""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
from pathlib import Path

_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"(KALSHI-ACCESS-SIGNATURE['\"]?\s*[:=]\s*['\"]?)[A-Za-z0-9+/=]+"),
    re.compile(r"(password['\"]?\s*[:=]\s*['\"]?)[^\s'\",]+", re.I),
]


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload)


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def setup_logging(log_dir: Path, name: str = "agent", level: int = logging.INFO) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_kalshi_configured", False):
        return
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(RedactingFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(console)
    fileh = logging.handlers.RotatingFileHandler(
        log_dir / f"{name}.jsonl", maxBytes=10_000_000, backupCount=5)
    fileh.setFormatter(JsonFormatter())
    root.addHandler(fileh)
    root._kalshi_configured = True  # type: ignore[attr-defined]
