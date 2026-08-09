"""Auth: password hashing, token signing, and endpoint protection."""
from __future__ import annotations

import pytest

from app.core.security import (
    create_token,
    hash_password,
    read_token,
    verify_password,
)


# --------------------------------------------------------------------------- #
#  Passwords
# --------------------------------------------------------------------------- #
def test_password_is_never_stored_in_plaintext():
    stored = hash_password("admin@123")
    assert "admin@123" not in stored
    assert stored.startswith("pbkdf2_sha256$")


def test_correct_password_verifies():
    assert verify_password("admin@123", hash_password("admin@123"))


@pytest.mark.parametrize("wrong", ["admin@1234", "Admin@123", "", " admin@123"])
def test_wrong_password_rejected(wrong):
    assert not verify_password(wrong, hash_password("admin@123"))


def test_same_password_gets_a_different_hash():
    """Per-password salt: identical passwords must not share a hash, or one
    precomputed table would break every account at once."""
    assert hash_password("admin@123") != hash_password("admin@123")


def test_malformed_stored_hash_is_rejected_not_crashed():
    for junk in ["", "garbage", "pbkdf2_sha256$notanumber$x$y", "a$b$c"]:
        assert verify_password("admin@123", junk) is False


# --------------------------------------------------------------------------- #
#  Tokens
# --------------------------------------------------------------------------- #
def test_token_roundtrip():
    token = create_token("user-1", "secret")
    assert read_token(token, "secret") == "user-1"


def test_token_rejected_with_wrong_secret():
    token = create_token("user-1", "secret")
    assert read_token(token, "other-secret") is None


def test_tampered_token_rejected():
    token = create_token("user-1", "secret")
    assert read_token(token[:-4] + "AAAA", "secret") is None


def test_tampered_payload_rejected():
    """Editing the payload must invalidate the signature — otherwise anyone
    could rewrite `sub` and become another user."""
    token = create_token("user-1", "secret")
    body, _, sig = token.partition(".")
    forged = create_token("admin", "attacker-secret").partition(".")[0]
    assert read_token(f"{forged}.{sig}", "secret") is None


def test_expired_token_rejected():
    assert read_token(create_token("user-1", "secret", ttl_seconds=-1), "secret") is None


@pytest.mark.parametrize("junk", ["", "no-dot", "a.b", "...."])
def test_malformed_token_rejected(junk):
    assert read_token(junk, "secret") is None


# --------------------------------------------------------------------------- #
#  Endpoint protection
# --------------------------------------------------------------------------- #
@pytest.fixture
def client():
    from unittest.mock import MagicMock, patch

    from fastapi.testclient import TestClient

    from app.api.routes import app

    # /health counts candidates, so an unstubbed repository sends this suite to
    # the real cluster. These tests are about auth, not data.
    repo = MagicMock()
    repo.count.return_value = 0

    with patch("app.api.routes.ensure_indexes"), \
         patch("app.api.routes.repo", return_value=repo):
        yield TestClient(app)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/candidates"),
        ("get", "/candidates/some-id"),
        ("get", "/job-orders"),
        ("get", "/sourcing-clients"),
        ("post", "/ingest/poll"),
    ],
)
def test_data_endpoints_require_a_token(client, method, path):
    """Without this the login screen would be decoration: the API would still
    serve every candidate record to an anonymous caller."""
    assert getattr(client, method)(path).status_code == 401


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200


def test_garbage_token_is_rejected(client):
    res = client.get("/candidates", headers={"Authorization": "Bearer not-a-token"})
    assert res.status_code == 401
