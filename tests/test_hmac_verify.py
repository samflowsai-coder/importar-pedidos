"""Tests for app.security.hmac_verify — HMAC-SHA256 + timestamp + rotation."""
from __future__ import annotations

import time

import pytest

from app.security.hmac_verify import (
    InvalidSignatureError,
    ReplayedRequestError,
    SignatureRequiredError,
    compute_signature,
    verify_hmac_request,
)


def _now() -> int:
    return int(time.time())


def test_valid_signature_accepted():
    secret = "shared-secret-1"
    body = b'{"event_id": "abc"}'
    ts = str(_now())
    sig = compute_signature(secret, ts, body)
    # Should not raise
    verify_hmac_request(
        body=body, signature_header=sig, timestamp_header=ts,
        secrets=[secret],
    )


def test_invalid_signature_rejected():
    body = b'{"event_id": "abc"}'
    ts = str(_now())
    bad_sig = compute_signature("wrong-secret", ts, body)
    with pytest.raises(InvalidSignatureError):
        verify_hmac_request(
            body=body, signature_header=bad_sig, timestamp_header=ts,
            secrets=["correct-secret"],
        )


def test_missing_signature_rejected():
    with pytest.raises(SignatureRequiredError):
        verify_hmac_request(
            body=b"{}", signature_header=None, timestamp_header=str(_now()),
            secrets=["secret"],
        )


def test_missing_timestamp_rejected():
    with pytest.raises(SignatureRequiredError):
        verify_hmac_request(
            body=b"{}", signature_header="sha256=abc", timestamp_header=None,
            secrets=["secret"],
        )


def test_no_configured_secret_fails_closed():
    """Misconfiguration must NEVER pass — reject with SignatureRequiredError."""
    body = b"{}"
    ts = str(_now())
    sig = compute_signature("anything", ts, body)
    with pytest.raises(SignatureRequiredError, match="No HMAC secret"):
        verify_hmac_request(
            body=body, signature_header=sig, timestamp_header=ts,
            secrets=["", "  "],  # empty / whitespace
        )


def test_old_timestamp_rejected_as_replay():
    """Timestamp older than max_skew_seconds is a replay attempt."""
    secret = "s"
    body = b"{}"
    old_ts = str(_now() - 600)  # 10min ago, default max is 5min
    sig = compute_signature(secret, old_ts, body)
    with pytest.raises(ReplayedRequestError):
        verify_hmac_request(
            body=body, signature_header=sig, timestamp_header=old_ts,
            secrets=[secret],
        )


def test_future_timestamp_rejected():
    """Clock skew works both ways — too far in the future is also rejected."""
    secret = "s"
    body = b"{}"
    future_ts = str(_now() + 600)
    sig = compute_signature(secret, future_ts, body)
    with pytest.raises(ReplayedRequestError):
        verify_hmac_request(
            body=body, signature_header=sig, timestamp_header=future_ts,
            secrets=[secret],
        )


def test_garbage_timestamp_rejected():
    secret = "s"
    body = b"{}"
    sig = compute_signature(secret, "not-a-number", body)
    with pytest.raises(ReplayedRequestError):
        verify_hmac_request(
            body=body, signature_header=sig, timestamp_header="not-a-number",
            secrets=[secret],
        )


def test_secret_rotation_accepts_previous():
    """During rotation, both current and previous secrets must validate."""
    body = b'{"a": 1}'
    ts = str(_now())
    sig_with_old = compute_signature("OLD-secret", ts, body)
    # Verifier configured with NEW (current) + OLD (previous)
    verify_hmac_request(
        body=body, signature_header=sig_with_old, timestamp_header=ts,
        secrets=["NEW-secret", "OLD-secret"],
    )


def test_signature_with_or_without_prefix():
    """Some providers send raw hex; others prefix with 'sha256='. Both work."""
    secret = "s"
    body = b"{}"
    ts = str(_now())
    sig = compute_signature(secret, ts, body)
    raw = sig.split("=", 1)[1]
    # Without prefix
    verify_hmac_request(
        body=body, signature_header=raw, timestamp_header=ts, secrets=[secret],
    )


def test_compute_signature_is_deterministic():
    """Same inputs always produce same digest."""
    s1 = compute_signature("k", "100", b"hello")
    s2 = compute_signature("k", "100", b"hello")
    assert s1 == s2
    assert s1.startswith("sha256=")
