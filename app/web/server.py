from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.observability.trace import current_trace_id, new_trace_id, with_trace_id
from app.web.middleware.rate_limit import check_and_consume
from app.persistence import invites_repo, sessions_repo, users_repo
from app.security import (
    PasswordTooLongError,
    WeakPasswordError,
    hash_needs_rehash,
    hash_password,
    verify_password,
)
from app.state import (
    EventSource,
    InvalidTransitionError,
    LifecycleEvent,
    transition,
)
from app.web.auth import (
    COOKIE_NAME,
    User,
    clear_env_cookie,
    clear_session_cookie,
    current_user,
    require_admin,
    require_user,
    set_session_cookie,
)


def _is_test_bypass() -> bool:
    return os.environ.get("TEST_AUTH_BYPASS", "").strip() == "1"

from app.web.preview_cache import PreviewConsumedError, PreviewNotFoundError, get_cache

STATIC_DIR = Path(__file__).parent / "static"

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".xls", ".xlsx"}
MAX_PAGE_SIZE = 500

app = FastAPI(title="Portal de Pedidos", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Inject UI-saved Firebird config into os.environ so connection.py picks it up
# without restart. Saved config takes precedence over .env (documented).
from app import firebird_config  # noqa: E402

firebird_config.apply_to_env()

# Multi-ambiente: middleware lê cookie `portal_env` e ativa env no contexto
# para todo o handler. Repos por-ambiente herdam o env via contextvar.
from app.web.middleware.environment import EnvironmentMiddleware  # noqa: E402

app.add_middleware(EnvironmentMiddleware)


# Multi-ambiente: traduz NoActiveEnvironmentError em 412 estruturado para
# que o cliente HTTP possa redirecionar para /selecionar-ambiente em vez
# de quebrar com 500.
from app.persistence.context import NoActiveEnvironmentError  # noqa: E402


@app.exception_handler(NoActiveEnvironmentError)
async def _no_env_handler(_request, _exc):
    return JSONResponse(
        status_code=412,
        content={"detail": "Selecione um ambiente para continuar.", "code": "no_active_env"},
    )

# Inbound webhooks (Fase 4 — Gestor de Produção status updates)
from app.web.webhooks import router as webhooks_router  # noqa: E402

app.include_router(webhooks_router)

# Multi-ambiente: rotas de seleção de ambiente.
from app.web.routes_env_select import router as env_select_router  # noqa: E402

app.include_router(env_select_router)

# Multi-ambiente: CRUD admin.
from app.web.routes_environments import router as environments_router  # noqa: E402

app.include_router(environments_router)


# ── Internal helpers ──────────────────────────────────────────────────────

def _get_cfg() -> dict:
    from app import config as app_config
    return app_config.load()


def _append_log(cfg: dict, entry: dict) -> None:
    """Persist to SQLite. `cfg` kept for signature compatibility."""
    del cfg  # unused — repo resolves db path on its own
    from app.persistence import repo
    repo.insert_import(entry)


def _make_log_entry(
    source_filename: str,
    order_number: Optional[str],
    customer: Optional[str],
    output_files: List[dict],
    status: str,
    error: Optional[str] = None,
    snapshot: Optional[dict] = None,
    fire_codigo: Optional[int] = None,
    db_result: Optional[dict] = None,
    trace_id: Optional[str] = None,
) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "source_filename": source_filename,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": order_number,
        "customer": customer,
        "output_files": output_files,
        "status": status,
        "error": error,
        "snapshot": snapshot,
        "fire_codigo": fire_codigo,
        "db_result": db_result,
        "trace_id": trace_id or current_trace_id() or new_trace_id(),
    }


# ── Preview helpers ───────────────────────────────────────────────────────

def _build_preview_payload(preview_id: str, source_filename: str, order, check: Optional[dict] = None) -> dict:
    """Shape an Order for the preview modal: items, per-store groups, totals, product check."""
    items = []
    for it in order.items:
        items.append({
            "description": it.description,
            "product_code": it.product_code,
            "ean": it.ean,
            "quantity": it.quantity,
            "unit_price": it.unit_price,
            "total_price": it.total_price,
            "obs": it.obs,
            "delivery_date": it.delivery_date,
            "delivery_cnpj": it.delivery_cnpj,
            "delivery_name": it.delivery_name,
        })

    groups: dict[str, dict] = {}
    for it in order.items:
        if it.delivery_cnpj and it.delivery_cnpj != order.header.customer_cnpj:
            key = f"cnpj:{it.delivery_cnpj}"
            label = it.delivery_name or it.delivery_cnpj
        elif it.delivery_name:
            key = f"name:{it.delivery_name}"
            label = it.delivery_name
        else:
            key = "default"
            label = order.header.customer_name or "Pedido"
        g = groups.setdefault(key, {
            "key": key,
            "label": label,
            "cnpj": it.delivery_cnpj,
            "items_count": 0,
            "total_qty": 0.0,
            "total_value": 0.0,
        })
        g["items_count"] += 1
        g["total_qty"] += float(it.quantity or 0)
        g["total_value"] += float(it.total_price or (it.quantity or 0) * (it.unit_price or 0))

    totals = {
        "items_count": len(order.items),
        "total_qty": sum(float(it.quantity or 0) for it in order.items),
        "total_value": sum(
            float(it.total_price or (it.quantity or 0) * (it.unit_price or 0))
            for it in order.items
        ),
    }

    return {
        "preview_id": preview_id,
        "source_filename": source_filename,
        "header": {
            "order_number": order.header.order_number,
            "issue_date": order.header.issue_date,
            "customer_name": order.header.customer_name,
            "customer_cnpj": order.header.customer_cnpj,
        },
        "items": items,
        "groups": sorted(groups.values(), key=lambda g: g["label"] or ""),
        "totals": totals,
        "check": check,
    }


def _run_exporters(order, output_path: Path, *, env: Optional[dict] = None) -> dict:
    """Execute XLSX + Firebird exporters per export_mode; return summary dict.

    `env`: ambiente ativo (dict de environments_repo). Se passado, o
    FirebirdExporter usa as creds do ambiente em vez das env vars FB_*.
    """
    from app import config as app_config
    from app.exporters.erp_exporter import ERPExporter
    from app.exporters.firebird_exporter import FirebirdExporter

    cfg = app_config.load()
    export_mode = cfg.get("export_mode", "xlsx")

    output_files: List[dict] = []
    db_result_dict: Optional[dict] = None
    fire_codigo: Optional[int] = None

    if export_mode in ("xlsx", "both"):
        exporter = ERPExporter()
        paths = exporter.export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]

    if export_mode in ("db", "both"):
        db_exp = FirebirdExporter(env=env)
        db_result = db_exp.export(order)
        db_result_dict = db_result.to_dict()
        fire_codigo = db_result.fire_codigo

    return {
        "output_files": output_files,
        "db_result": db_result_dict,
        "fire_codigo": fire_codigo,
    }


def _process_file(file_path: Path, output_path: Path) -> dict:
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    ext = file_path.suffix.lower()
    raw = file_path.read_bytes()
    loaded = LoadedFile(path=file_path, extension=ext, raw=raw)
    order = process(loaded)
    if not order:
        raise ValueError("Formato não reconhecido ou pedido sem itens")

    exported = _run_exporters(order, output_path)

    return {
        "order_number": order.header.order_number,
        "customer": order.header.customer_name,
        "snapshot": order.model_dump(),
        **exported,
    }


