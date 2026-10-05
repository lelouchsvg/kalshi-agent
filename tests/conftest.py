import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kalshi_agent.config import Settings  # noqa: E402
from kalshi_agent.db import open_db  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("TRADING_MODE", "KILL_SWITCH", "KALSHI_LIVE_CONFIRM", "KALSHI_API_KEY_ID",
              "KALSHI_PRIVATE_KEY_PATH", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def settings(tmp_path):
    s = Settings()
    s.db_path = tmp_path / "test.db"
    s.kill_file = tmp_path / "KILL"
    s.log_dir = tmp_path / "logs"
    s.costs = [{"item": "VPS", "monthly_usd": 5.0}]
    return s


@pytest.fixture
def db(settings):
    d = open_db(settings.db_path)
    yield d
    d.close()
