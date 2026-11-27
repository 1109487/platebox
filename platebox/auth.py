"""One administrator. No accounts, no roles, nothing to forget but a single password."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque

# scrypt from the standard library: no extra dependency to build for ARM, and slow
# enough to make guessing a stolen database pointless.
_N, _R, _P = 2**14, 8, 1

MIN_PASSWORD = 8


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, want = stored.split("$")
        if scheme != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


def password_problem(password: str) -> str | None:
    """Why a password is not acceptable, or None. Deliberately short: length is what matters."""
    if len(password) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if password.lower() in {"password", "12345678", "admin123", "platebox"}:
        return "That password is too easy to guess."
    return None


def new_code(n: int = 6) -> str:
    """A short one-time code, e.g. for claiming a fresh box."""
    return "".join(secrets.choice("0123456789") for _ in range(n))


class Signer:
    """Signed, expiring cookie values. Stateless: restarting the box does not log anyone out."""

    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def sign(self, subject: str, ttl_s: int, now: float | None = None) -> str:
        exp = int((now if now is not None else time.time()) + ttl_s)
        body = f"{subject}|{exp}".encode()
        mac = hmac.new(self._secret, body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def verify(self, token: str | None, now: float | None = None) -> str | None:
        if not token or "." not in token:
            return None
        try:
            b, m = token.split(".", 1)
            body = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
            mac = base64.urlsafe_b64decode(m + "=" * (-len(m) % 4))
            want = hmac.new(self._secret, body, hashlib.sha256).digest()
            if not hmac.compare_digest(mac, want):
                return None
            subject, exp = body.decode().rsplit("|", 1)
            if int(exp) < (now if now is not None else time.time()):
                return None
            return subject
        except (ValueError, UnicodeDecodeError):
            return None


class Throttle:
    """Too many wrong passwords from one place in a short while: make them wait."""

    def __init__(self, attempts: int = 5, window_s: int = 300) -> None:
        self.attempts, self.window_s = attempts, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        q = self._fails[key]
        while q and now - q[0] > self.window_s:
            q.popleft()
        return q

    def blocked(self, key: str, now: float | None = None) -> int:
        """Seconds still to wait, or 0."""
        t = now if now is not None else time.time()
        q = self._trim(key, t)
        if len(q) < self.attempts:
            return 0
        return max(1, int(self.window_s - (t - q[0])))

    def fail(self, key: str, now: float | None = None) -> None:
        t = now if now is not None else time.time()
        self._trim(key, t).append(t)

    def ok(self, key: str) -> None:
        self._fails.pop(key, None)
