"""Password hashing using bcrypt.

bcrypt rounds=12 is the OWASP-recommended default for 2024+. Higher rounds
increase login latency without substantially raising attacker cost beyond
~30s/guess; 12 is the sweet spot.

Stored format includes algorithm + rounds + salt + hash, so verification
needs only the stored hash and the candidate password — no separate salt
column. Future rounds bumps are detectable via `hash_needs_rehash`.

API:
    hash_password(plaintext)      -> stored hash (UTF-8 string)
    verify_password(plain, hash)  -> bool, constant time
    hash_needs_rehash(hash)       -> bool (use for opportunistic upgrades on login)
"""
from __future__ import annotations

import bcrypt

DEFAULT_ROUNDS = 12

# Reject inputs that bcrypt would silently truncate. bcrypt only uses the
# first 72 bytes of the password; anything longer is a footgun (a 100-char
# password and the same prefix + extra bytes produce the same hash).
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 8


class WeakPasswordError(ValueError):
    """Password fails minimum-strength rule (length-only for now)."""


class PasswordTooLongError(ValueError):
    """Password exceeds bcrypt's silent-truncation limit."""


def _validate(plaintext: str) -> bytes:
    if not isinstance(plaintext, str):
        raise TypeError("password must be str")
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise WeakPasswordError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters"
        )
    encoded = plaintext.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(
            f"password exceeds {MAX_PASSWORD_BYTES} bytes "
            "(bcrypt truncates silently — reject loudly instead)"
        )
    return encoded


def hash_password(plaintext: str, *, rounds: int = DEFAULT_ROUNDS) -> str:
    """Hash a password. Salt + cost are embedded in the returned string."""
    encoded = _validate(plaintext)
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(encoded, salt).decode("utf-8")


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Constant-time comparison. Returns False on any failure (incl. malformed hash)."""
    if not isinstance(plaintext, str) or not isinstance(stored_hash, str):
        return False
    try:
        encoded = plaintext.encode("utf-8")
        # Truncate to bcrypt's max so a too-long input compares against the
        # truncated form already in the DB (still won't match, but safe).
        encoded = encoded[:MAX_PASSWORD_BYTES]
        return bcrypt.checkpw(encoded, stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def hash_needs_rehash(stored_hash: str, *, rounds: int = DEFAULT_ROUNDS) -> bool:
    """True if the stored hash uses fewer rounds than the current default.

    Call after a successful login to opportunistically upgrade — re-hash
    the plaintext (which we have at login time) with `hash_password` and
    UPDATE the row. Users get stronger hashes silently as they sign in.
    """
    try:
        # bcrypt format: $2b$<rounds>$<salt><hash>
        parts = stored_hash.split("$")
        current = int(parts[2])
        return current < rounds
    except (IndexError, ValueError):
        return True  # malformed → definitely rehash


__all__ = [
    "DEFAULT_ROUNDS",
    "MAX_PASSWORD_BYTES",
    "MIN_PASSWORD_LENGTH",
    "PasswordTooLongError",
    "WeakPasswordError",
    "hash_needs_rehash",
    "hash_password",
    "verify_password",
]
