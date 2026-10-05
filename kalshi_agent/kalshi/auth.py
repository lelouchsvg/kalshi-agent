"""Kalshi request signing (verified against docs.kalshi.com, 2026-10-05).

Headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP (ms), KALSHI-ACCESS-SIGNATURE.
Message: timestamp_ms + HTTP_METHOD + path, where path EXCLUDES the query string
and includes the /trade-api/v2 prefix. RSA keys sign with RSA-PSS / SHA-256 /
MGF1-SHA256 / salt = digest length; Ed25519 keys sign the message directly.
"""
from __future__ import annotations

import base64
import os
import stat
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa


class KeyFileError(RuntimeError):
    pass


def load_private_key(path: str | Path):
    p = Path(path).expanduser()
    if not p.exists():
        raise KeyFileError(f"Private key file not found at {p}")
    mode = p.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeyFileError(
            f"Private key file {p} is readable by other users. Fix with: chmod 600 {p}")
    key = serialization.load_pem_private_key(p.read_bytes(), password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, ed25519.Ed25519PrivateKey)):
        raise KeyFileError("Unsupported key type; Kalshi accepts RSA or Ed25519 keys")
    return key


def signing_path(url_or_path: str) -> str:
    """Path to sign: no scheme/host, no query string."""
    parsed = urlparse(url_or_path)
    return parsed.path if parsed.scheme else url_or_path.split("?", 1)[0]


def sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    message = f"{timestamp_ms}{method.upper()}{signing_path(path)}".encode()
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        sig = private_key.sign(message)
    else:
        sig = private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode()


class KalshiSigner:
    def __init__(self, key_id: str, private_key_path: str | os.PathLike):
        self.key_id = key_id
        self._key = load_private_key(private_key_path)

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign(self._key, ts, method, url_or_path),
        }

    def __repr__(self) -> str:  # never print key material
        return f"KalshiSigner(key_id={self.key_id[:4]}…)"