# ── Routes ────────────────────────────────────────────────────────────────

@app.get("/")
def index(request: Request):
    """Dashboard. Redireciona para login se não autenticado, e para
    seleção de ambiente se logado mas sem env ativo (cookie portal_env
    ausente ou inválido — middleware não hidrata request.state.environment).
    """
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    if getattr(request.state, "environment", None) is None and not _is_test_bypass():
        return RedirectResponse(url="/selecionar-ambiente")
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/login")
def login_page() -> FileResponse:
    """Static login form. Talks to /api/auth/login via fetch + sets cookie."""
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/selecionar-ambiente")
def select_environment_page(request: Request):
    """Página estática para escolher o ambiente ativo. Auth é exigida via fetch."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "selecionar-ambiente.html"))


@app.get("/admin/ambientes")
def admin_envs_page(request: Request):
    """Lista CRUD de ambientes (admin-only). API enforce o role."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambientes.html"))


@app.get("/admin/ambientes/novo")
def admin_env_new_page(request: Request):
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambiente-edit.html"))


@app.get("/admin/ambientes/{env_id}")
def admin_env_edit_page(env_id: str, request: Request):  # noqa: ARG001 — env_id consumed in client
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-ambiente-edit.html"))


@app.get("/admin/usuarios")
def admin_users_legacy() -> RedirectResponse:
    """Legacy URL — moved under /configuracoes/usuarios with the redesign."""
    return RedirectResponse(url="/configuracoes/usuarios", status_code=301)


@app.get("/configuracoes/usuarios")
def admin_users_page() -> FileResponse:
    """User-management UI. Auth is enforced by the API endpoints it calls;
    the page itself is static and redirects to /login if /api/auth/me returns null."""
    return FileResponse(str(STATIC_DIR / "admin-usuarios.html"))


@app.get("/configuracoes/banco")
def config_banco_page() -> FileResponse:
    """Firebird connection settings (admin-only — gated client-side by the shell)."""
    return FileResponse(str(STATIC_DIR / "config-banco.html"))


@app.get("/configuracoes/diretorios")
def config_diretorios_page() -> FileResponse:
    """Watch/output directories + export mode (replaces the old in-page modal)."""
    return FileResponse(str(STATIC_DIR / "config-diretorios.html"))


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "importar-pedidos"})


