"""Session-cookie auth for the Portal UI.

Cookie design:
    name      = "portal_session"
    value     = high-entropy random token (sessions_repo.new_token)
    HttpOnly  = True          (not readable from JS — XSS containment)
    SameSite  = "Strict"      (CSRF containment for browsers)
    Secure    = depends on env (default True in prod via PORTAL_COOKIE_SECURE)
    Max-Age   = TTL in seconds (matches sessions.expires_at)
    Path      = "/"

FastAPI surface:
    Depends(current_user)  → Optional[User]   (None if no/invalid session)
    Depends(require_user)  → User             (raises 401 if no session)
    set_session_cookie(response, session)    — applied on /login
    clear_session_cookie(response)            — applied on /logout

Auth bypass for tests is provided via `TEST_AUTH_BYPASS` env var: when set
to "1", `require_user` returns a synthetic test User. Used ONLY in tests
that don't care about the auth flow itself (e.g. read-only assertions).
Real auth tests do not set the bypass.
"""
from __future__ import annotations

import os

from fastapi import Cookie, HTTPException, Request, Response

from app.persistence import sessions_repo, users_repo
from app.persistence.users_repo import User

COOKIE_NAME = "portal_session"
ENV_COOKIE_NAME = "portal_env"


def _is_secure_cookie() -> bool:
    """Production prod default is Secure=True (HTTPS only).

    Tests / local HTTP set `PORTAL_COOKIE_SECURE=0`. We default to TRUE so
    forgetting the env var fails closed (cookie won't reach the server,
    user notices immediately) rather than silently shipping cookies over
    plaintext.
    """
    raw = os.environ.get("PORTAL_COOKIE_SECURE", "1").strip().lower()
    return raw in ("1", "true", "yes")


def _ttl_hours() -> int:
    try:
        return max(1, int(os.environ.get("SESSION_TTL_HOURS", "24")))
    except ValueError:
        return 24


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=_ttl_hours() * 3600,
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="strict",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def set_env_cookie(response: Response, environment_id: str) -> None:
    """Marca o ambiente ativo na sessão. Mesmo TTL e flags do session cookie."""
    response.set_cookie(
        key=ENV_COOKIE_NAME,
        value=environment_id,
        max_age=_ttl_hours() * 3600,
        httponly=True,
        secure=_is_secure_cookie(),
        samesite="strict",
        path="/",
    )


def clear_env_cookie(response: Response) -> None:
    response.delete_cookie(key=ENV_COOKIE_NAME, path="/")


def _is_test_bypass() -> bool:
    return os.environ.get("TEST_AUTH_BYPASS", "").strip() == "1"


_TEST_USER = User(
    id=0,
    email="test@portal.local",
    role="admin",
    active=True,
    created_at="1970-01-01T00:00:00",
    last_login_at=None,
    password_hash="",  # never used in bypass mode
)


def current_user(
    request: Request,
    portal_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User | None:
    """Return the user behind the request's session cookie, or None.

    Use this when an endpoint should *adapt* based on auth (e.g. show
    extra fields to admins) but still respond unauthenticated. For
    enforcement, use `require_user` instead.
    """
    if _is_test_bypass():
        return _TEST_USER
    if not portal_session:
        return None
    sess = sessions_repo.get_active(portal_session)
    if sess is None:
        return None
    user = users_repo.find_by_id(sess.user_id)
    if user is None or not user.active:
        return None
    # Stash on request.state so downstream code can avoid a second lookup
    request.state.user = user
    return user


def require_user(
    request: Request,
    portal_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    """Enforce that the request has a valid session. 401 otherwise."""
    user = current_user(request, portal_session)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="autenticação requerida",
            headers={"WWW-Authenticate": "Session"},
        )
    return user


def require_admin(
    request: Request,
    portal_session: str | None = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    """Enforce role='admin'. 401 if anonymous, 403 if logged in but not admin."""
    user = require_user(request, portal_session)
    if user.role != "admin":
        raise HTTPException(
            status_code=403,
            detail="apenas administradores podem acessar",
        )
    return user


__all__ = [
    "COOKIE_NAME",
    "ENV_COOKIE_NAME",
    "User",
    "clear_env_cookie",
    "clear_session_cookie",
    "current_user",
    "require_admin",
    "require_user",
    "set_env_cookie",
    "set_session_cookie",
]
