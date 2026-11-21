"""Kalshi API request signing.

Headers (per Kalshi docs and the official generated SDK, kalshi_python_sync/auth.py):
    KALSHI-ACCESS-KEY        API key id
    KALSHI-ACCESS-TIMESTAMP  unix time in MILLISECONDS, as a string
    KALSHI-ACCESS-SIGNATURE  base64( sign( timestamp + METHOD + path ) )

`path` is the URL path INCLUDING the /trade-api/v2 prefix and EXCLUDING the
query string. RSA keys sign with RSA-PSS / SHA-256 / MGF1-SHA256 /
salt_length = digest length; Ed25519 keys sign the same message directly.
The private key is never logged or included in repr().
"""

from __future__ import annotations

import base64
import time
from typing import Callable, Optional
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey


def signing_path(url_or_path: str) -> str:
    """Return the path component to sign: query string and fragment stripped."""
    if "://" in url_or_path:
        return urlparse(url_or_path).path
    return url_or_path.split("?", 1)[0].split("#", 1)[0]


def build_message(timestamp_ms: str, method: str, path: str) -> bytes:
    return (str(timestamp_ms) + method.upper() + signing_path(path)).encode("utf-8")


class KalshiSigner:
    def __init__(
        self,
        key_id: str,
        private_key_pem: bytes,
        clock: Optional[Callable[[], float]] = None,
    ):
        if not key_id:
            raise ValueError("API key id is empty")
        key = serialization.load_pem_private_key(private_key_pem, password=None)
        if not isinstance(key, (RSAPrivateKey, Ed25519PrivateKey)):
            raise ValueError("Private key must be an RSA or Ed25519 key")
        self.key_id = key_id
        self._key = key
        # clock() returns unix seconds; overridable for drift correction / tests.
        self._clock = clock or time.time

    @classmethod
    def from_file(cls, key_id: str, path: str, clock=None) -> "KalshiSigner":
        with open(path, "rb") as fh:
            return cls(key_id, fh.read(), clock=clock)

    @property
    def key_type(self) -> str:
        return "ed25519" if isinstance(self._key, Ed25519PrivateKey) else "rsa"

    def sign(self, message: bytes) -> bytes:
        if isinstance(self._key, Ed25519PrivateKey):
            return self._key.sign(message)
        return self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

    def headers(self, method: str, url_or_path: str, timestamp_ms: Optional[int] = None) -> dict:
        ts = str(int(timestamp_ms if timestamp_ms is not None else self._clock() * 1000))
        sig = self.sign(build_message(ts, method, url_or_path))
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("ascii"),
        }

    def __repr__(self) -> str:
        return f"KalshiSigner(key_id={self.key_id[:4]}..., type={self.key_type})"
