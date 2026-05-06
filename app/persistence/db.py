"""SQLite legacy facade — agora roteia para `router.shared_connect()` ou
`router.env_connect(slug)` baseado no contexto.

API pública preservada para compatibilidade:
- `connect()`: abre conexão do **ambiente ativo** (lê `context.active_env`).
  Sem ambiente ativo, levanta `NoActiveEnvironmentError`. Usar para tabelas
  por-ambiente: imports, audit_log, order_lifecycle_events, outbox.
- `connect_shared()`: abre conexão da **DB compartilhada** (auth, sessões,
  ambientes, idempotência, rate-limit). Usar nessas tabelas.
- `db_path()`: caminho da DB compartilhada — usado pelo APScheduler jobstore.
- `init()`: garante schema compartilhado e, se ambiente ativo, schema do env.
- `set_db_path(file_or_none)`, `reset_init_cache()`: helpers de teste.
  `set_db_path(p)` marca `APP_DATA_DIR=p.parent` e ativa um ambiente "test".
  `set_db_path(None)` desativa o ambiente e limpa override.

Migração:
- Repos novos usam diretamente `app/persistence/router.py`.
- Código legado (state_machine, repo.py, outbox_repo.py) chama `connect()` e
  herda o ambiente ativo do middleware/worker.
- Testes legados que chamavam `db.set_db_path` continuam funcionando porque
  o helper auto-ativa um ambiente "test".
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from app.persistence import context as env_context
from app.persistence import router

_DEFAULT_TEST_ENV_ID = "test-env-id"
_DEFAULT_TEST_ENV_SLUG = "test"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Conexão da DB do ambiente ativo (env_connect).

    Levanta NoActiveEnvironmentError se nenhum ambiente foi ativado no
    contexto — chame com `with active_env(...): ...` ou via middleware web.
    """
    slug = env_context.current_env_slug()
    with router.env_connect(slug) as conn:
        yield conn


@contextmanager
def connect_shared() -> Iterator[sqlite3.Connection]:
    """Conexão da DB compartilhada (auth/env metadata)."""
    with router.shared_connect() as conn:
        yield conn


def db_path() -> Path:
    """Caminho da DB compartilhada (mantido como `db_path` por
    compatibilidade — APScheduler jobstore usa este path)."""
    return router.shared_db_path()


def init() -> None:
    """Garante schema da shared, e se há ambiente ativo, do env também."""
    with router.shared_connect():
        pass
    cur = env_context.current()
    if cur is not None:
        with router.env_connect(cur["slug"]):
            pass


def reset_init_cache() -> None:
    """Reset cache de inicialização do router (apenas o cache de schemas).
    NÃO limpa ambiente ativo — para isso use `set_db_path(None)`.
    """
    router.reset_init_cache()


def set_db_path(path: Optional[Path]) -> None:
    """Helper de teste: aponta `APP_DATA_DIR` pra parent dir e ativa
    um ambiente "test" (id=test-env-id, slug=test).

    Compatibilidade com o padrão antigo `db.set_db_path(tmp_path / 'app_state.db')`:
    o arquivo legado vira o **diretório** de dados, e os arquivos reais
    serão `<tmp_path>/app_shared.db` e `<tmp_path>/app_state_test.db`.

    `set_db_path(None)` limpa override e desativa o ambiente.
    """
    if path is None:
        os.environ.pop("APP_DATA_DIR", None)
        env_context.clear_active_env()
        return
    p = Path(path)
    target_dir = p.parent if p.suffix in (".db", ".sqlite", ".sqlite3") else p
    target_dir.mkdir(parents=True, exist_ok=True)
    os.environ["APP_DATA_DIR"] = str(target_dir)
    env_context.set_active_env(_DEFAULT_TEST_ENV_ID, _DEFAULT_TEST_ENV_SLUG)
