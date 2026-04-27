"""Security primitives: HMAC verification, password hashing,
rate limiting (Phase 6).
"""
from app.security.hmac_verify import (
    InvalidSignatureError,
    ReplayedRequestError,
    SignatureRequiredError,
    compute_signature,
    verify_hmac_request,
)
from app.security.passwords import (
    PasswordTooLongError,
    WeakPasswordError,
    hash_needs_rehash,
    hash_password,
    verify_password,
)

__all__ = [
    "InvalidSignatureError",
    "PasswordTooLongError",
    "ReplayedRequestError",
    "SignatureRequiredError",
    "WeakPasswordError",
    "compute_signature",
    "hash_needs_rehash",
    "hash_password",
    "verify_password",
    "verify_hmac_request",
]