@app.get("/metrics", include_in_schema=False)
def metrics_endpoint() -> Response:
    """Prometheus scrape endpoint. Restrict to internal network in production."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Auth (Fase 4b) ────────────────────────────────────────────────────────


def _login_rate_limit(request: Request) -> None:
    """FastAPI dependency: token-bucket per-IP rate limit on login attempts."""
    ip = (request.client.host if request.client else None) or "unknown"
    if not check_and_consume(key=f"login:{ip}", capacity=10, refill_rate=10 / 900):
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas de login. Tente novamente em 15 minutos.",
            headers={"Retry-After": "900"},
        )


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/login", dependencies=[Depends(_login_rate_limit)])
def login(body: LoginRequest, request: Request) -> JSONResponse:
    """Authenticate by email + password. Sets HttpOnly session cookie.

    Always responds with the same generic message on failure to avoid
    enumeration of registered emails.
    """
    user = users_repo.find_by_email(body.email)
    # Run verify_password unconditionally (with a dummy hash if user is None)
    # to keep timing identical between "no such user" and "wrong password".
    _DUMMY_HASH = "$2b$12$ChXp.TS9Wq8pTNNGxN3lT.LYsVVJrAvZBwR9NLfKMKzVsC9ATcz9G"
    is_valid = verify_password(body.password, user.password_hash if user else _DUMMY_HASH)

    if user is None or not user.active or not is_valid:
        raise HTTPException(status_code=401, detail="email ou senha inválidos")

    # Opportunistic rehash if rounds changed
    if hash_needs_rehash(user.password_hash):
        try:
            users_repo.update_password_hash(user.id, hash_password(body.password))
        except (WeakPasswordError, PasswordTooLongError):
            # User has a legacy weak/long password we no longer accept;
            # leave the existing hash and let them keep logging in.
            pass

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse({
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "session_expires_at": sess.expires_at,
    })
    set_session_cookie(response, sess.token)
    return response


@app.post("/api/auth/logout")
def logout(
    request: Request,
    user: User = Depends(require_user),  # noqa: ARG001 — enforce auth
) -> JSONResponse:
    """Delete the current session. Cookies (sessão e ambiente) cleared on response."""
    token = request.cookies.get("portal_session")
    if token:
        sessions_repo.delete(token)
    response = JSONResponse({"ok": True})
    clear_session_cookie(response)
    clear_env_cookie(response)
    return response


@app.get("/api/auth/me")
def auth_me(
    request: Request,
    user: User | None = Depends(current_user),
) -> JSONResponse:
    """Whoami — usuário + ambiente atual. SPA usa pra renderizar shell."""
    if user is None:
        return JSONResponse({"user": None, "environment": None})
    env = getattr(request.state, "environment", None)
    env_payload = (
        {"id": env["id"], "slug": env["slug"], "name": env["name"]} if env else None
    )
    return JSONResponse({
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "environment": env_payload,
    })


# ── Bootstrap (first-admin signup; closes after first user) ──────────────


@app.get("/api/auth/bootstrap-status")
def bootstrap_status() -> JSONResponse:
    """Tells the login page whether to show the 'create first admin' form.

    Open as long as the `users` table has zero ACTIVE rows. Once the first
    admin exists, this returns `required: false` and `/api/auth/bootstrap`
    starts rejecting with 403.
    """
    return JSONResponse({"required": users_repo.count_active_users() == 0})


class BootstrapRequest(BaseModel):
    email: str
    password: str


@app.post("/api/auth/bootstrap")
def bootstrap_admin(body: BootstrapRequest, request: Request) -> JSONResponse:
    """Create the first admin. ONLY works if no active user exists yet.

    Re-checks the users count under the same connection lifetime to avoid
    a TOCTOU where two simultaneous bootstrap calls each create an admin.
    The UNIQUE(email) constraint helps too if the same email is used.
    """
    if users_repo.count_active_users() > 0:
        raise HTTPException(
            status_code=403,
            detail="bootstrap fechado — já existe administrador ativo",
        )
    try:
        user = users_repo.create_user(
            email=body.email, password=body.password, role="admin",
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse({
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "session_expires_at": sess.expires_at,
    })
    set_session_cookie(response, sess.token)
    return response


# ── Admin user management (admin-only) ───────────────────────────────────


def _user_dto(u: users_repo.User) -> dict:
    return {
        "id": u.id,
        "email": u.email,
        "role": u.role,
        "active": u.active,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


@app.get("/api/admin/users")
def admin_list_users(
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    return JSONResponse({
        "users": [_user_dto(u) for u in users_repo.list_users(limit=500)],
    })


class AdminCreateUserRequest(BaseModel):
    email: str
    password: str
    role: str = "operator"


@app.post("/api/admin/users")
def admin_create_user(
    body: AdminCreateUserRequest,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    try:
        user = users_repo.create_user(
            email=body.email, password=body.password, role=body.role,
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except users_repo.InvalidRoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"user": _user_dto(user)}, status_code=201)


class AdminResetPasswordRequest(BaseModel):
    password: str


@app.post("/api/admin/users/{user_id}/reset-password")
def admin_reset_password(
    user_id: int,
    body: AdminResetPasswordRequest,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    try:
        new_hash = hash_password(body.password)
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    users_repo.update_password_hash(target.id, new_hash)
    # Sign target out everywhere — they must log in with the new password.
    sessions_repo.delete_all_for_user(target.id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/users/{user_id}/deactivate")
def admin_deactivate(
    user_id: int,
    admin_user: User = Depends(require_admin),
) -> JSONResponse:
    if user_id == admin_user.id:
        raise HTTPException(
            status_code=409, detail="você não pode desativar a si mesmo",
        )
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    users_repo.deactivate(user_id)
    sessions_repo.delete_all_for_user(user_id)
    return JSONResponse({"ok": True})


@app.post("/api/admin/users/{user_id}/reactivate")
def admin_reactivate(
    user_id: int,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    target = users_repo.find_by_id(user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="usuário não encontrado")
    users_repo.reactivate(user_id)
    return JSONResponse({"ok": True})


# ── Invitations (admin issues, invitee accepts) ──────────────────────────


def _invite_dto(inv: invites_repo.Invite, request: Request | None = None) -> dict:
    """Public-safe view of an invite. Builds the absolute URL when a request
    is in scope, so the admin UI can show a copy-paste link.
    """
    base = ""
    if request is not None:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return {
        "token": inv.token,
        "email": inv.email,
        "role": inv.role,
        "invited_by_user_id": inv.invited_by_user_id,
        "created_at": inv.created_at,
        "expires_at": inv.expires_at,
        "expired": inv.is_expired(),
        "accepted_at": inv.accepted_at,
        "revoked_at": inv.revoked_at,
        "accept_url": f"{base}/invite/{inv.token}",
    }


class CreateInviteRequest(BaseModel):
    email: str
    role: str = "operator"
    ttl_hours: int | None = None  # None → repo default (7d)


@app.post("/api/admin/invites")
def admin_create_invite(
    body: CreateInviteRequest,
    request: Request,
    admin_user: User = Depends(require_admin),
) -> JSONResponse:
    # Refuse if there's already an active user with this email (vs creating
    # a confusing duplicate-then-fail-on-accept).
    existing_user = users_repo.find_by_email(body.email)
    if existing_user is not None:
        raise HTTPException(
            status_code=409,
            detail=f"já existe usuário com este e-mail (id={existing_user.id})",
        )
    try:
        ttl = body.ttl_hours if body.ttl_hours and body.ttl_hours > 0 else invites_repo.DEFAULT_TTL_HOURS
        inv = invites_repo.create(
            email=body.email,
            role=body.role,
            invited_by_user_id=admin_user.id,
            ttl_hours=ttl,
        )
    except users_repo.InvalidRoleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except invites_repo.OpenInviteExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return JSONResponse({"invite": _invite_dto(inv, request)}, status_code=201)


@app.get("/api/admin/invites")
def admin_list_invites(
    request: Request,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    return JSONResponse({
        "invites": [_invite_dto(inv, request) for inv in invites_repo.list_pending()],
    })


@app.delete("/api/admin/invites/{token}")
def admin_revoke_invite(
    token: str,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    inv = invites_repo.get_by_token(token)
    if inv is None:
        raise HTTPException(status_code=404, detail="convite não encontrado")
    changed = invites_repo.revoke(token)
    if not changed:
        # already accepted or already revoked — return idempotent OK
        return JSONResponse({"ok": True, "noop": True})
    return JSONResponse({"ok": True})


# Public endpoints — invitee uses these. NO auth dependency. They are
# guarded entirely by the secret token in the URL.

@app.get("/api/invites/{token}")
def public_get_invite(token: str) -> JSONResponse:
    """Returns minimal info to render the accept page. 404 if invalid."""
    inv = invites_repo.get_by_token(token)
    if inv is None or inv.is_revoked or inv.is_accepted:
        raise HTTPException(status_code=404, detail="convite inválido ou já utilizado")
    if inv.is_expired():
        raise HTTPException(status_code=410, detail="convite expirado")
    return JSONResponse({
        "email": inv.email,
        "role": inv.role,
        "expires_at": inv.expires_at,
    })


class AcceptInviteRequest(BaseModel):
    password: str


@app.post("/api/invites/{token}/accept")
def public_accept_invite(
    token: str,
    body: AcceptInviteRequest,
    request: Request,
) -> JSONResponse:
    """Accept the invite: create user with the chosen password, mark invite
    used, log the new user in.
    """
    inv = invites_repo.get_by_token(token)
    if inv is None or inv.is_revoked or inv.is_accepted:
        raise HTTPException(status_code=404, detail="convite inválido ou já utilizado")
    if inv.is_expired():
        raise HTTPException(status_code=410, detail="convite expirado")

    # Edge case: someone (admin manually) created a user with this email
    # between issue and accept — refuse rather than collide.
    if users_repo.find_by_email(inv.email) is not None:
        raise HTTPException(
            status_code=409,
            detail="usuário com este e-mail já existe; revogue o convite",
        )

    try:
        user = users_repo.create_user(
            email=inv.email, password=body.password, role=inv.role,
        )
    except WeakPasswordError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except PasswordTooLongError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except users_repo.DuplicateEmailError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        invites_repo.accept_for_user(token, accepted_user_id=user.id)
    except invites_repo.InviteUnusableError as exc:
        # Lost the race: someone consumed the same token concurrently.
        users_repo.deactivate(user.id)
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    users_repo.update_last_login(user.id)
    sess = sessions_repo.create_session(
        user_id=user.id,
        ip=(request.client.host if request.client else None),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    response = JSONResponse({
        "user": {"id": user.id, "email": user.email, "role": user.role},
        "session_expires_at": sess.expires_at,
    }, status_code=201)
    set_session_cookie(response, sess.token)
    return response


@app.get("/invite/{token}")
def invite_accept_page(token: str) -> FileResponse:  # noqa: ARG001 — token used by frontend
    """Public page where invitee sets password. JS reads token from URL."""
    return FileResponse(str(STATIC_DIR / "invite.html"))


@app.get("/api/config")
def get_config() -> JSONResponse:
    cfg = _get_cfg()
    return JSONResponse({
        "watchDir": cfg["watch_dir"],
        "outputDir": cfg["output_dir"],
        "exportMode": cfg.get("export_mode", "xlsx"),
        "firebirdConfigured": (
            firebird_config.is_configured() or bool(os.environ.get("FB_DATABASE"))
        ),
    })


class ConfigUpdate(BaseModel):
    watchDir: Optional[str] = None
    outputDir: Optional[str] = None
    exportMode: Optional[str] = None


@app.post("/api/config")
def update_config(
    body: ConfigUpdate,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config
    watch_dir = str(Path(body.watchDir).expanduser().resolve()) if body.watchDir else None
    output_dir = str(Path(body.outputDir).expanduser().resolve()) if body.outputDir else None
    cfg = app_config.save(watch_dir=watch_dir, output_dir=output_dir, export_mode=body.exportMode)
    return JSONResponse({
        "watchDir": cfg["watch_dir"],
        "outputDir": cfg["output_dir"],
        "exportMode": cfg.get("export_mode", "xlsx"),
    })


# ── Firebird connection config (admin-managed via UI) ────────────────────


class FirebirdConfigUpdate(BaseModel):
    path: Optional[str] = None
    host: Optional[str] = None
    port: Optional[str] = None
    user: Optional[str] = None
    charset: Optional[str] = None
    # Omit `password` to keep current; empty string clears it.
    password: Optional[str] = None


@app.get("/api/firebird/config")
def get_firebird_config(
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Public view — never returns the password (encrypted or otherwise)."""
    cfg = firebird_config.public_view()
    cfg["configured"] = firebird_config.is_configured()
    cfg["passwordSet"] = bool(firebird_config.load()["password_enc"])
    return JSONResponse(cfg)


