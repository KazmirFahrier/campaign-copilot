"""Replay protected Ed25519 authentication for API to executor requests."""

from __future__ import annotations

import base64
import hashlib
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

_NONCE = re.compile(r"^[0-9a-f]{32}$")


def _message(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{method.upper()}\n{path}\n{digest}".encode()


@dataclass(frozen=True, slots=True)
class RequestSigner:
    """Sign one request with the API's raw Ed25519 private key."""

    key: Ed25519PrivateKey

    @classmethod
    def from_base64(cls, encoded: str) -> RequestSigner:
        """Decode a raw private key from its base64 configuration value."""
        return cls(Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded)))

    def headers(self, method: str, path: str, body: bytes) -> dict[str, str]:
        """Return signed timestamp, nonce, and body authentication headers."""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = self.key.sign(_message(timestamp, nonce, method, path, body))
        return {
            "X-CC-Timestamp": timestamp,
            "X-CC-Nonce": nonce,
            "X-CC-Signature": base64.b64encode(signature).decode(),
        }


@dataclass
class RequestVerifier:
    """Verify signatures and reject stale or replayed requests."""

    key: Ed25519PublicKey
    max_age_seconds: int = 60
    max_nonces: int = 4096
    _seen: set[str] = field(default_factory=set, init=False, repr=False)
    _order: deque[str] = field(default_factory=deque, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    @classmethod
    def from_base64(cls, encoded: str) -> RequestVerifier:
        """Decode a raw public key from its base64 configuration value."""
        return cls(Ed25519PublicKey.from_public_bytes(base64.b64decode(encoded)))

    def verify(
        self,
        *,
        timestamp: str,
        nonce: str,
        signature: str,
        method: str,
        path: str,
        body: bytes,
    ) -> bool:
        """Return true only for a fresh, correctly signed, previously unseen request."""
        try:
            issued = int(timestamp)
            decoded = base64.b64decode(signature, validate=True)
        except (ValueError, TypeError):
            return False
        if not _NONCE.fullmatch(nonce) or abs(int(time.time()) - issued) > self.max_age_seconds:
            return False
        try:
            self.key.verify(decoded, _message(timestamp, nonce, method, path, body))
        except InvalidSignature:
            return False
        with self._lock:
            if nonce in self._seen:
                return False
            self._seen.add(nonce)
            self._order.append(nonce)
            while len(self._order) > self.max_nonces:
                self._seen.discard(self._order.popleft())
        return True
