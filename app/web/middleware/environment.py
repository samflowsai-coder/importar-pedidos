"""Middleware FastAPI: lê o cookie `portal_env` e ativa o ambiente no contexto.

Assina-se via `app.add_middleware(EnvironmentMiddleware)`. Para cada request:

- Lê `portal_env` cookie (se houver)
- Carrega via `environments_repo.get(env_id)`
- Se ativo, embrulha o handler com `env_context.active_env(...)` — repos
  por-ambiente que chamarem `db.connect()` durante o handler verão a DB
  certa, e INSERTs populam `environment_id` automaticamente.

Sem cookie ou ambiente inválido → o handler roda sem contexto. Rotas que
exigem ambiente declaram `Depends(current_environment)` que retorna 412
nesse caso.

Rotas administrativas (CRUD de ambientes, gestão de usuários) não declaram
a dependency — funcionam sem ambiente selecionado. É como o operador
configura o sistema antes de escolher.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.persistence import context as env_context
from app.persistence import environments_repo
from app.web.auth import ENV_COOKIE_NAME


class EnvironmentMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        env_id = request.cookies.get(ENV_COOKIE_NAME)
        env: dict | None = None
        if env_id:
            try:
                env = environments_repo.get(env_id)
                if env and not env["is_active"]:
                    env = None
            except Exception:
                env = None

        if env is None:
            return await call_next(request)

        # Anexa o env ao request.state para que dependencies leiam barato.
        request.state.environment = env
        with env_context.active_env(env["id"], env["slug"]):
            response: Response = await call_next(request)
        return response
