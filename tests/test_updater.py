import io
import json
import stat
import zipfile

import pytest

from kalshi_agent import updater
from kalshi_agent.credentials import CredentialError

SHA = "a" * 40


def make_zip(live="False", demo="False"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = "lelouchsvg-kalshi-agent-aaaaaaa/"
        z.writestr(top + "kalshi_agent/__init__.py", "")
        z.writestr(top + "kalshi_agent/safety.py",
                   f"LIVE_TRADING_UNLOCKED = {live}   # note\nDEMO_TRADING_UNLOCKED = {demo}\n")
        z.writestr(top + "kalshi_agent/new_module.py", "X = 1\n")
        z.writestr(top + "requirements.txt", "requests\n")
        z.writestr(top + ".env", "SHOULD_NOT_COPY=1\n")
        info = zipfile.ZipInfo(top + "start.sh")
        info.external_attr = 0o755 << 16
        z.writestr(info, "#!/bin/bash\n")
    return buf.getvalue()


class Resp:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code, self._body, self.content = status, body or {}, content

    def json(self):
        return self._body


class FakeGitHub:
    def __init__(self, sha=SHA, zip_bytes=None, status=200):
        self.sha, self.zip, self.status, self.calls = sha, zip_bytes or make_zip(), status, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        if self.status != 200:
            return Resp(self.status)
        if "/zipball/" in url:
            return Resp(content=self.zip)
        if "/commits/" in url:
            return Resp(body={"sha": self.sha})
        return Resp(body={"full_name": "lelouchsvg/kalshi-agent"})


@pytest.fixture
def root(tmp_path, settings):
    r = tmp_path / "agent"
    (r / "kalshi_agent").mkdir(parents=True)
    (r / "kalshi_agent" / "safety.py").write_text("LIVE_TRADING_UNLOCKED = False\n")
    (r / "data").mkdir()
    (r / "logs").mkdir()
    (r / "requirements.txt").write_text("requests\n")
    (r / ".env").write_text("KALSHI_API_KEY_ID=keep\n")
    (r / "secrets").mkdir()
    (r / "secrets" / "github.token").write_text("github_pat_" + "x" * 30 + "\n")
    settings.db_path = r / "data" / "kalshi_agent.db"
    return r


def test_installs_new_version_after_tests_pass(root, settings, db):
    restarted = []
    gh = FakeGitHub()
    out = updater.check(settings, root, http=gh, tests=lambda r, n: (True, "80 passed"),
                        do_restart=restarted.append)
    assert out["state"] == "installed" and out["installed"] == SHA
    assert restarted == [root]
    assert (root / "kalshi_agent" / "new_module.py").exists()
    assert (root / ".env").read_text() == "KALSHI_API_KEY_ID=keep\n"   # never overwritten
    assert updater.installed_revision(root) == SHA
    assert stat.S_IMODE((root / "start.sh").stat().st_mode) == 0o755
    assert (root / "data" / "backups" / "code_prev" / "kalshi_agent" / "safety.py").exists()
    assert not (root / "data" / "update_staging").exists()
    assert gh.calls[0][1]["Authorization"].startswith("Bearer github_pat_")
    # next check: nothing new
    again = updater.check(settings, root, http=gh, tests=lambda r, n: pytest.fail("no retest"),
                          do_restart=restarted.append)
    assert again["state"] == "current" and len(restarted) == 1


@pytest.mark.parametrize("live,demo", [("True", "False"), ("False", "True"), ("1", "False")])
def test_refuses_updates_that_unlock_trading(root, settings, live, demo):
    gh = FakeGitHub(zip_bytes=make_zip(live, demo))
    out = updater.check(settings, root, http=gh, tests=lambda r, n: pytest.fail("must not test"),
                        do_restart=lambda r: pytest.fail("must not restart"))
    assert out["state"] == "error" and "manual install" in out["message"]
    assert not (root / "kalshi_agent" / "new_module.py").exists()
    # remembered: the same version is not downloaded again
    n = len(gh.calls)
    assert updater.check(settings, root, http=gh)["state"] == "rejected"
    assert len(gh.calls) == n + 1


def test_failed_tests_keep_current_version(root, settings):
    out = updater.check(settings, root, http=FakeGitHub(), tests=lambda r, n: (False, "1 failed"),
                        do_restart=lambda r: pytest.fail("must not restart"))
    assert out["state"] == "error" and "1 failed" in out["message"]
    assert not (root / "kalshi_agent" / "new_module.py").exists()
    assert not (root / "data" / "update_staging").exists()   # never left behind for pytest to trip on


def test_token_problems_explained(root, settings):
    (root / "secrets" / "github.token").unlink()
    out = updater.check(settings, root, http=FakeGitHub(status=404))
    assert out["state"] == "error" and "token" in out["message"]
    settings.auto_update = False
    assert updater.check(settings, root, http=FakeGitHub())["state"] == "off"


def test_save_token(root):
    with pytest.raises(CredentialError):
        updater.save_token(root, "hello", "o/r", http=FakeGitHub())
    with pytest.raises(CredentialError):
        updater.save_token(root, "github_pat_" + "y" * 30, "o/r", http=FakeGitHub(status=401))
    updater.save_token(root, " github_pat_" + "z" * 30 + " ", "o/r", http=FakeGitHub())
    p = updater.token_path(root)
    assert p.read_text().strip() == "github_pat_" + "z" * 30
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_dashboard_update_endpoints(root, settings, db, monkeypatch):
    from kalshi_agent.dashboard.server import DashboardApp
    app = DashboardApp(settings, db, root=root)
    db.set_control("updater", json.dumps({"state": "current", "message": "Up to date."}), "t")
    st = json.loads(app.handle("GET", "/api/updates", None)[2])
    assert st["state"] == "current" and st["has_token"] and "github_pat" not in json.dumps(st)
    app.handle("POST", "/api/updates/check", None, b"", {})
    assert (root / "data" / "update_now").exists()
    monkeypatch.setattr(updater, "save_token", lambda *a, **k: (_ for _ in ()).throw(CredentialError("nope")))
    bad = json.loads(app.handle("POST", "/api/updates/token", None, b'{"token":"x"}', {})[2])
    assert bad == {"ok": False, "error": "nope"}


def test_demo_unlock_may_stay_but_never_appear_or_spread(root, settings):
    (root / "kalshi_agent" / "safety.py").write_text("LIVE_TRADING_UNLOCKED = False\nDEMO_TRADING_UNLOCKED = True\n")
    out = updater.check(settings, root, http=FakeGitHub(zip_bytes=make_zip("False", "True")),
                        tests=lambda r, n: (True, "ok"), do_restart=lambda r: None)
    assert out["state"] == "installed"
    (root / "REVISION").write_text("b" * 40)
    out = updater.check(settings, root, http=FakeGitHub(sha="c" * 40, zip_bytes=make_zip("True", "True")),
                        tests=lambda r, n: pytest.fail("must not test"), do_restart=lambda r: None)
    assert out["state"] == "error" and "LIVE_TRADING_UNLOCKED" in out["message"]
