"""Context-local active environment.

O Portal opera em N ambientes (MM, Nasmar, ...) com DBs SQLite separadas.
Em vez de passar `Connection` ou `environment_id` por todo lugar, usamos
um `ContextVar` que carrega o ambiente ativo da request/worker iteration:

- Web: middleware lê cookie `portal_env`, hidrata o ambiente, e usa
  `with active_env(env): ...` ao redor do call do handler. Async-safe.
- Worker: jobs iteram `router.list_env_slugs()` e usam `with active_env(env):`
  por iteração — isolando contagem, conexões e INSERTs por ambiente.

`db.connect()` lê este contexto pra abrir a DB certa. Repos por-ambiente
populam coluna `environment_id` lendo `current_env_id()`.

Tentar usar `db.connect()` sem ambiente ativo levanta `NoActiveEnvironmentError`
— defesa em profundidade contra bug de wiring que escreveria dados sem env.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, TypedDict


class ActiveEnv(TypedDict):
    id: str
    slug: str


class NoActiveEnvironmentError(RuntimeError):
    """Tentativa de operar em DB de ambiente sem ambiente ativo no contexto."""


_active: ContextVar[ActiveEnv | None] = ContextVar("active_env", default=None)


def current() -> ActiveEnv | None:
    """Retorna o ambiente ativo, ou None se não estiver setado."""
    return _active.get()


def current_or_raise() -> ActiveEnv:
    env = _active.get()
    if env is None:
        raise NoActiveEnvironmentError(
            "nenhum ambiente ativo no contexto — esqueceu de embrulhar com active_env()?"
        )
    return env


def current_env_id() -> str:
    """Lê o id do ambiente ativo. Levanta se ausente."""
    return current_or_raise()["id"]


def current_env_slug() -> str:
    """Lê o slug do ambiente ativo. Levanta se ausente."""
    return current_or_raise()["slug"]


@contextmanager
def active_env(env_id: str, slug: str) -> Iterator[None]:
    """Context manager: define o ambiente ativo até o fim do bloco."""
    token = _active.set({"id": env_id, "slug": slug})
    try:
        yield
    finally:
        _active.reset(token)


def set_active_env(env_id: str, slug: str) -> None:
    """Setter explícito — útil em FastAPI middleware antes de chamar handler.

    NÃO use em código de aplicação fora de middleware/setup; prefira
    `with active_env(...): ...` que garante reset automático.
    """
    _active.set({"id": env_id, "slug": slug})


def clear_active_env() -> None:
    """Limpa o ambiente ativo. Útil em logout ou teardown."""
    _active.set(None)
