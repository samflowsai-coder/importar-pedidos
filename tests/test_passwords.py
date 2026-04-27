"""Tests for app.security.passwords (bcrypt wrapper)."""
from __future__ import annotations

import pytest

from app.security.passwords import (
    DEFAULT_ROUNDS,
    MAX_PASSWORD_BYTES,
    PasswordTooLongError,
    WeakPasswordError,
    hash_needs_rehash,
    hash_password,
    verify_password,
)


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)
    assert not verify_password("wrong password", h)


def test_hash_uses_unique_salt():
    """Same plaintext → different hashes (different salts)."""
    h1 = hash_password("samepassword123")
    h2 = hash_password("samepassword123")
    assert h1 != h2
    # Both still verify
    assert verify_password("samepassword123", h1)
    assert verify_password("samepassword123", h2)


def test_short_password_rejected():
    with pytest.raises(WeakPasswordError):
        hash_password("short")  # 5 chars < min 8


def test_long_password_rejected():
    """Bcrypt silently truncates >72 bytes — we reject loudly."""
    with pytest.raises(PasswordTooLongError):
        hash_password("x" * (MAX_PASSWORD_BYTES + 1))


def test_verify_handles_garbage_hash_safely():
    """Malformed hash should return False, not raise."""
    assert not verify_password("anything", "not-a-hash")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "$2b$12$bogus")


def test_verify_handles_non_string_inputs():
    assert not verify_password(None, "h")  # type: ignore[arg-type]
    assert not verify_password("p", None)  # type: ignore[arg-type]


def test_hash_needs_rehash_at_lower_rounds():
    """Hash created at 10 rounds needs upgrade to current default 12."""
    low = hash_password("password123", rounds=10)
    assert hash_needs_rehash(low, rounds=DEFAULT_ROUNDS) is True


def test_hash_does_not_need_rehash_at_default():
    h = hash_password("password123")
    assert hash_needs_rehash(h) is False


def test_hash_needs_rehash_for_garbage():
    """Malformed hash should be flagged for rehash (forces upgrade)."""
    assert hash_needs_rehash("not-a-bcrypt-hash") is True


def test_long_password_truncates_safely_in_verify():
    """A too-long input should NOT raise on verify — returns False cleanly.

    We can't store too-long hashes (hash_password rejects), but a malicious
    client sending 10KB password should not crash the login route.
    """
    h = hash_password("normalpass")
    huge = "x" * 100_000
    assert verify_password(huge, h) is False
