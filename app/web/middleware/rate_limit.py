"""Token-bucket rate limiter backed by SQLite.

Usage (FastAPI dependency)::

    from app.web.middleware.rate_limit import check_and_consume

    async def _login_rate_limit(request: Request) -> None:
        ip = request.client.host or "unknown"
        if not check_and_consume(f"login:{ip}", capacity=10, refill_rate=10 / 900):
            raise HTTPException(429, detail="Too many login attempts",
                                headers={"Retry-After": "900"})

The bucket is initialised lazily on first hit. Token arithmetic is done
inside a single SQLite connection (DEFERRED isolation) so concurrent
requests are safely serialised at the DB layer — no external lock needed.

Set env var RATE_LIMIT_ENABLED=false to bypass for dev/test environments.
"""
from __future__ import annotations

import os
import time

from app.persistence.db import connect_shared as connect


def _enabled() -> bool:
    return os.environ.get("RATE_LIMIT_ENABLED", "true").lower() not in ("false", "0", "no")


def check_and_consume(
    key: str,
    capacity: int,
    refill_rate: float,
    cost: float = 1.0,
) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    Args:
        key: Bucket identifier, e.g. ``"login:192.168.1.1"``.
        capacity: Maximum tokens in the bucket.
        refill_rate: Tokens added per second.
        cost: Tokens consumed by this request (default 1).
    """
    if not _enabled():
        return True

    now = time.time()

    with connect() as conn:
        row = conn.execute(
            "SELECT tokens, last_refill_at FROM rate_limit_buckets WHERE key = ?",
            (key,),
        ).fetchone()

        if row is None:
            # First request — initialise bucket, consume one token.
            new_tokens = float(capacity) - cost
            conn.execute(
                "INSERT INTO rate_limit_buckets (key, tokens, last_refill_at) VALUES (?, ?, ?)",
                (key, new_tokens, now),
            )
            return True

        stored_tokens: float = row[0]
        last_refill_at: float = row[1]

        elapsed = now - last_refill_at
        refilled = min(float(capacity), stored_tokens + elapsed * refill_rate)

        if refilled < cost:
            # Bucket dry — update timestamp but do not consume.
            conn.execute(
                "UPDATE rate_limit_buckets SET tokens = ?, last_refill_at = ? WHERE key = ?",
                (refilled, now, key),
            )
            return False

        conn.execute(
            "UPDATE rate_limit_buckets SET tokens = ?, last_refill_at = ? WHERE key = ?",
            (refilled - cost, now, key),
        )
        return True
