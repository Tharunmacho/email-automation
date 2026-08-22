"""Password hashing and signed session tokens.

Uses only the standard library — PBKDF2-HMAC-SHA256 for passwords and an
HMAC-SHA256 signature for tokens — so no new dependency is required.

Passwords are never stored or logged in plaintext. Each password gets its own
random salt, so two users with the same password produce different hashes and a
stolen database cannot be attacked with a single precomputed table.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Optional

# Cost factor. OWASP's 2023 floor for PBKDF2-HMAC-SHA256 is 600k iterations.
_ITERATIONS = 600_000
_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16


# --------------------------------------------------------------------------- #
#  Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Return ``algo$iterations$salt$hash`` — everything needed to verify later."""
    if not password:
        raise ValueError("Password must not be empty.")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join([
        _ALGO,
        str(_ITERATIONS),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a candidate password against a stored hash."""
    if not password or not stored:
        return False
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    # compare_digest, not ==, so the comparison cannot be timed to leak the hash.
    return hmac.compare_digest(actual, expected)


# --------------------------------------------------------------------------- #
#  Session tokens
# --------------------------------------------------------------------------- #
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(subject: str, secret: str, ttl_seconds: int = 60 * 60 * 12) -> str:
    """Sign ``payload.signature``. Stateless, so no server-side session store."""
    payload = {"sub": subject, "exp": int(time.time()) + ttl_seconds}
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64url(sig)}"


def read_token(token: str, secret: str) -> Optional[str]:
    """Return the subject if the signature is valid and unexpired, else None."""
    if not token or not secret or "." not in token:
        return None
    body, _, sig = token.partition(".")
    expected = hmac.new(
        secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
    ).digest()
    try:
        if not hmac.compare_digest(_unb64url(sig), expected):
            return None
        payload = json.loads(_unb64url(body))
    except (ValueError, TypeError):
        return None

    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) and subject else None


# --------------------------------------------------------------------------- #
#  Service-to-service keys
# --------------------------------------------------------------------------- #
def verify_service_key(presented: Optional[str], expected: Optional[str]) -> bool:
    """Whether a caller's `X-Service-Key` matches the configured one.

    Separate from the session tokens above, and deliberately so. A session token
    identifies a person, comes from a login form and expires in hours; a service
    key identifies another system, is issued once and lives until it is rotated.
    Collapsing the two would mean a leaked recruiter session could create
    candidates, and rotating the bot's credential would sign every recruiter out.

    An unset `expected` returns False. A blank configuration is a closed door,
    not an open one — a deployment that forgot to set the key must fail every
    request rather than accept an empty header and serve an unauthenticated
    write endpoint.

    `compare_digest` throughout: a plain `==` on a secret leaks its length and
    its matching prefix through timing, and this value is a bearer credential.
    """
    if not expected or not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
