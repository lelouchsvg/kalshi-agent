"""Automatic updates from the private GitHub repo.

Run once per check by update_loop.sh (started by start.sh). Each check:
  1. asks GitHub for the newest commit on the update branch,
  2. downloads it into data/update_staging/ (needs a read-only token for a private repo),
  3. refuses it if it would unlock DEMO or LIVE trading (those always need a manual install),
  4. installs any new libraries and runs the full test suite against the new code,
  5. only if every test passes: backs up the database and current code, copies the new
     code in (never data/, logs/, .env, secrets/ or the private Python) and restarts.
If the restarted agent fails to start, restart_after_update.sh puts the old code back.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

import requests

from .config import ROOT, Settings, load_settings
from .db import open_db

API = "https://api.github.com"
SKIP = {"data", "logs", ".runtime", ".venv", ".env", ".git", "secrets", "__pycache__", ".pytest_cache"}
TOKEN_RE = re.compile(r"^(github_pat_[A-Za-z0-9_]{20,255}|gh[pousr]_[A-Za-z0-9]{20,255})$")
LOCKS = ("LIVE_TRADING_UNLOCKED", "DEMO_TRADING_UNLOCKED")


class UpdateError(RuntimeError):
    """A reason shown on the dashboard."""


def token_path(root: Path) -> Path:
    return root / "secrets" / "github.token"


def read_token(root: Path) -> str | None:
    p = token_path(root)
    try:
        t = p.read_text().strip()
    except OSError:
        return None
    return t or None


def installed_revision(root: Path) -> str | None:
    try:
        rev = (root / "REVISION").read_text().strip()
    except OSError:
        return None
    return rev if re.fullmatch(r"[0-9a-f]{40}", rev) else None


def _headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "kalshi-agent-updater",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _explain(resp: requests.Response, has_token: bool) -> UpdateError:
    if resp.status_code in (401, 403) and has_token:
        return UpdateError("GitHub refused the token. Create a new read-only token and save it again.")
    if resp.status_code in (401, 403, 404):
        return UpdateError("Can't see the GitHub repo. Save a read-only GitHub token on the dashboard."
                           if not has_token else "The token can't read this repo. Give it access to kalshi-agent.")
    return UpdateError(f"GitHub answered {resp.status_code}.")


def latest_commit(repo: str, branch: str, token: str | None, http=requests) -> str:
    r = http.get(f"{API}/repos/{repo}/commits/{branch}", headers=_headers(token), timeout=20)
    if r.status_code != 200:
        raise _explain(r, bool(token))
    return r.json()["sha"]


def download(repo: str, sha: str, token: str | None, dest: Path, http=requests) -> Path:
    r = http.get(f"{API}/repos/{repo}/zipball/{sha}", headers=_headers(token), timeout=120)
    if r.status_code != 200:
        raise _explain(r, bool(token))
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for info in z.infolist():
            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                raise UpdateError("Downloaded update has unsafe file paths; refusing it.")
            z.extract(info, dest)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(target, mode)
    tops = [p for p in dest.iterdir() if p.is_dir()]
    if len(tops) != 1 or not (tops[0] / "kalshi_agent").is_dir():
        raise UpdateError("Downloaded update doesn't look like the agent.")
    new = tops[0]
    (new / "REVISION").write_text(sha + "\n")      # GitHub zipballs may not expand it
    return new


def _lock_values(root: Path) -> dict[str, list[str]]:
    try:
        text = (root / "kalshi_agent" / "safety.py").read_text()
    except OSError:
        text = ""
    return {name: re.findall(rf"^{name}\s*=\s*(\w+)", text, re.M) for name in LOCKS}


def check_locks(new: Path, current: Path | None = None) -> None:
    """An automatic update may never unlock anything: LIVE must stay locked, and DEMO
    may only stay as it is now (unlocking it takes a manual install)."""
    now = _lock_values(current) if current else {}
    for name, found in _lock_values(new).items():
        allowed = [["False"]] + ([["True"]] if name != "LIVE_TRADING_UNLOCKED" and now.get(name) == ["True"] else [])
        if found not in allowed:
            raise UpdateError(f"This update changes {name}. It needs a manual install and a review "
                              "with you, so it was not installed automatically.")


def _req_hash(d: Path) -> str:
    return "".join(p.read_text() for p in sorted(d.glob("requirements*.txt")))


def run_tests(root: Path, new: Path, run: Callable[..., Any] = subprocess.run) -> tuple[bool, str]:
    py = root / ".venv" / "bin" / "python"
    py = py if py.exists() else Path(sys.executable)
    if _req_hash(new) != _req_hash(root):
        uv = root / ".runtime" / "bin" / "uv"
        cmd = ([str(uv), "pip", "install", "--python", str(py)] if uv.exists()
               else [str(py), "-m", "pip", "install"]) + ["-r", str(new / "requirements-dev.txt")]
        res = run(cmd, cwd=new, capture_output=True, text=True, timeout=900)
        if res.returncode != 0:
            return False, "Couldn't install the new libraries: " + (res.stderr or res.stdout)[-300:]
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KALSHI_", "DASHBOARD_"))}
    env["KALSHI_AGENT_HOME"] = str(new)
    res = run([str(py), "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=new, env=env,
              capture_output=True, text=True, timeout=1800)
    tail = (res.stdout or "").strip().splitlines()[-1:] or [""]
    return res.returncode == 0, tail[0][:300]


def backup_db(db_path: Path, backups: Path, keep: int = 3) -> None:
    if not db_path.exists():
        return
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"kalshi_agent.db.{time.strftime('%Y%m%d-%H%M%S')}"
    src = sqlite3.connect(str(db_path))
    try:
        out = sqlite3.connect(str(dest))
        with out:
            src.backup(out)
        out.close()
    finally:
        src.close()
    for old in sorted(backups.glob("kalshi_agent.db.*"), reverse=True)[keep:]:
        old.unlink(missing_ok=True)


def install(root: Path, new: Path) -> None:
    prev = root / "data" / "backups" / "code_prev"
    if prev.exists():
        shutil.rmtree(prev)
    prev.mkdir(parents=True)
    for item in root.iterdir():
        if item.name in SKIP:
            continue
        (shutil.copytree if item.is_dir() else shutil.copy2)(item, prev / item.name)
    for item in new.iterdir():
        if item.name in SKIP:
            continue
        dest = root / item.name
        if item.is_dir():
            shutil.copytree(item, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dest)


def restart(root: Path) -> None:
    subprocess.Popen(["/bin/bash", str(root / "restart_after_update.sh")], cwd=root,
                     stdin=subprocess.DEVNULL, stdout=open(root / "logs" / "update.log", "a"),
                     stderr=subprocess.STDOUT, start_new_session=True)


def check(settings: Settings, root: Path = ROOT, http=requests, tests=run_tests,
          do_restart: Callable[[Path], None] = restart) -> dict[str, Any]:
    """One update check. Returns the status that is also saved for the dashboard."""
    status: dict[str, Any] = {"checked_ms": int(time.time() * 1000), "installed": installed_revision(root),
                              "enabled": settings.auto_update, "has_token": bool(read_token(root))}
    if not settings.auto_update:
        return {**status, "state": "off", "message": "Automatic updates are turned off in settings."}
    try:
        token = read_token(root)
        sha = latest_commit(settings.update_repo, settings.update_branch, token, http)
        status["latest"] = sha
        if sha == status["installed"]:
            return {**status, "state": "current", "message": "Up to date."}
        rejected = _rejected(root)
        if rejected.get("sha") == sha:
            return {**status, "state": "rejected", "message": rejected.get("message", "")}
        new = download(settings.update_repo, sha, token, root / "data" / "update_staging", http)
        check_locks(new, root)
        ok, summary = tests(root, new)
        if not ok:
            raise UpdateError(f"New version failed its safety tests ({summary}); kept the current version.")
        backup_db(settings.db_path, root / "data" / "backups")
        install(root, new)
        shutil.rmtree(root / "data" / "update_staging", ignore_errors=True)
        do_restart(root)
        return {**status, "state": "installed", "installed": sha,
                "message": f"Installed version {sha[:7]} ({summary}). Restarting."}
    except UpdateError as exc:
        shutil.rmtree(root / "data" / "update_staging", ignore_errors=True)
        msg = str(exc)
        if status.get("latest") and ("safety tests" in msg or "manual install" in msg):
            _rejected(root, {"sha": status["latest"], "message": msg})
        return {**status, "state": "error", "message": msg}
    except requests.RequestException:
        shutil.rmtree(root / "data" / "update_staging", ignore_errors=True)
        return {**status, "state": "offline", "message": "Couldn't reach GitHub; will try again later."}


def _rejected(root: Path, value: dict | None = None) -> dict:
    """Remember a version that failed so it isn't downloaded and tested every 30 minutes."""
    p = root / "data" / "update_rejected.json"
    if value is not None:
        p.write_text(json.dumps(value))
        return value
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def save_token(root: Path, token: str, repo: str, http=requests) -> dict[str, Any]:
    """Validate a GitHub token against the repo, then store it owner-only."""
    from .credentials import CredentialError, _write_private
    token = (token or "").strip()
    if not TOKEN_RE.match(token):
        raise CredentialError("That doesn't look like a GitHub token. It starts with github_pat_ or ghp_.")
    try:
        r = http.get(f"{API}/repos/{repo}", headers=_headers(token), timeout=20)
    except requests.RequestException:
        raise CredentialError("Couldn't reach GitHub to check the token. Try again in a minute.") from None
    if r.status_code != 200:
        raise CredentialError(str(_explain(r, True)))
    d = root / "secrets"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    _write_private(token_path(root), token + "\n")
    return {"ok": True}


def status(db) -> dict[str, Any]:
    try:
        return json.loads(db.get_control("updater") or "{}")
    except ValueError:
        return {}


def main() -> None:
    s = load_settings()
    db = open_db(s.db_path)
    result = check(s)
    db.set_control("updater", json.dumps(result), "updater")
    if result["state"] in ("installed", "error"):
        db.log_event("updater", "info" if result["state"] == "installed" else "warning", result["message"])
    print(time.strftime("%Y-%m-%d %H:%M:%S"), result["state"], result["message"], flush=True)


if __name__ == "__main__":
    main()