@app.post("/api/firebird/config")
def save_firebird_config(
    body: FirebirdConfigUpdate,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    payload = {
        "path": body.path or "",
        "host": body.host or "",
        "port": body.port or "",
        "user": body.user or "",
        "charset": body.charset or "",
    }
    firebird_config.save(payload, password=body.password)
    firebird_config.apply_to_env()
    out = firebird_config.public_view()
    out["configured"] = firebird_config.is_configured()
    out["passwordSet"] = bool(firebird_config.load()["password_enc"])
    return JSONResponse(out)


@app.post("/api/firebird/test")
def test_firebird_connection(
    body: FirebirdConfigUpdate,
    _admin: User = Depends(require_admin),
) -> JSONResponse:
    """Try to open a Firebird connection with either the saved config or the
    payload (if any field is provided). Returns ok/error + trace_id for debug.

    The payload's password, when present, is used as-is and not persisted.
    """
    from app.erp.connection import FirebirdConnection
    from app.erp.exceptions import FirebirdConnectionError

    saved = firebird_config.load()
    has_payload_field = any(
        getattr(body, k) not in (None, "")
        for k in ("path", "host", "port", "user", "charset", "password")
    )

    # Resolve effective values (payload field overrides saved when provided).
    def pick(field: str) -> str:
        v = getattr(body, field)
        return v if (v is not None and v != "") else saved.get(field, "") or ""

    eff_path = pick("path")
    eff_host = pick("host")
    eff_port = pick("port") or "3050"
    eff_user = pick("user") or "SYSDBA"
    eff_charset = pick("charset") or "WIN1252"
    if body.password is not None:
        eff_password = body.password
    else:
        eff_password = firebird_config.get_password() or os.environ.get("FB_PASSWORD", "masterkey")

    if not eff_path:
        return JSONResponse(
            {"ok": False, "error": "Caminho do banco (path) é obrigatório.",
             "traceId": current_trace_id()},
            status_code=400,
        )

    # Temporarily mutate os.environ for the duration of the test only when
    # the request supplied overrides — otherwise just use the saved/env state.
    saved_env: dict[str, str | None] = {}
    env_keys = {
        "FB_DATABASE": eff_path,
        "FB_HOST": eff_host,
        "FB_PORT": eff_port,
        "FB_USER": eff_user,
        "FB_CHARSET": eff_charset,
        "FB_PASSWORD": eff_password,
    }
    if has_payload_field:
        for k, v in env_keys.items():
            saved_env[k] = os.environ.get(k)
            if v:
                os.environ[k] = v
            elif k in os.environ:
                del os.environ[k]

    try:
        with FirebirdConnection().connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return JSONResponse({"ok": True, "traceId": current_trace_id()})
    except FirebirdConnectionError as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "traceId": current_trace_id()},
            status_code=400,
        )
    except Exception as exc:  # noqa: BLE001 — surface any driver error
        return JSONResponse(
            {"ok": False, "error": f"Erro inesperado: {exc}",
             "traceId": current_trace_id()},
            status_code=500,
        )
    finally:
        if has_payload_field:
            for k, prev in saved_env.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev


@app.get("/api/pending")
def list_pending() -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])
    imp = app_config.imported_dir(cfg)

    if not watch.exists():
        return JSONResponse({"files": [], "watchDir": cfg["watch_dir"], "exists": False})

    files = []
    for f in sorted(watch.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
        if not f.is_file():
            continue
        if f.suffix.lower() not in ALLOWED_EXTENSIONS:
            continue
        # Exclude anything inside "Pedidos importados" (safety, iterdir is not recursive)
        try:
            stat = f.stat()
            files.append({
                "name": f.name,
                "path": str(f),
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "ext": f.suffix.lower().lstrip("."),
            })
        except Exception:
            pass

    return JSONResponse({"files": files, "watchDir": cfg["watch_dir"], "exists": True})


class ImportRequest(BaseModel):
    files: List[str]
    outputDir: Optional[str] = None


@app.post("/api/import")
def import_files(
    body: ImportRequest,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])
    imp = app_config.imported_dir(cfg)

    output_path = (
        Path(body.outputDir).expanduser().resolve()
        if body.outputDir
        else Path(cfg["output_dir"]).expanduser().resolve()
    )

    try:
        output_path.mkdir(parents=True, exist_ok=True)
        imp.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao criar diretórios: {exc}")

    results = []
    errors = []

    for filename in body.files:
        name = Path(filename).name  # strip any path component — security
        src = watch / name

        if not src.exists() or not src.is_file():
            errors.append({"source": name, "error": "Arquivo não encontrado na pasta de entrada"})
            continue
        if src.suffix.lower() not in ALLOWED_EXTENSIONS:
            errors.append({"source": name, "error": "Extensão não permitida"})
            continue

        try:
            result = _process_file(src, output_path)

            dest = imp / name
            if dest.exists():
                stem, suffix = src.stem, src.suffix
                dest = imp / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
            shutil.move(str(src), str(dest))

            entry = _make_log_entry(
                source_filename=name,
                order_number=result["order_number"],
                customer=result["customer"],
                output_files=result["output_files"],
                status="success",
                snapshot=result.get("snapshot"),
                fire_codigo=result.get("fire_codigo"),
                db_result=result.get("db_result"),
            )
            _append_log(cfg, entry)

            results.append({
                "source": name,
                "order": result["order_number"] or "—",
                "customer": result["customer"] or "—",
                "files": result["output_files"],
                "fire_codigo": result.get("fire_codigo"),
                "entry_id": entry["id"],
            })

        except Exception as exc:
            entry = _make_log_entry(
                source_filename=name,
                order_number=None,
                customer=None,
                output_files=[],
                status="error",
                error=str(exc),
            )
            _append_log(cfg, entry)
            errors.append({"source": name, "error": str(exc)})

    return JSONResponse({"results": results, "errors": errors})


