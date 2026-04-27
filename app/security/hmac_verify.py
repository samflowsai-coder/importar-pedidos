"""HMAC-SHA256 webhook signature verification with replay protection.

Wire format we expect (assumed for Gestor; common in Stripe/GitHub-style):

    X-Signature: sha256=<hex digest>
    X-Timestamp: <unix seconds>

The signed payload is `f"{timestamp}.{body}"`. Receiver:
    1. Reject if timestamp older/younger than `max_skew_seconds` (default 5min).
    2. Recompute HMAC-SHA256 with the shared secret.
    3. Compare with `hmac.compare_digest` (constant-time, anti-timing-attack).
    4. Accept if EITHER `current` OR `previous` secret matches (rotation).

Why timestamp + signature: HMAC alone doesn't prevent replay of a captured
request. An attacker who sniffed one valid call could resend it forever.
The timestamp window bounds replay to a 5-minute window, after which the
signature is invalid (timestamp too old).

Idempotency-key on the same request (handled separately in
`idempotency_repo`) closes the loop: even within the 5-minute window, a
replay returns the cached response without re-processing.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable

DEFAULT_MAX_SKEW_SECONDS = 5 * 60
SIGNATURE_PREFIX = "sha256="


class SignatureRequiredError(Exception):
    """Header missing entirely. 401 to caller."""


class InvalidSignatureError(Exception):
    """Signature did not match any known secret. 403 to caller."""


class ReplayedRequestError(Exception):
    """Timestamp outside acceptable skew window. 403 to caller."""


def compute_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Compute the canonical signature header value for `(secret, timestamp, body)`.

    Returns the full header value: `sha256=<hex>`. Used by both the
    verifier and any test/utility code that needs to construct valid
    signatures (e.g. integration tests, CLI to ping the webhook locally).
    """
    payload = f"{timestamp}.".encode() + body
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def _strip_prefix(value: str) -> str:
    if value.startswith(SIGNATURE_PREFIX):
        return value[len(SIGNATURE_PREFIX):]
    return value


def verify_hmac_request(
    *,
    body: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
    secrets: Iterable[str],
    max_skew_seconds: int = DEFAULT_MAX_SKEW_SECONDS,
    now: int | None = None,
) -> None:
    """Validate signature + timestamp. Raises on any failure; returns None on success.

    `secrets` accepts multiple keys to support rotation (current + previous).
    Empty/whitespace secrets are skipped, so passing the env value directly
    when not set yields a clean SignatureRequiredError rather than a confusing
    pass-through.
    """
    if not signature_header or not timestamp_header:
        raise SignatureRequiredError(
            "Missing X-Signature or X-Timestamp header"
        )

    # Timestamp skew check first — cheap rejection of replays.
    try:
        ts_int = int(timestamp_header)
    except (TypeError, ValueError) as exc:
        raise ReplayedRequestError(
            f"Invalid timestamp format: {timestamp_header!r}"
        ) from exc

    current = now if now is not None else int(time.time())
    if abs(current - ts_int) > max_skew_seconds:
        raise ReplayedRequestError(
            f"Timestamp skew {current - ts_int}s exceeds max {max_skew_seconds}s"
        )

    given_digest = _strip_prefix(signature_header.strip())
    valid_secrets = [s for s in secrets if s and s.strip()]
    if not valid_secrets:
        # Misconfiguration — fail closed. Better to 4xx than to silently accept.
        raise SignatureRequiredError("No HMAC secret configured on the receiver")

    for secret in valid_secrets:
        expected = _strip_prefix(compute_signature(secret, timestamp_header, body))
        if hmac.compare_digest(expected, given_digest):
            return  # success — at least one secret matched

    raise InvalidSignatureError("Signature did not match any configured secret")


__all__ = [
    "DEFAULT_MAX_SKEW_SECONDS",
    "InvalidSignatureError",
    "ReplayedRequestError",
    "SignatureRequiredError",
    "compute_signature",
    "verify_hmac_request",
]
