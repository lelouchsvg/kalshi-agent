"""Save and check the Kalshi API key from the local dashboard.

The key is typed into the dashboard in the user's own browser, sent only to this
Mac (127.0.0.1), and written to secrets/kalshi.key (owner-only, chmod 600) plus two
lines in .env. It is never logged, never sent back to the browser, and never
leaves this computer except as request signatures to Kalshi.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable

KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,79}$")
KINDS = {  # prod = the real-money exchange's market data key; demo = demo.kalshi.co account
    "prod": ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "kalshi.key"),
    "demo": ("KALSHI_DEMO_API_KEY_ID", "KALSHI_DEMO_PRIVATE_KEY_PATH", "kalshi-demo.key"),
}
MAX_PEM_CHARS = 12_000


class CredentialError(ValueError):
    """A problem the user can fix; the message is shown on the dashboard."""


def key_path(root: Path, kind: str = "prod") -> Path:
    return root / "secrets" / KINDS[kind][2]


def read_env(root: Path) -> dict[str, str]:
    """Read .env fresh from disk (the dashboard process may predate a change)."""
    out: dict[str, str] = {}
    p = root / ".env"
    if not p.exists():
        return out
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def clean_pem(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(text) > MAX_PEM_CHARS:
        raise CredentialError("That is too long to be a Kalshi private key.")
    if "-----BEGIN" not in text or "PRIVATE KEY-----" not in text or "-----END" not in text:
        raise CredentialError("Paste the whole private key, from the -----BEGIN line to the -----END line.")
    # People sometimes copy a label before the key; keep only the PEM block.
    start = text.index("-----BEGIN")
    end = text.index("-----", text.index("-----END") + 8) + 5
    return text[start:end] + "\n"


def clean_key_id(text: str) -> str:
    key_id = (text or "").strip()
    if not KEY_ID_RE.match(key_id):
        raise CredentialError("The Key ID looks wrong. It is the short ID Kalshi shows next to the key, "
                              "like a1b2c3d4-e5f6-... (letters, numbers and dashes only).")
    return key_id


def _write_private(path: Path, data: str) -> None:
    """Atomic write with owner-only permissions from the first byte."""
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _key_type(path: Path) -> str:
    from .kalshi.auth import load_private_key
    key = load_private_key(path)
    return "Ed25519" if "ed25519" in type(key).__name__.lower() else "RSA"


def save(root: Path, key_id: str, pem: str, kind: str = "prod") -> dict[str, Any]:
    """Validate, then write the key file under secrets/ and update .env. Raises CredentialError."""
    if kind not in KINDS:
        raise CredentialError("Unknown key type.")
    id_var, path_var, _ = KINDS[kind]
    key_id = clean_key_id(key_id)
    pem = clean_pem(pem)
    secrets_dir = root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    target = key_path(root, kind)

    # Check the key parses before replacing anything that already works.
    trial = secrets_dir / (target.name + ".check")
    _write_private(trial, pem)
    try:
        key_type = _key_type(trial)
    except CredentialError:
        trial.unlink(missing_ok=True)
        raise
    except Exception as exc:
        trial.unlink(missing_ok=True)
        if "cryptography" in str(exc):
            raise CredentialError("The agent is missing its key library. Run start.sh once more, then try again.") from None
        raise CredentialError("That private key could not be read. Copy it again from Kalshi, "
                              "including the BEGIN and END lines.") from None
    os.replace(trial, target)
    os.chmod(target, 0o600)

    env_path = root / ".env"
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    kept = [ln for ln in lines if ln.split("=", 1)[0].strip() not in (id_var, path_var)]
    kept += [f"{id_var}={key_id}", f"{path_var}={target}"]
    _write_private(env_path, "\n".join(kept) + "\n")
    return {"key_type": key_type, "key_id_hint": hint(key_id)}


def hint(key_id: str | None) -> str | None:
    return f"…{key_id[-4:]}" if key_id else None


def status(root: Path, kind: str = "prod") -> dict[str, Any]:
    """What the dashboard shows. Never includes the key itself."""
    env = read_env(root)
    key_id = env.get(KINDS[kind][0])
    path = env.get(KINDS[kind][1])
    out: dict[str, Any] = {"configured": bool(key_id and path), "key_id_hint": hint(key_id), "problem": None}
    if not out["configured"]:
        return out
    p = Path(path).expanduser()
    if not p.exists():
        out["problem"] = "The key file is missing. Save the key again."
    elif p.stat().st_size == 0:
        out["problem"] = "The key file is empty. Save the key again."
    elif p.stat().st_mode & 0o077:
        out["problem"] = "The key file can be read by other users on this Mac. Save the key again to fix it."
    return out


def verify(base_url: str, key_id: str, path: Path,
           client_factory: Callable[..., Any] | None = None) -> tuple[bool | None, str]:
    """Ask Kalshi for the account balance (read-only) to prove the key works.

    Returns (True, msg) if accepted, (False, msg) if Kalshi rejected it, and
    (None, msg) if Kalshi could not be reached (the key is still saved).
    """
    from .kalshi.auth import KalshiSigner
    from .kalshi.client import KalshiAPIError, KalshiClient
    factory = client_factory or (lambda signer: KalshiClient(base_url, signer=signer, requests_per_second=1))
    try:
        factory(KalshiSigner(key_id, path)).get_balance()
        return True, "Kalshi accepted the key."
    except KalshiAPIError as exc:
        if exc.status in (401, 403):
            return False, ("Kalshi rejected the key. Check the Key ID belongs to this private key, "
                           "or create a new key on Kalshi and paste both again.")
        return None, f"Saved, but Kalshi could not confirm it yet (error {exc.status})."
    except Exception:
        return None, "Saved, but Kalshi could not be reached to confirm it. It will be tried when the agent restarts."