@app.get("/api/imported")
def list_imported(
    limit: int = 100,
    offset: int = 0,
    status: Optional[str] = None,
    portal_status: Optional[str] = None,
    production_status: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> JSONResponse:
    from app.persistence import repo
    entries = repo.list_imports(
        limit=limit,
        offset=offset,
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    total = repo.count_imports(
        status=status,
        portal_status=portal_status,
        production_status=production_status,
        customer_search=q,
        date_from=date_from,
        date_to=date_to,
    )
    return JSONResponse({"entries": entries, "total": total, "limit": limit, "offset": offset})


@app.get("/api/imported/{import_id}")
def get_imported(import_id: str) -> JSONResponse:
    from app.persistence import repo
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Importação não encontrada")
    audit = repo.list_audit(import_id)
    return JSONResponse({"entry": entry, "audit": audit})


class ReimportRequest(BaseModel):
    filename: str
    outputDir: Optional[str] = None


@app.post("/api/reimport")
def reimport_file(
    body: ReimportRequest,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app import config as app_config
    cfg = _get_cfg()
    imp = app_config.imported_dir(cfg)

    name = Path(body.filename).name
    src = imp / name

    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado em 'Pedidos importados'")
    if src.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Extensão não permitida")

    output_path = (
        Path(body.outputDir).expanduser().resolve()
        if body.outputDir
        else Path(cfg["output_dir"]).expanduser().resolve()
    )

    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Erro ao criar diretório de saída: {exc}")

    try:
        result = _process_file(src, output_path)
        entry = _make_log_entry(
            source_filename=name,
            order_number=result["order_number"],
            customer=result["customer"],
            output_files=result["output_files"],
            status="success",
            snapshot=result.get("snapshot"),
            fire_codigo=result.get("fire_codigo"),
            db_result=result.get("db_result"),
        )
        _append_log(cfg, entry)
        return JSONResponse({
            "source": name,
            "order": result["order_number"] or "—",
            "customer": result["customer"] or "—",
            "files": result["output_files"],
            "fire_codigo": result.get("fire_codigo"),
            "entry_id": entry["id"],
        })
    except Exception as exc:
        entry = _make_log_entry(
            source_filename=name,
            order_number=None,
            customer=None,
            output_files=[],
            status="error",
            error=str(exc),
        )
        _append_log(cfg, entry)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/download")
def download_file(path: str) -> FileResponse:
    file_path = Path(path).expanduser().resolve()
    if file_path.suffix.lower() != ".xlsx":
        raise HTTPException(status_code=403, detail="Apenas arquivos .xlsx podem ser baixados")
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado")
    return FileResponse(
        str(file_path),
        filename=file_path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/api/fs")
def browse_filesystem(path: str = "~", file_ext: str = "") -> JSONResponse:
    """Lista subpastas. Quando `file_ext` (ex: '.fdb') é passado, também lista
    arquivos com essa extensão na pasta atual — útil para pickers de arquivo.
    Sem `file_ext`, comportamento histórico (apenas pastas).
    """
    try:
        p = Path(path).expanduser().resolve()
        while not p.exists() or not p.is_dir():
            parent = p.parent
            if parent == p:
                p = Path.home()
                break
            p = parent
        dirs = sorted(
            [
                {"name": e.name, "path": str(e), "type": "dir"}
                for e in p.iterdir()
                if e.is_dir() and not e.name.startswith(".")
            ],
            key=lambda x: x["name"].lower(),
        )
        files: list[dict] = []
        if file_ext:
            ext = file_ext.lower().strip()
            if not ext.startswith("."):
                ext = "." + ext
            files = sorted(
                [
                    {"name": e.name, "path": str(e), "type": "file"}
                    for e in p.iterdir()
                    if e.is_file()
                    and e.suffix.lower() == ext
                    and not e.name.startswith(".")
                ],
                key=lambda x: x["name"].lower(),
            )
        parent = str(p.parent) if p != p.parent else None
        return JSONResponse({
            "current": str(p),
            "parent": parent,
            "entries": dirs,         # mantém shape antigo (só dirs) p/ chamadas legadas
            "files": files,           # adicional, vazio quando file_ext não foi passado
        })
    except PermissionError:
        return JSONResponse({"error": "Sem permissão para acessar este diretório"}, status_code=403)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Preview → Commit flow ─────────────────────────────────────────────────

@app.post("/api/preview")
async def preview_file(
    file: UploadFile = File(...),
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    filename = file.filename or "arquivo"
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
        )

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)

        loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
        order = process(loaded)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()

    if not order:
        raise HTTPException(
            status_code=422,
            detail="Formato não reconhecido ou pedido sem itens",
        )

    from app.erp.product_check import check_order
    check = check_order(order)
    entry = get_cache().put(
        order=order, source_filename=filename, source_bytes=raw, source_ext=ext, check=check,
    )
    payload = _build_preview_payload(entry.preview_id, filename, order, check)
    return JSONResponse(payload)


class PreviewPendingRequest(BaseModel):
    filename: str


@app.post("/api/preview-pending")
def preview_pending(
    body: PreviewPendingRequest,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Preview a file already in the watch folder (no upload)."""
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    cfg = _get_cfg()
    watch = Path(cfg["watch_dir"])

    name = Path(body.filename).name  # strip path components
    src = watch / name
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Arquivo não encontrado na pasta de entrada")
    ext = src.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Tipo de arquivo não suportado: {ext}")
    if src.stat().st_size > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Arquivo excede o limite")

    raw = src.read_bytes()
    loaded = LoadedFile(path=src, extension=ext, raw=raw)
    order = process(loaded)

    if not order:
        raise HTTPException(status_code=422, detail="Formato não reconhecido ou pedido sem itens")

    from app.erp.product_check import check_order
    check = check_order(order)
    entry = get_cache().put(
        order=order,
        source_filename=name,
        source_bytes=raw,
        source_ext=ext,
        source_path=str(src),
        check=check,
    )
    payload = _build_preview_payload(entry.preview_id, name, order, check)
    return JSONResponse(payload)


class CommitRequest(BaseModel):
    preview_id: str


@app.post("/api/commit")
def commit_preview(
    body: CommitRequest,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Salva o pedido no portal como 'em revisão'. NÃO grava no Fire.
    O usuário revisa o match na aba Pedidos e só depois clica em 'Cadastrar no Fire'.
    """
    cfg = _get_cfg()
    try:
        entry = get_cache().consume(body.preview_id)
    except PreviewNotFoundError:
        raise HTTPException(status_code=404, detail="Preview expirado ou inexistente")
    except PreviewConsumedError:
        raise HTTPException(status_code=409, detail="Preview já foi importado")

    order = entry.order

    # Trace_id is minted at this boundary and travels with the pedido for life.
    with with_trace_id() as trace_id:
        log_entry = _make_log_entry(
            source_filename=entry.source_filename,
            order_number=order.header.order_number,
            customer=order.header.customer_name,
            output_files=[],
            status="success",
            snapshot=order.model_dump(),
            trace_id=trace_id,
        )
        log_entry["portal_status"] = "parsed"
        log_entry["check"] = entry.check

        # DB first — if this fails, the file stays in the watch folder and user
        # can retry without losing the original document.
        from app.persistence import repo
        repo.insert_import(log_entry)
        repo.append_audit(
            log_entry["id"],
            "imported_to_portal",
            {
                "source": "preview_commit",
                "items": len(order.items),
                "from_watch": entry.source_path is not None,
                "check": entry.check.get("summary") if entry.check else None,
            },
        )
        transition(
            log_entry["id"],
            LifecycleEvent.IMPORTED,
            source=EventSource.PORTAL,
            payload={
                "items": len(order.items),
                "from_watch": entry.source_path is not None,
                "check_summary": entry.check.get("summary") if entry.check else None,
            },
        )

        # Only move the source after persistence succeeded.
        if entry.source_path:
            from app import config as app_config
            src = Path(entry.source_path)
            if src.exists():
                imp = app_config.imported_dir(cfg)
                imp.mkdir(parents=True, exist_ok=True)
                dest = imp / src.name
                if dest.exists():
                    stem, suffix = src.stem, src.suffix
                    dest = imp / f"{stem}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{suffix}"
                shutil.move(str(src), str(dest))

        return JSONResponse({
            "entry_id": log_entry["id"],
            "order": order.header.order_number or "—",
            "customer": order.header.customer_name or "—",
            "portal_status": "parsed",
            "trace_id": trace_id,
        })


# ── Per-order actions ───────────────────────────────────────────────────

class _FireSendOutcome:
    """Internal result of _send_one_to_fire. HTTP layer translates to status."""
    __slots__ = ("ok", "reason", "http_status", "fire_codigo", "items_inserted", "detail")

    def __init__(
        self,
        ok: bool,
        reason: Optional[str] = None,
        http_status: int = 200,
        fire_codigo: Optional[int] = None,
        items_inserted: int = 0,
        detail: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.http_status = http_status
        self.fire_codigo = fire_codigo
        self.items_inserted = items_inserted
        self.detail = detail


def _send_one_to_fire(import_id: str, cfg: dict, *, request_env: Optional[dict] = None) -> _FireSendOutcome:
    """Insert a parsed order into Fire. Returns structured outcome (no HTTP exceptions)
    so batch callers can aggregate per-item results.

    `request_env`: ambiente atual hidratado pelo middleware. Quando presente,
    o FirebirdExporter conecta com as creds do env (multi-ambiente). Quando
    None, cai no fallback de env vars FB_* (legado).

    State mutation contract:
        - On Fire failure: log SEND_TO_FIRE_FAILED (state stays PARSED).
        - On Fire success: update aux fields then transition SEND_TO_FIRE_SUCCEEDED.
    """
    from app.exporters.firebird_exporter import FirebirdExporter
    from app.persistence import repo
    from app.models.order import Order

    entry = repo.get_import(import_id)
    if entry is None:
        return _FireSendOutcome(False, reason="not_found", http_status=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        return _FireSendOutcome(
            False,
            reason="wrong_status",
            http_status=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        return _FireSendOutcome(
            False, reason="no_snapshot", http_status=422, detail="Snapshot do pedido indisponível"
        )

    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        return _FireSendOutcome(
            False, reason="invalid_snapshot", http_status=422, detail=f"Snapshot inválido: {exc}"
        )

    # Reuse the trace_id minted on commit so logs across commit→send-to-fire
    # are correlated for the same pedido.
    with with_trace_id(entry.get("trace_id")):
        output_path = Path(cfg["output_dir"]).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        export_mode = cfg.get("export_mode", "xlsx")
        output_files: list[dict] = []
        if export_mode in ("xlsx", "both"):
            from app.exporters.erp_exporter import ERPExporter
            paths = ERPExporter().export(order, str(output_path))
            output_files = [{"name": p.name, "path": str(p)} for p in paths]

        override = entry.get("cliente_override_codigo")
        # Multi-ambiente: usa creds do env atual em vez de env vars FB_*.
        result = FirebirdExporter(env=request_env).export(order, override_client_id=override)
        db_result = result.to_dict()

        if result.skipped or result.fire_codigo is None:
            repo.append_audit(
                import_id,
                "send_to_fire_failed",
                {
                    "skip_reason": result.skip_reason,
                    "items_inserted": result.items_inserted,
                    "cliente_override_codigo": override,
                },
            )
            try:
                transition(
                    import_id,
                    LifecycleEvent.SEND_TO_FIRE_FAILED,
                    source=EventSource.PORTAL,
                    payload={
                        "skip_reason": result.skip_reason,
                        "items_inserted": result.items_inserted,
                        "cliente_override_codigo": override,
                    },
                )
            except InvalidTransitionError:
                # State already moved; lifecycle log captured by audit_log.
                pass
            return _FireSendOutcome(
                False,
                reason=result.skip_reason or "no_fire_codigo",
                http_status=409,
                detail=f"Fire rejeitou o pedido: {result.skip_reason or 'sem fire_codigo'}",
            )

        now = datetime.now().isoformat(timespec="seconds")
        repo.update_fire_metadata(
            import_id,
            fire_codigo=result.fire_codigo,
            db_result=db_result,
            output_files=output_files or entry.get("output_files") or [],
            sent_to_fire_at=now,
        )
        repo.append_audit(
            import_id,
            "sent_to_fire",
            {
                "fire_codigo": result.fire_codigo,
                "items_inserted": result.items_inserted,
                "cliente_override_codigo": override,
            },
        )
        transition(
            import_id,
            LifecycleEvent.SEND_TO_FIRE_SUCCEEDED,
            source=EventSource.PORTAL,
            payload={
                "fire_codigo": result.fire_codigo,
                "items_inserted": result.items_inserted,
                "cliente_override_codigo": override,
            },
        )

        return _FireSendOutcome(
            True,
            fire_codigo=result.fire_codigo,
            items_inserted=result.items_inserted,
        )


@app.post("/api/imported/{import_id}/send-to-fire")
def send_to_fire(
    import_id: str,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    cfg = _get_cfg()
    request_env = getattr(request.state, "environment", None)
    outcome = _send_one_to_fire(import_id, cfg, request_env=request_env)
    if not outcome.ok:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return JSONResponse({
        "entry_id": import_id,
        "fire_codigo": outcome.fire_codigo,
        "items_inserted": outcome.items_inserted,
        "portal_status": "sent_to_fire",
    })


class _XlsxExportOutcome:
    """Internal result of _export_one_xlsx. HTTP layer translates to status."""
    __slots__ = ("ok", "reason", "http_status", "output_files", "detail")

    def __init__(
        self,
        ok: bool,
        reason: Optional[str] = None,
        http_status: int = 200,
        output_files: Optional[list[dict]] = None,
        detail: Optional[str] = None,
    ) -> None:
        self.ok = ok
        self.reason = reason
        self.http_status = http_status
        self.output_files = output_files or []
        self.detail = detail


def _export_one_xlsx(import_id: str, cfg: dict) -> _XlsxExportOutcome:
    """Generate XLSX for a parsed order WITHOUT touching Firebird.

    Used when EXPORT_MODE='xlsx'. Audit-only side effect; portal_status stays
    'parsed' so the operator can still cancel or re-export.
    """
    from app.exporters.erp_exporter import ERPExporter
    from app.persistence import repo
    from app.models.order import Order

    entry = repo.get_import(import_id)
    if entry is None:
        return _XlsxExportOutcome(False, reason="not_found", http_status=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        return _XlsxExportOutcome(
            False,
            reason="wrong_status",
            http_status=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        return _XlsxExportOutcome(
            False, reason="no_snapshot", http_status=422, detail="Snapshot do pedido indisponível"
        )

    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        return _XlsxExportOutcome(
            False, reason="invalid_snapshot", http_status=422, detail=f"Snapshot inválido: {exc}"
        )

    with with_trace_id(entry.get("trace_id")):
        output_path = Path(cfg["output_dir"]).expanduser().resolve()
        output_path.mkdir(parents=True, exist_ok=True)
        paths = ERPExporter().export(order, str(output_path))
        output_files = [{"name": p.name, "path": str(p)} for p in paths]

        repo.update_fire_metadata(import_id, output_files=output_files)
        repo.append_audit(import_id, "xlsx_exported", {"files": output_files})

        return _XlsxExportOutcome(True, output_files=output_files)


@app.post("/api/imported/{import_id}/export-xlsx")
def export_xlsx(
    import_id: str,
    _user: User = Depends(require_user),
) -> JSONResponse:
    cfg = _get_cfg()
    outcome = _export_one_xlsx(import_id, cfg)
    if not outcome.ok:
        raise HTTPException(status_code=outcome.http_status, detail=outcome.detail)
    return JSONResponse({
        "entry_id": import_id,
        "output_files": outcome.output_files,
        "portal_status": "parsed",
    })


class BatchSendRequest(BaseModel):
    ids: List[str]


@app.post("/api/batch/send-to-fire")
def batch_send_to_fire(
    body: BatchSendRequest,
    request: Request,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Send multiple parsed orders to Fire. Tolerates partial failures — each id is
    attempted independently; response lists per-id outcome."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Lista de ids vazia")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 pedidos por lote")

    cfg = _get_cfg()
    request_env = getattr(request.state, "environment", None)
    results: list[dict] = []
    ok_count = 0
    fail_count = 0
    for import_id in body.ids:
        outcome = _send_one_to_fire(import_id, cfg, request_env=request_env)
        if outcome.ok:
            ok_count += 1
            results.append({
                "id": import_id,
                "ok": True,
                "fire_codigo": outcome.fire_codigo,
                "items_inserted": outcome.items_inserted,
            })
        else:
            fail_count += 1
            results.append({
                "id": import_id,
                "ok": False,
                "reason": outcome.reason,
                "detail": outcome.detail,
            })

    return JSONResponse({
        "total": len(body.ids),
        "ok": ok_count,
        "failed": fail_count,
        "results": results,
    })


@app.post("/api/batch/export-xlsx")
def batch_export_xlsx(
    body: BatchSendRequest,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Generate XLSX for multiple parsed orders WITHOUT touching Firebird."""
    if not body.ids:
        raise HTTPException(status_code=400, detail="Lista de ids vazia")
    if len(body.ids) > 100:
        raise HTTPException(status_code=400, detail="Máximo 100 pedidos por lote")

    cfg = _get_cfg()
    results: list[dict] = []
    ok_count = 0
    fail_count = 0
    for import_id in body.ids:
        outcome = _export_one_xlsx(import_id, cfg)
        if outcome.ok:
            ok_count += 1
            results.append({
                "id": import_id,
                "ok": True,
                "output_files": outcome.output_files,
            })
        else:
            fail_count += 1
            results.append({
                "id": import_id,
                "ok": False,
                "reason": outcome.reason,
                "detail": outcome.detail,
            })

    return JSONResponse({
        "total": len(body.ids),
        "ok": ok_count,
        "failed": fail_count,
        "results": results,
    })


class CancelRequest(BaseModel):
    reason: Optional[str] = None


# ── Gestor de Produção (Fase 3, gatilho manual + drain inline) ──────────


@app.post("/api/imported/{import_id}/post-to-gestor")
def post_to_gestor(
    import_id: str,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Envia pedido (já em Fire) para o Gestor de Produção.

    Phase 3 wiring: enqueue outbox → transition REQUESTED → drain inline.
    Phase 5 substitutes inline drain by background worker.
    """
    from app.integrations.gestor import (
        GESTOR_TARGET_NAME,
        GestorClient,
        GestorClientError,
        build_gestor_payload,
    )
    from app.models.order import Order
    from app.persistence import outbox_repo, repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "sent_to_fire":
        raise HTTPException(
            status_code=409,
            detail=(
                "Pedido precisa estar em Fire antes do Gestor "
                f"(status atual: {entry.get('portal_status')})"
            ),
        )
    if entry.get("production_status") != "none":
        raise HTTPException(
            status_code=409,
            detail=(
                "Pedido já enviado ao Gestor "
                f"(production_status: {entry.get('production_status')})"
            ),
        )

    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"Snapshot inválido: {exc}"
        ) from exc

    with with_trace_id(entry.get("trace_id")) as trace_id:
        payload_request = build_gestor_payload(
            import_id=import_id,
            order=order,
            metadata={
                "fire_codigo": entry.get("fire_codigo"),
                "trace_id": trace_id,
            },
        )
        idempotency_key = str(uuid.uuid4())

        # 1) Enqueue durably FIRST. If something blows up next, the row is
        #    in DB and a retry is straightforward.
        try:
            row = outbox_repo.enqueue(
                import_id=import_id,
                target=GESTOR_TARGET_NAME,
                endpoint="/v1/orders",
                payload=payload_request.model_dump(),
                idempotency_key=idempotency_key,
            )
        except outbox_repo.OutboxDuplicateError as exc:
            raise HTTPException(
                status_code=409, detail=f"Idempotency key collision: {exc}"
            ) from exc

        # 2) State transition: this is the audit-grade record of the request.
        try:
            transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_REQUESTED,
                source=EventSource.PORTAL,
                payload={"outbox_id": row.id, "idempotency_key": idempotency_key},
            )
        except InvalidTransitionError as exc:
            outbox_repo.mark_failed(
                row.id, error=f"transition_invalid: {exc}", dead=True
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # 3) Drain inline. Phase 5 worker will own this.
        try:
            client = GestorClient()
        except GestorClientError as exc:
            outbox_repo.mark_failed(row.id, error=str(exc))
            transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_FAILED,
                source=EventSource.PORTAL,
                payload={"reason": str(exc)},
            )
            raise HTTPException(
                status_code=503, detail=f"Gestor não configurado: {exc}"
            ) from exc

        try:
            try:
                response = client.create_order(
                    payload_request, idempotency_key=idempotency_key,
                )
            except GestorClientError as exc:
                outbox_repo.mark_failed(row.id, error=str(exc))
                transition(
                    import_id,
                    LifecycleEvent.POST_TO_GESTOR_FAILED,
                    source=EventSource.PORTAL,
                    payload={"reason": str(exc), "status_code": exc.status_code},
                )
                raise HTTPException(
                    status_code=502, detail=f"Gestor rejeitou: {exc}"
                ) from exc

            # 4) Success: persist correlation, mark outbox, transition SENT.
            outbox_repo.mark_sent(row.id, response=response.model_dump())
            repo.set_gestor_order_id(import_id, response.id)
            result = transition(
                import_id,
                LifecycleEvent.POST_TO_GESTOR_SENT,
                source=EventSource.PORTAL,
                payload={
                    "gestor_order_id": response.id,
                    "outbox_id": row.id,
                },
            )
        finally:
            client.close()

        return JSONResponse({
            "entry_id": import_id,
            "gestor_order_id": response.id,
            "production_status": result.production_status.value,
            "outbox_id": row.id,
            "trace_id": trace_id,
        })


@app.post("/api/imported/{import_id}/cancel")
def cancel_import(
    import_id: str,
    body: CancelRequest | None = None,
    _user: User = Depends(require_user),
) -> JSONResponse:
    from app.persistence import repo
    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") == "sent_to_fire":
        raise HTTPException(status_code=409, detail="Pedido já foi enviado ao Fire — não pode ser cancelado pelo portal")

    reason = body.reason if body else None
    with with_trace_id(entry.get("trace_id")):
        repo.append_audit(import_id, "cancelled", {"reason": reason})
        try:
            result = transition(
                import_id,
                LifecycleEvent.CANCELLED,
                source=EventSource.PORTAL,
                payload={"reason": reason},
            )
        except InvalidTransitionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return JSONResponse({
        "entry_id": import_id,
        "portal_status": result.portal_status.value,
    })


# ── Manual cliente override (CLIENT_NOT_FOUND recovery) ─────────────────
#
# When `FirebirdExporter` fails with skip_reason=CLIENT_NOT_FOUND, the user
# can search CADASTRO and pick the right cliente manually. The selection is
# stored as a sidecar on `imports` (não muta snapshot) and consumed by
# `_send_one_to_fire` on the next attempt. Identificação do usuário que
# aplicou o override fica em `audit_log` e em `imports.cliente_override_by`.


@app.get("/api/clientes/search")
def search_clientes(
    q: str,
    limit: int = 20,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Busca clientes ativos em CADASTRO por razão social ou CNPJ.

    `q`: ao menos 2 caracteres (após strip).
    `limit`: clamp em [1, 50].
    503 se Firebird não configurado.
    """
    from app.erp import queries
    from app.erp.connection import FirebirdConnection

    needle = (q or "").strip()
    if len(needle) < 2:
        raise HTTPException(status_code=400, detail="Informe ao menos 2 caracteres")
    limit = max(1, min(int(limit), 50))

    conn_mgr = FirebirdConnection()
    if not conn_mgr.is_configured():
        raise HTTPException(status_code=503, detail="FB_DATABASE não configurado")

    razao_pattern = f"%{needle.upper()}%"
    cnpj_digits = re.sub(r"\D", "", needle)
    # Sentinel pattern that LIKE never matches: avoids OR-injection of
    # rows when the query has no digits at all.
    cnpj_pattern = f"%{cnpj_digits}%" if cnpj_digits else "%__never_matches__%"

    try:
        with conn_mgr.connect() as conn:
            cur = conn.cursor()
            cur.execute(queries.SEARCH_CLIENTS, (razao_pattern, cnpj_pattern))
            rows = cur.fetchall()
            cur.close()
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger
        logger.warning(f"clientes/search falhou ({type(exc).__name__}): {exc}")
        raise HTTPException(status_code=502, detail="Falha consultando o Fire") from exc

    results = [
        {
            "codigo": int(r[0]) if r[0] is not None else None,
            "razao_social": (r[1] or "").strip() if r[1] else None,
            "cpf_cnpj": (r[2] or "").strip() if r[2] else None,
        }
        for r in rows[:limit]
    ]
    return JSONResponse({"results": results, "total_returned": len(results)})


class ClienteOverrideRequest(BaseModel):
    cliente_codigo: int
    reason: Optional[str] = None


@app.post("/api/imported/{import_id}/override-cliente")
def override_cliente(
    import_id: str,
    body: ClienteOverrideRequest,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Aplica seleção manual de cliente a um pedido em revisão.

    Não muda portal_status (override é metadado sidecar, não evento de SM).
    Registra em `audit_log` (`cliente_override_selected`) e em
    `imports.cliente_override_by` o email do usuário autenticado.
    """
    from app.erp import queries
    from app.erp.connection import FirebirdConnection
    from app.persistence import repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Override só é permitido em pedidos em revisão "
                f"(status atual: {entry.get('portal_status')})"
            ),
        )

    conn_mgr = FirebirdConnection()
    if not conn_mgr.is_configured():
        raise HTTPException(status_code=503, detail="FB_DATABASE não configurado")

    try:
        with conn_mgr.connect() as conn:
            cur = conn.cursor()
            cur.execute(queries.FIND_CLIENT_BY_CODIGO, (body.cliente_codigo,))
            row = cur.fetchone()
            cur.close()
    except Exception as exc:  # noqa: BLE001
        from app.utils.logger import logger
        logger.warning(f"override-cliente falhou ({type(exc).__name__}): {exc}")
        raise HTTPException(status_code=502, detail="Falha consultando o Fire") from exc

    if row is None:
        raise HTTPException(
            status_code=422,
            detail=f"Cliente CODIGO={body.cliente_codigo} não encontrado em CADASTRO ou inativo",
        )

    razao = (row[1] or "").strip() if row[1] else ""
    actor = user.email

    with with_trace_id(entry.get("trace_id")):
        repo.set_client_override(
            import_id, codigo=int(row[0]), razao=razao, user=actor,
        )
        repo.append_audit(
            import_id,
            "cliente_override_selected",
            {
                "cliente_codigo": int(row[0]),
                "cliente_razao": razao,
                "previous_cnpj": entry.get("customer_cnpj"),
                "reason": body.reason,
                "user_id": user.id,
                "user_email": actor,
            },
        )

    return JSONResponse({
        "entry_id": import_id,
        "cliente_override_codigo": int(row[0]),
        "cliente_override_razao": razao,
    })


@app.get("/api/imported/{import_id}/preview")
def rehydrate_preview(import_id: str) -> JSONResponse:
    """Rebuild the preview payload for a stored order (for the review modal)."""
    from app.persistence import repo
    from app.models.order import Order

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    check = entry.get("check")
    payload = _build_preview_payload(import_id, entry.get("source_filename", ""), order, check)
    payload["portal_status"] = entry.get("portal_status")
    payload["fire_codigo"] = entry.get("fire_codigo")

    # Surface the manual cliente override into the check banner so the UI shows
    # it in green instead of the original "✗ Cliente não encontrado" red flag.
    override_codigo = entry.get("cliente_override_codigo")
    if override_codigo and payload.get("check"):
        payload["check"]["client"] = {
            "match": True,
            "fire_id": override_codigo,
            "razao_social": entry.get("cliente_override_razao"),
            "cnpj": entry.get("customer_cnpj"),
            "override": True,
        }
        if "summary" in payload["check"]:
            payload["check"]["summary"]["client_matched"] = True
    payload["cliente_override"] = (
        {
            "codigo": override_codigo,
            "razao_social": entry.get("cliente_override_razao"),
            "at": entry.get("cliente_override_at"),
            "by": entry.get("cliente_override_by"),
        }
        if override_codigo
        else None
    )
    return JSONResponse(payload)


# Keep /api/process for backward compat (drag-drop upload flow)
@app.post("/api/process")
async def process_files(
    _user: User = Depends(require_user),
    files: List[UploadFile] = File(...),
    output_dir: str = Form("output"),
) -> JSONResponse:
    from app.exporters.erp_exporter import ERPExporter
    from app.ingestion.file_loader import LoadedFile
    from app.pipeline import process

    output_path = Path(output_dir).expanduser().resolve()
    try:
        output_path.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return JSONResponse({
            "results": [],
            "errors": [{"source": "—", "error": f"Pasta inválida: {exc}"}],
        })

    exporter = ERPExporter()
    results = []
    errors = []

    for upload in files:
        filename = upload.filename or "arquivo"
        ext = Path(filename).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            errors.append({"source": filename, "error": f"Tipo de arquivo não suportado: {ext}"})
            continue

        raw = await upload.read()

        if len(raw) > MAX_UPLOAD_BYTES:
            errors.append({
                "source": filename,
                "error": f"Arquivo excede o limite de {MAX_UPLOAD_BYTES // (1024 * 1024)} MB",
            })
            continue

        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(raw)
                tmp_path = Path(tmp.name)

            loaded = LoadedFile(path=tmp_path, extension=ext, raw=raw)
            order = process(loaded)

            if order:
                paths = exporter.export(order, str(output_path))
                results.append({
                    "source": filename,
                    "order": order.header.order_number or "—",
                    "files": [{"name": p.name, "path": str(p)} for p in paths],
                })
            else:
                errors.append({
                    "source": filename,
                    "error": "Formato não reconhecido ou pedido sem itens",
                })
        except Exception as exc:
            errors.append({"source": filename, "error": str(exc)})
        finally:
            if tmp_path and tmp_path.exists():
                tmp_path.unlink()

    return JSONResponse({"results": results, "errors": errors})
