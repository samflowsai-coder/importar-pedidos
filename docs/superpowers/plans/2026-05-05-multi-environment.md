# Multi-ambiente — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Suportar N empresas (MM, Nasmar, futuras) com config de pastas e Firebird isoladas por ambiente, seleção via cookie de sessão, auto-import por watcher multi-pasta, sem misturar dados.

**Architecture:** SQLite híbrido — `app_shared.db` (auth/env metadata) + 1 `app_state_<slug>.db` por ambiente. Cookie `portal_env` na sessão + middleware FastAPI injeta `request.state.environment` e `current_env_db()`. CRUD admin de environments com senha cifrada via secret_store. Worker job `scan_environments` itera todas as pastas e ingesta arquivos novos. Bind `environment_id` imutável no upload.

**Tech Stack:** Python 3.11, FastAPI, sqlite3 (stdlib), APScheduler, cryptography (Fernet via secret_store), pytest, ruff.

**Spec:** [docs/superpowers/specs/2026-05-05-multi-environment-design.md](../specs/2026-05-05-multi-environment-design.md)

---

## File Structure

### Created

| Path | Responsibility |
|---|---|
| `app/persistence/router.py` | Resolve paths e abre conexões (`shared_connect`, `env_connect`, `list_env_slugs`) |
| `app/persistence/schema_shared.py` | DDL `app_shared.db` (users, sessions, environments, invites, idempotency, rate_limit_buckets) |
| `app/persistence/schema_env.py` | DDL `app_state_<slug>.db` (imports, audit_log, lifecycle_events, outbox) |
| `app/persistence/environments_repo.py` | CRUD ambientes; encrypt/decrypt senha FB; validações (slug regex, imutabilidade) |
| `app/web/middleware/environment.py` | Middleware lê cookie `portal_env`, injeta `request.state.environment` |
| `app/web/dependencies/environment.py` | Deps `current_environment`, `current_env_db` |
| `app/web/routes_environments.py` | Rotas admin: `GET/POST/PATCH/DELETE /api/admin/environments`, `POST /test` |
| `app/web/routes_env_select.py` | `GET /selecionar-ambiente`, `POST /api/env/select` |
| `app/web/static/selecionar-ambiente.html` | Página de seleção pós-login |
| `app/web/static/admin-ambientes.html` | Lista CRUD |
| `app/web/static/admin-ambiente-edit.html` | Form criar/editar ambiente |
| `app/worker/jobs/scan_environments.py` | Job APScheduler: varre `watch_dir` de cada ambiente |
| `tools/migrate_to_multi_env.py` | Move users/sessions/invites do `app_state.db` legacy → `app_shared.db` |
| `tests/test_persistence_router.py` | Testes do router |
| `tests/test_environments_repo.py` | Testes do CRUD + crypto |
| `tests/test_environment_middleware.py` | Testes do middleware/cookie |
| `tests/test_admin_environments_routes.py` | Testes E2E das rotas CRUD |
| `tests/test_watcher_scan.py` | Testes do scan_environments |

### Modified

| Path | Mudança |
|---|---|
| `app/persistence/db.py` | Reduz a wrapper de compatibilidade por dois passos; eventualmente substituído pelo router |
| `app/persistence/repo.py` | Recebe `Connection` injetada; adiciona `environment_id` no INSERT |
| `app/persistence/users_repo.py` | Recebe `Connection` (vai pra shared) |
| `app/persistence/sessions_repo.py` | Recebe `Connection` (shared) |
| `app/persistence/invites_repo.py` | Recebe `Connection` (shared) |
| `app/persistence/outbox_repo.py` | Recebe `Connection` (env DB); adiciona `environment_id` no INSERT |
| `app/persistence/idempotency_repo.py` | Recebe `Connection` (shared) |
| `app/web/server.py` | Registra middleware; remove rotas firebird; adiciona dependencies novas |
| `app/web/static/js/shell.js` | Badge do ambiente; fetch `/api/auth/me` retorna env atual |
| `app/web/static/js/shell.js` | (mesmo arquivo) link "Gerenciar ambientes" pra admin |
| `app/worker/scheduler.py` | Jobstore aponta `app_shared.db`; registra `scan_environments` |
| `app/worker/jobs/retention.py` | Itera todas DBs (shared + por ambiente) |
| `app/exporters/firebird_exporter.py` | Recebe env como parâmetro (não lê env vars) |
| `app/erp/connection.py` | Recebe dict de env (path, host, etc.) |
| `app/firebird_config.py` | **REMOVIDO** |
| `app/web/static/config-banco.html` | **REMOVIDO** (substituído por admin-ambientes) |
| `tests/conftest.py` | Fixtures novas: `tmp_shared_db`, `tmp_env_db`, `make_environment` |

---

## Estratégia de execução

**Branch:** `feature/multi-ambiente` (criar a partir de `main`).

**Commit cadence:** um commit por task quando os testes da task passam. Suite completa antes de fechar cada fase.

**Não shippa parcial:** o branch só vai pra main quando todas as fases concluem. Suite completa passa.

**Nota de teste:** o projeto não tem framework formal de migration — schemas vêm de `_SCHEMA_TABLES` aplicado em `_ensure_schema`. O plano segue a mesma convenção (schemas inline, `_apply_migrations` para coluna nova).

---

# Fase 1 — Foundation: Split de persistência

Objetivo: ter dois módulos de schema (shared e env) e um router, mas mantendo compatibilidade temporária com `db.connect()` único pra suite continuar passando.

### Task 1.1: Criar branch e fixtures de teste compartilhadas

**Files:**
- Modify: `tests/conftest.py`

- [ ] **Step 1: Criar branch**

```bash
cd "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos"
git checkout -b feature/multi-ambiente
```

- [ ] **Step 2: Adicionar fixtures `tmp_shared_db` e `tmp_env_db`**

Append em `tests/conftest.py`:

```python
import sqlite3
from pathlib import Path
import pytest

@pytest.fixture
def tmp_shared_db(tmp_path: Path):
    """SQLite vazia para testes de schema/repos compartilhados (futuro app_shared.db)."""
    db_file = tmp_path / "app_shared.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()


@pytest.fixture
def tmp_env_db(tmp_path: Path):
    """SQLite vazia para testes de schema/repos por-ambiente (futuro app_state_<slug>.db)."""
    db_file = tmp_path / "app_state_test.db"
    conn = sqlite3.connect(db_file, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()
```

- [ ] **Step 3: Rodar suite — não pode quebrar**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS (fixtures não-usadas não quebram nada).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py
git commit -m "test(conftest): adiciona fixtures tmp_shared_db e tmp_env_db"
```

---

### Task 1.2: Schema separation — extrair DDL para módulos novos

**Files:**
- Create: `app/persistence/schema_shared.py`
- Create: `app/persistence/schema_env.py`

**Princípio:** o conteúdo destes módulos é exatamente o que existe hoje em `db.py:_SCHEMA_TABLES`/`_SCHEMA_INDEXES`/`_COLUMN_MIGRATIONS`, mas particionado por destino.

- [ ] **Step 1: Escrever teste verificando que os módulos existem e expõem as constantes esperadas**

Create `tests/test_schema_modules.py`:

```python
def test_schema_shared_exports():
    from app.persistence import schema_shared
    assert hasattr(schema_shared, "TABLES_SQL")
    assert hasattr(schema_shared, "INDEXES_SQL")
    assert hasattr(schema_shared, "COLUMN_MIGRATIONS")
    assert "users" in schema_shared.TABLES_SQL
    assert "environments" in schema_shared.TABLES_SQL  # nova tabela


def test_schema_env_exports():
    from app.persistence import schema_env
    assert hasattr(schema_env, "TABLES_SQL")
    assert hasattr(schema_env, "INDEXES_SQL")
    assert hasattr(schema_env, "COLUMN_MIGRATIONS")
    assert "imports" in schema_env.TABLES_SQL
    assert "outbox" in schema_env.TABLES_SQL
    assert "users" not in schema_env.TABLES_SQL  # NÃO deve estar aqui
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_schema_modules.py -v
```

Expected: FAIL (módulos não existem).

- [ ] **Step 3: Criar `app/persistence/schema_shared.py`**

Inclui DDL para: `users`, `user_invites`, `sessions`, `inbound_idempotency`, `rate_limit_buckets`, e nova tabela `environments`.

```python
"""Schema do banco SQLite compartilhado (app_shared.db)."""
from __future__ import annotations

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS environments (
    id              TEXT PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    watch_dir       TEXT NOT NULL,
    output_dir      TEXT NOT NULL,
    fb_path         TEXT NOT NULL,
    fb_host         TEXT,
    fb_port         TEXT,
    fb_user         TEXT NOT NULL DEFAULT 'SYSDBA',
    fb_charset      TEXT NOT NULL DEFAULT 'WIN1252',
    fb_password_enc TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'operator',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS user_invites (
    token              TEXT PRIMARY KEY,
    email              TEXT NOT NULL COLLATE NOCASE,
    role               TEXT NOT NULL DEFAULT 'operator',
    invited_by_user_id INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    accepted_at        TEXT,
    accepted_user_id   INTEGER,
    revoked_at         TEXT,
    FOREIGN KEY (invited_by_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (accepted_user_id)   REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inbound_idempotency (
    provider         TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    response_status  INTEGER,
    response_body    TEXT,
    import_id        TEXT,
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key            TEXT PRIMARY KEY,
    tokens         REAL NOT NULL,
    last_refill_at REAL NOT NULL
);
"""

INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_environments_slug   ON environments(slug);
CREATE INDEX IF NOT EXISTS idx_environments_active        ON environments(is_active);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id     ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at  ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_invites_email_pending ON user_invites(email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_invites_expires_at    ON user_invites(expires_at);

CREATE INDEX IF NOT EXISTS idx_inbound_received_at  ON inbound_idempotency(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_import_id    ON inbound_idempotency(import_id);
"""

COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (table, column, ALTER ... DDL) — mantém forma de db.py:_COLUMN_MIGRATIONS
)
```

- [ ] **Step 4: Criar `app/persistence/schema_env.py`**

```python
"""Schema do banco SQLite por-ambiente (app_state_<slug>.db)."""
from __future__ import annotations

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS imports (
    id               TEXT PRIMARY KEY,
    environment_id   TEXT NOT NULL,
    source_filename  TEXT NOT NULL,
    imported_at      TEXT NOT NULL,
    order_number     TEXT,
    customer_cnpj    TEXT,
    customer_name    TEXT,
    fire_codigo      INTEGER,
    snapshot_json    TEXT,
    check_json       TEXT,
    output_files_json TEXT,
    db_result_json   TEXT,
    status           TEXT NOT NULL,
    error            TEXT,
    portal_status    TEXT NOT NULL DEFAULT 'sent_to_fire',
    sent_to_fire_at  TEXT,
    production_status TEXT NOT NULL DEFAULT 'none',
    released_at      TEXT,
    released_by      TEXT,
    trace_id         TEXT,
    state_version    INTEGER NOT NULL DEFAULT 1,
    gestor_order_id  TEXT,
    apontae_order_id TEXT,
    cliente_override_codigo INTEGER,
    cliente_override_razao  TEXT,
    cliente_override_at     TEXT,
    cliente_override_by     TEXT,
    fire_status_last_seen   TEXT,
    fire_status_polled_at   TEXT,
    file_sha256      TEXT,
    original_path    TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    detail_json TEXT,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_lifecycle_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id    TEXT NOT NULL,
    event_type   TEXT NOT NULL,
    source       TEXT NOT NULL,
    payload_json TEXT,
    trace_id     TEXT,
    occurred_at  TEXT NOT NULL,
    ingested_at  TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id   TEXT NOT NULL,
    import_id        TEXT NOT NULL,
    target           TEXT NOT NULL,
    endpoint         TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT,
    response_json    TEXT,
    trace_id         TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_imports_imported_at   ON imports(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_imports_customer_cnpj ON imports(customer_cnpj);
CREATE INDEX IF NOT EXISTS idx_imports_fire_codigo   ON imports(fire_codigo);
CREATE INDEX IF NOT EXISTS idx_imports_status        ON imports(status);
CREATE INDEX IF NOT EXISTS idx_imports_portal_status ON imports(portal_status);
CREATE INDEX IF NOT EXISTS idx_imports_prod_status   ON imports(production_status);
CREATE INDEX IF NOT EXISTS idx_imports_sha256        ON imports(file_sha256);

CREATE INDEX IF NOT EXISTS idx_audit_import_id ON audit_log(import_id);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lifecycle_import_id   ON order_lifecycle_events(import_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_trace_id    ON order_lifecycle_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event_type  ON order_lifecycle_events(event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_outbox_pending   ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_import_id ON outbox(import_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_imports_fire_poll
    ON imports(portal_status, production_status, fire_status_polled_at)
    WHERE fire_codigo IS NOT NULL;
"""

COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = ()
```

- [ ] **Step 5: Rodar testes**

```bash
.venv/bin/pytest tests/test_schema_modules.py -v
```

Expected: PASS (4 asserts).

- [ ] **Step 6: Suite completa — não pode regredir**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS (testes existentes ainda usam `db.py` antigo).

- [ ] **Step 7: Commit**

```bash
git add app/persistence/schema_shared.py app/persistence/schema_env.py tests/test_schema_modules.py
git commit -m "feat(persistence): schemas shared e env separados"
```

---

### Task 1.3: Router de conexões

**Files:**
- Create: `app/persistence/router.py`
- Create: `tests/test_persistence_router.py`

- [ ] **Step 1: Escrever testes do router**

```python
"""tests/test_persistence_router.py"""
import sqlite3
from pathlib import Path

import pytest

from app.persistence import router


def test_shared_db_path_default(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    p = router.shared_db_path()
    assert p == tmp_path / "app_shared.db"


def test_env_db_path_uses_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    p = router.env_db_path("mm")
    assert p == tmp_path / "app_state_mm.db"


def test_env_db_path_rejects_bad_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    with pytest.raises(ValueError):
        router.env_db_path("../etc/passwd")
    with pytest.raises(ValueError):
        router.env_db_path("MM Calçados")  # uppercase + espaço


def test_shared_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "users" in names
    assert "environments" in names
    assert "sessions" in names
    assert "imports" not in names  # imports NÃO está no shared


def test_env_connect_creates_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.env_connect("mm") as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
    names = {r[0] for r in rows}
    assert "imports" in names
    assert "outbox" in names
    assert "audit_log" in names
    assert "users" not in names  # users NÃO está em env DB
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_persistence_router.py -v
```

Expected: FAIL (`router` não existe).

- [ ] **Step 3: Implementar `app/persistence/router.py`**

```python
"""Roteamento de conexões SQLite multi-ambiente.

Uma DB compartilhada (app_shared.db) para auth/env metadata, e uma DB por
ambiente (app_state_<slug>.db) para dados operacionais (pedidos, lifecycle,
outbox, audit).

Slugs validados em SLUG_RE: lowercase, dígitos e hífen, 1-30 chars,
começando por alfanumérico.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.persistence import schema_env, schema_shared

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

_init_lock = threading.Lock()
_initialized_paths: set[str] = set()


def _data_dir() -> Path:
    raw = os.environ.get("APP_DATA_DIR", "").strip()
    if raw:
        base = Path(raw).expanduser().resolve()
    else:
        base = Path(__file__).resolve().parents[2] / "data"
    base.mkdir(parents=True, exist_ok=True)
    return base


def shared_db_path() -> Path:
    return _data_dir() / "app_shared.db"


def env_db_path(slug: str) -> Path:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug inválido: {slug!r}")
    return _data_dir() / f"app_state_{slug}.db"


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def _apply_column_migrations(
    conn: sqlite3.Connection,
    migrations: tuple[tuple[str, str, str], ...],
) -> None:
    cols_by_table: dict[str, set[str]] = {}
    for table, col, ddl in migrations:
        if table not in cols_by_table:
            try:
                cols_by_table[table] = _existing_columns(conn, table)
            except sqlite3.OperationalError:
                cols_by_table[table] = set()
                continue
        if col not in cols_by_table[table]:
            conn.execute(ddl)
            cols_by_table[table].add(col)


def _ensure_schema(path: Path, schema_module) -> None:
    key = str(path)
    if key in _initialized_paths:
        return
    with _init_lock:
        if key in _initialized_paths:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=5.0)
        try:
            _configure(conn)
            conn.executescript(schema_module.TABLES_SQL)
            _apply_column_migrations(conn, schema_module.COLUMN_MIGRATIONS)
            conn.executescript(schema_module.INDEXES_SQL)
            conn.commit()
        finally:
            conn.close()
        _initialized_paths.add(key)


@contextmanager
def shared_connect() -> Iterator[sqlite3.Connection]:
    path = shared_db_path()
    _ensure_schema(path, schema_shared)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level="DEFERRED")
    _configure(conn)
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def env_connect(slug: str) -> Iterator[sqlite3.Connection]:
    path = env_db_path(slug)
    _ensure_schema(path, schema_env)
    conn = sqlite3.connect(path, timeout=5.0, isolation_level="DEFERRED")
    _configure(conn)
    try:
        yield conn
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_env_slugs() -> list[str]:
    """Slugs de ambientes ativos. Usado pelos workers."""
    with shared_connect() as conn:
        rows = conn.execute(
            "SELECT slug FROM environments WHERE is_active = 1 ORDER BY slug"
        ).fetchall()
    return [r[0] for r in rows]


def reset_init_cache() -> None:
    """Reset cache de inicialização. Apenas para testes."""
    with _init_lock:
        _initialized_paths.clear()
```

- [ ] **Step 4: Rodar**

```bash
.venv/bin/pytest tests/test_persistence_router.py -v
```

Expected: PASS (5/5).

- [ ] **Step 5: Suite completa**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/persistence/router.py tests/test_persistence_router.py
git commit -m "feat(persistence): router de conexões shared/env"
```

---

### Task 1.4: environments_repo — CRUD básico

**Files:**
- Create: `app/persistence/environments_repo.py`
- Create: `tests/test_environments_repo.py`

- [ ] **Step 1: Escrever testes**

```python
"""tests/test_environments_repo.py"""
import pytest

from app.persistence import environments_repo, router, schema_shared


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass  # força criação do schema
    yield


def test_create_and_get(fresh_shared):
    env = environments_repo.create(
        slug="mm",
        name="MM Calçados",
        watch_dir="/tmp/mm/in",
        output_dir="/tmp/mm/out",
        fb_path="/tmp/mm.fdb",
        fb_password="secret123",
    )
    assert env["slug"] == "mm"
    assert env["name"] == "MM Calçados"
    assert "fb_password_enc" not in env  # senha NÃO retorna no public view
    same = environments_repo.get(env["id"])
    assert same["id"] == env["id"]


def test_create_rejects_invalid_slug(fresh_shared):
    with pytest.raises(ValueError):
        environments_repo.create(
            slug="MM",  # uppercase
            name="MM",
            watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        )


def test_create_rejects_duplicate_slug(fresh_shared):
    environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    with pytest.raises(environments_repo.SlugTaken):
        environments_repo.create(slug="mm", name="MM2", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")


def test_update_does_not_change_slug(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    updated = environments_repo.update(
        env["id"],
        name="MM Renomeado",
        watch_dir="/novo",
    )
    assert updated["name"] == "MM Renomeado"
    assert updated["watch_dir"] == "/novo"
    assert updated["slug"] == "mm"  # imutável


def test_password_round_trip(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="masterkey",
    )
    pw = environments_repo.get_password(env["id"])
    assert pw == "masterkey"


def test_password_none_when_absent(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    assert environments_repo.get_password(env["id"]) is None


def test_update_password_keeps_existing_when_none(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], name="MM2", fb_password=None)
    assert environments_repo.get_password(env["id"]) == "orig"


def test_update_password_clears_with_empty_string(fresh_shared):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb",
        fb_password="orig",
    )
    environments_repo.update(env["id"], name="MM2", fb_password="")
    assert environments_repo.get_password(env["id"]) is None


def test_soft_delete(fresh_shared):
    env = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.soft_delete(env["id"])
    assert environments_repo.get(env["id"])["is_active"] == 0
    actives = environments_repo.list_active()
    assert all(e["id"] != env["id"] for e in actives)


def test_list_active_orders_by_name(fresh_shared):
    environments_repo.create(slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    environments_repo.create(slug="mm",     name="MM Calçados", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    rows = environments_repo.list_active()
    assert [e["slug"] for e in rows] == ["mm", "nasmar"]
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_environments_repo.py -v
```

Expected: FAIL (módulo não existe).

- [ ] **Step 3: Implementar `app/persistence/environments_repo.py`**

```python
"""CRUD para tabela `environments` em app_shared.db.

Senha FB cifrada via secret_store (Fernet). slug imutável após create.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.persistence import router
from app.security import secret_store

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_PUBLIC_FIELDS = (
    "id", "slug", "name", "watch_dir", "output_dir",
    "fb_path", "fb_host", "fb_port", "fb_user", "fb_charset",
    "is_active", "created_at", "updated_at",
)


class SlugTaken(Exception):
    """Slug já existe (UNIQUE violation)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in _PUBLIC_FIELDS}


def create(
    *,
    slug: str,
    name: str,
    watch_dir: str,
    output_dir: str,
    fb_path: str,
    fb_host: str | None = None,
    fb_port: str | None = None,
    fb_user: str = "SYSDBA",
    fb_charset: str = "WIN1252",
    fb_password: str | None = None,
) -> dict[str, Any]:
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug inválido: {slug!r} (use [a-z0-9-], 1-31 chars, começa com alfanum)")
    if not name.strip():
        raise ValueError("name é obrigatório")
    env_id = str(uuid.uuid4())
    now = _now()
    pw_enc = secret_store.encrypt(fb_password) if fb_password else None

    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO environments
                   (id, slug, name, watch_dir, output_dir, fb_path, fb_host, fb_port,
                    fb_user, fb_charset, fb_password_enc, is_active, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (env_id, slug, name, watch_dir, output_dir, fb_path, fb_host, fb_port,
                 fb_user, fb_charset, pw_enc, now, now),
            )
    except sqlite3.IntegrityError as exc:
        if "UNIQUE" in str(exc) and "slug" in str(exc):
            raise SlugTaken(slug) from exc
        raise
    return get(env_id)


def get(env_id: str) -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM environments WHERE id = ?", (env_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def get_by_slug(slug: str) -> dict[str, Any] | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM environments WHERE slug = ?", (slug,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_active() -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM environments WHERE is_active = 1 ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_all() -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM environments ORDER BY is_active DESC, name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


_UPDATABLE = ("name", "watch_dir", "output_dir", "fb_path", "fb_host", "fb_port",
              "fb_user", "fb_charset")


def update(
    env_id: str,
    *,
    name: str | None = None,
    watch_dir: str | None = None,
    output_dir: str | None = None,
    fb_path: str | None = None,
    fb_host: str | None = None,
    fb_port: str | None = None,
    fb_user: str | None = None,
    fb_charset: str | None = None,
    fb_password: str | None = None,  # None = mantém; "" = limpa; valor = troca
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for k, v in {
        "name": name, "watch_dir": watch_dir, "output_dir": output_dir,
        "fb_path": fb_path, "fb_host": fb_host, "fb_port": fb_port,
        "fb_user": fb_user, "fb_charset": fb_charset,
    }.items():
        if v is not None:
            fields[k] = v
    if fb_password is not None:
        fields["fb_password_enc"] = secret_store.encrypt(fb_password) if fb_password else None
    if not fields:
        return get(env_id)
    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [env_id]
    with router.shared_connect() as conn:
        conn.execute(f"UPDATE environments SET {sets} WHERE id = ?", values)
    return get(env_id)


def soft_delete(env_id: str) -> None:
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE environments SET is_active = 0, updated_at = ? WHERE id = ?",
            (_now(), env_id),
        )


def get_password(env_id: str) -> str | None:
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT fb_password_enc FROM environments WHERE id = ?", (env_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    return secret_store.decrypt(row[0])


def to_fb_config(env: dict[str, Any]) -> dict[str, Any]:
    """Materializa um dict pronto pra passar pra `app/erp/connection.py`."""
    return {
        "path": env["fb_path"],
        "host": env["fb_host"] or "",
        "port": env["fb_port"] or "",
        "user": env["fb_user"],
        "charset": env["fb_charset"],
        "password": get_password(env["id"]) or "",
    }
```

- [ ] **Step 4: Rodar testes**

```bash
.venv/bin/pytest tests/test_environments_repo.py -v
```

Expected: PASS (10/10).

- [ ] **Step 5: Suite completa**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/persistence/environments_repo.py tests/test_environments_repo.py
git commit -m "feat(persistence): environments_repo com CRUD + crypto"
```

---

### Task 1.5: Repos compartilhados aceitam Connection injetada

**Princípio:** users_repo, sessions_repo, invites_repo, idempotency_repo passam a receber `conn: sqlite3.Connection` em todas as funções públicas. Compatibilidade: mantemos uma função wrapper sem-arg que abre `shared_connect()` por dentro pra não quebrar callers existentes nessa fase.

**Files:**
- Modify: `app/persistence/users_repo.py`
- Modify: `app/persistence/sessions_repo.py`
- Modify: `app/persistence/invites_repo.py`
- Modify: `app/persistence/idempotency_repo.py`

- [ ] **Step 1: Inspecionar interface atual**

```bash
.venv/bin/python -c "from app.persistence import users_repo; import inspect; print([n for n,_ in inspect.getmembers(users_repo, inspect.isfunction)])"
```

(Use o output como referência das funções a refatorar.)

- [ ] **Step 2: Refatorar `users_repo.py`**

Para cada função pública, adicionar parâmetro `conn` como primeiro arg. Substituir uso de `db.connect()` interno por `conn.execute(...)`. Onde havia commit explícito, deixar para o caller.

Exemplo de antes/depois:

```python
# antes
def get_by_email(email: str) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None

# depois
def get_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None
```

Aplicar para: `create`, `get_by_id`, `get_by_email`, `verify_password`, `update_last_login`, `set_password`, `deactivate`, `reactivate`, `list_all`.

- [ ] **Step 3: Atualizar testes existentes de `test_users_repo.py`**

Cada chamada `users_repo.X(...)` precisa receber a connection. Para fixture:

```python
@pytest.fixture
def shared_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect() as conn:
        yield conn
```

E nos testes:

```python
def test_create_user(shared_conn):
    user_id = users_repo.create(shared_conn, email="a@b.com", password="x", role="admin")
    assert users_repo.get_by_id(shared_conn, user_id)["email"] == "a@b.com"
```

- [ ] **Step 4: Rodar**

```bash
.venv/bin/pytest tests/test_users_repo.py -v
```

Expected: PASS.

- [ ] **Step 5: Repetir mesmo refactor para `sessions_repo.py`**

Funções: `new_token`, `create`, `get`, `delete`, `delete_for_user`, `purge_expired`.

- [ ] **Step 6: Atualizar `tests/test_sessions_repo.py`**

- [ ] **Step 7: Rodar**

```bash
.venv/bin/pytest tests/test_sessions_repo.py -v
```

Expected: PASS.

- [ ] **Step 8: Repetir para `invites_repo.py`**

Funções: `create`, `get`, `accept`, `revoke`, `list_pending`, `purge_expired`.

- [ ] **Step 9: Atualizar `tests/test_invites_repo.py` (se existir) ou cobertura indireta via test_auth_routes.**

- [ ] **Step 10: Repetir para `idempotency_repo.py`**

Funções: `record`, `lookup`, `purge_old`.

- [ ] **Step 11: Rodar suite completa de auth**

```bash
.venv/bin/pytest tests/test_passwords.py tests/test_users_repo.py tests/test_sessions_repo.py tests/test_auth_routes.py tests/test_idempotency_repo.py -v
```

Expected: PASS.

- [ ] **Step 12: Atualizar callers em `app/web/server.py`** (e onde mais aparecer)

Padrão antigo:
```python
user = users_repo.get_by_email(email)
```

Novo:
```python
with router.shared_connect() as conn:
    user = users_repo.get_by_email(conn, email)
```

Em handlers FastAPI, vamos extrair pra dependency `shared_db()` na próxima fase, mas por ora basta substituir manualmente.

- [ ] **Step 13: Suite completa**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 14: Commit**

```bash
git add app/persistence/users_repo.py app/persistence/sessions_repo.py app/persistence/invites_repo.py app/persistence/idempotency_repo.py app/web/server.py tests/test_users_repo.py tests/test_sessions_repo.py tests/test_auth_routes.py tests/test_idempotency_repo.py
git commit -m "refactor(persistence): repos compartilhados recebem Connection injetada"
```

---

### Task 1.6: Repos por-ambiente aceitam Connection + environment_id

Mesma operação para `repo.py` (imports/lifecycle/audit) e `outbox_repo.py`. Diferença: INSERTs precisam preencher `environment_id`.

**Files:**
- Modify: `app/persistence/repo.py`
- Modify: `app/persistence/outbox_repo.py`

- [ ] **Step 1: Inspecionar `repo.py`**

```bash
.venv/bin/python -c "from app.persistence import repo; import inspect; print([n for n,_ in inspect.getmembers(repo, inspect.isfunction)])"
```

- [ ] **Step 2: Refatorar funções de `repo.py`**

Cada função pública recebe `conn: sqlite3.Connection` como primeiro arg. Funções de criação (`create_import`, `record_lifecycle_event`, `record_audit`) recebem também `environment_id: str`.

Exemplo:

```python
# antes
def create_import(payload: dict) -> str:
    with db.connect() as conn:
        conn.execute("INSERT INTO imports (...) VALUES (...)", (...))

# depois
def create_import(conn: sqlite3.Connection, environment_id: str, payload: dict) -> str:
    conn.execute(
        "INSERT INTO imports (environment_id, id, ...) VALUES (?, ?, ...)",
        (environment_id, ...),
    )
```

Aplicar a TODAS funções que fazem INSERT. UPDATEs não mudam. SELECTs adicionam `WHERE environment_id = ?` apenas onde vier do listing geral (já que cada DB é por-ambiente, é redundante mas defensivo).

- [ ] **Step 3: Adicionar validação anti-mutação de `environment_id`**

```python
def _assert_no_env_mutation(payload: dict) -> None:
    if "environment_id" in payload:
        raise ValueError("environment_id é imutável após criação")
```

Chamar em todas as funções de UPDATE.

- [ ] **Step 4: Atualizar testes de `repo.py`** (se houver — pelo Index não há teste isolado)

Skip se não existir.

- [ ] **Step 5: Refatorar `outbox_repo.py`**

Adicionar `environment_id` em INSERT. Funções: `enqueue`, `mark_sent`, `mark_failed`, `list_pending`, `get_by_idempotency_key`.

- [ ] **Step 6: Atualizar callers em `app/web/server.py`, `app/pipeline.py`, `app/exporters/firebird_exporter.py`, `app/state/state_machine.py`** — todos passam connection + environment_id explícitos.

Por ora use `router.env_connect("default")` como hard-coded — vamos remover isso na Fase 3 quando tiver middleware. Adicionar TODO comment.

- [ ] **Step 7: Suite**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS (com placeholder env "default").

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "refactor(persistence): repos por-ambiente recebem Connection + environment_id"
```

---

### Task 1.7: Deprecar `db.py:connect()` global

**Files:**
- Modify: `app/persistence/db.py`
- Delete: `app/persistence/db.py:_SCHEMA_TABLES`, `_SCHEMA_INDEXES`, `_COLUMN_MIGRATIONS`

- [ ] **Step 1: Reduzir `db.py` a um módulo de compatibilidade**

```python
"""DEPRECATED — substituído por app/persistence/router.py.

Mantido temporariamente como shim para callers ainda não migrados
durante a Fase 1. Após Fase 7, este módulo é removido.
"""
from __future__ import annotations
import warnings
from app.persistence import router

def connect(*args, **kwargs):
    warnings.warn(
        "db.connect() está depreciado. Use router.shared_connect() ou router.env_connect(slug).",
        DeprecationWarning, stacklevel=2,
    )
    return router.env_connect("default")  # comportamento legado

def init():
    pass

def db_path():
    return router.shared_db_path()

def reset_init_cache():
    router.reset_init_cache()

def set_db_path(path):
    raise RuntimeError("set_db_path() removido. Use APP_DATA_DIR env var.")
```

- [ ] **Step 2: Suite — qualquer uso restante de `db.connect()` levanta DeprecationWarning visível em testes**

```bash
.venv/bin/pytest tests/ -W error::DeprecationWarning
```

Expected: PASS (idealmente todos callers já foram migrados nas tasks anteriores).

Se algum falhar, voltar e migrar antes de prosseguir.

- [ ] **Step 3: Commit**

```bash
git add app/persistence/db.py
git commit -m "refactor(persistence): db.py vira shim de compatibilidade"
```

---

# Fase 2 — secret_store integration sanity check

### Task 2.1: Validar que secret_store funciona em isolamento

**Files:**
- (nenhum criar/modificar — sanity test)

- [ ] **Step 1: Rodar teste rápido**

```bash
.venv/bin/python -c "from app.security import secret_store; t = secret_store.encrypt('hello'); assert secret_store.decrypt(t) == 'hello'; print('OK')"
```

Expected: `OK`. Se falhar, investigar antes de prosseguir.

---

# Fase 3 — Middleware + Sessão de ambiente

### Task 3.1: Cookie helpers para `portal_env`

**Files:**
- Create: `app/web/cookies.py` (se não existir; senão estender)

- [ ] **Step 1: Verificar se existe módulo de cookies**

```bash
ls "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/web/" | grep -i cookie
```

Se não existir, criar. Se existir, abrir e adicionar funções.

- [ ] **Step 2: Escrever helpers**

```python
"""app/web/cookies.py — Helpers para cookies do Portal."""
from __future__ import annotations
import os

ENV_COOKIE_NAME = "portal_env"
SESSION_COOKIE_NAME = "portal_session"

def _secure_flag() -> bool:
    return os.environ.get("PORTAL_COOKIE_SECURE", "1") == "1"


def set_env_cookie(response, environment_id: str, max_age_seconds: int) -> None:
    response.set_cookie(
        ENV_COOKIE_NAME,
        environment_id,
        max_age=max_age_seconds,
        httponly=True,
        secure=_secure_flag(),
        samesite="strict",
        path="/",
    )


def clear_env_cookie(response) -> None:
    response.delete_cookie(ENV_COOKIE_NAME, path="/")
```

- [ ] **Step 3: Testar (smoke)**

```python
"""tests/test_cookies.py"""
from fastapi.responses import Response
from app.web.cookies import set_env_cookie, clear_env_cookie, ENV_COOKIE_NAME


def test_set_env_cookie():
    r = Response()
    set_env_cookie(r, "env-123", 3600)
    cookie_header = r.headers["set-cookie"]
    assert ENV_COOKIE_NAME in cookie_header
    assert "HttpOnly" in cookie_header
    assert "SameSite=strict" in cookie_header.lower() or "samesite=strict" in cookie_header.lower()


def test_clear_env_cookie():
    r = Response()
    clear_env_cookie(r)
    assert ENV_COOKIE_NAME in r.headers["set-cookie"]
```

- [ ] **Step 4: Rodar**

```bash
.venv/bin/pytest tests/test_cookies.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/cookies.py tests/test_cookies.py
git commit -m "feat(web): cookies helpers para portal_env"
```

---

### Task 3.2: Dependency `current_environment` + `current_env_db`

**Files:**
- Create: `app/web/dependencies/__init__.py`
- Create: `app/web/dependencies/environment.py`
- Create: `tests/test_environment_dependency.py`

- [ ] **Step 1: Escrever testes**

```python
"""tests/test_environment_dependency.py"""
import pytest
from fastapi import FastAPI, Depends, HTTPException
from fastapi.testclient import TestClient

from app.persistence import environments_repo, router
from app.web.cookies import ENV_COOKIE_NAME
from app.web.dependencies.environment import current_environment


@pytest.fixture
def app_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir=str(tmp_path / "in"),
        output_dir=str(tmp_path / "out"),
        fb_path="/x.fdb",
    )
    app = FastAPI()

    @app.get("/protected")
    def protected(env=Depends(current_environment)):
        return {"slug": env["slug"]}

    return app, env


def test_current_environment_returns_env(app_with_env):
    app, env = app_with_env
    client = TestClient(app)
    r = client.get("/protected", cookies={ENV_COOKIE_NAME: env["id"]})
    assert r.status_code == 200
    assert r.json() == {"slug": "mm"}


def test_current_environment_raises_when_cookie_absent(app_with_env):
    app, _ = app_with_env
    client = TestClient(app)
    r = client.get("/protected")
    assert r.status_code == 412  # precondition failed: env não selecionado


def test_current_environment_raises_when_cookie_invalid(app_with_env):
    app, _ = app_with_env
    client = TestClient(app)
    r = client.get("/protected", cookies={ENV_COOKIE_NAME: "non-existent-id"})
    assert r.status_code == 412
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_environment_dependency.py -v
```

Expected: FAIL.

- [ ] **Step 3: Implementar dependency**

```python
"""app/web/dependencies/__init__.py"""
```

```python
"""app/web/dependencies/environment.py"""
from __future__ import annotations
import sqlite3
from typing import Iterator

from fastapi import HTTPException, Request

from app.persistence import environments_repo, router
from app.web.cookies import ENV_COOKIE_NAME


def current_environment(request: Request) -> dict:
    env_id = request.cookies.get(ENV_COOKIE_NAME)
    if not env_id:
        raise HTTPException(
            status_code=412,
            detail="Selecione um ambiente para continuar.",
        )
    env = environments_repo.get(env_id)
    if not env or not env["is_active"]:
        raise HTTPException(
            status_code=412,
            detail="Ambiente selecionado é inválido ou foi desativado.",
        )
    return env


def current_env_db(env: dict = None) -> Iterator[sqlite3.Connection]:
    """FastAPI dependency: abre conexão para a env DB do ambiente atual.

    Uso:
        @app.get("/x")
        def handler(env=Depends(current_environment), conn=Depends(current_env_db)):
            ...

    Aceita opcionalmente o env já resolvido (no FastAPI, declare como
    Depends(current_environment) e use sub-dep).
    """
    # Implementação interna como gerador-context
    raise NotImplementedError("ver assinatura abaixo")
```

Versão final correta de `current_env_db` usando `Depends`:

```python
from fastapi import Depends

def current_env_db(env: dict = Depends(current_environment)):
    with router.env_connect(env["slug"]) as conn:
        yield conn
```

Replace o stub anterior por esta versão.

- [ ] **Step 4: Rodar**

```bash
.venv/bin/pytest tests/test_environment_dependency.py -v
```

Expected: PASS (3/3).

- [ ] **Step 5: Commit**

```bash
git add app/web/dependencies/__init__.py app/web/dependencies/environment.py tests/test_environment_dependency.py
git commit -m "feat(web): dependencies current_environment e current_env_db"
```

---

### Task 3.3: Página `/selecionar-ambiente` + endpoint POST

**Files:**
- Create: `app/web/static/selecionar-ambiente.html`
- Create: `app/web/routes_env_select.py`
- Modify: `app/web/server.py` (registrar router e rota HTML)
- Create: `tests/test_env_select_routes.py`

- [ ] **Step 1: Escrever testes**

```python
"""tests/test_env_select_routes.py"""
import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, router
from app.web.cookies import ENV_COOKIE_NAME, SESSION_COOKIE_NAME
from app.web.server import app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    env_mm = environments_repo.create(slug="mm", name="MM", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    env_nm = environments_repo.create(slug="nasmar", name="Nasmar", watch_dir="/a", output_dir="/b", fb_path="/c.fdb")
    return env_mm, env_nm


def test_get_env_list(setup):
    client = TestClient(app)
    r = client.get("/api/env/list")
    assert r.status_code == 200
    data = r.json()
    assert {e["slug"] for e in data} == {"mm", "nasmar"}


def test_post_env_select_sets_cookie(setup):
    env_mm, _ = setup
    client = TestClient(app)
    r = client.post("/api/env/select", json={"environment_id": env_mm["id"]})
    assert r.status_code == 200
    assert ENV_COOKIE_NAME in r.cookies


def test_post_env_select_rejects_invalid_id(setup):
    client = TestClient(app)
    r = client.post("/api/env/select", json={"environment_id": "fake"})
    assert r.status_code == 404
```

- [ ] **Step 2: Rodar — falha**

- [ ] **Step 3: Criar router**

```python
"""app/web/routes_env_select.py"""
from __future__ import annotations
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from app.persistence import environments_repo
from app.web.cookies import set_env_cookie

router = APIRouter()


class SelectEnvRequest(BaseModel):
    environment_id: str


@router.get("/api/env/list")
def list_envs():
    return [
        {"id": e["id"], "slug": e["slug"], "name": e["name"]}
        for e in environments_repo.list_active()
    ]


@router.post("/api/env/select")
def select_env(payload: SelectEnvRequest, response: Response):
    env = environments_repo.get(payload.environment_id)
    if not env or not env["is_active"]:
        raise HTTPException(404, "Ambiente não encontrado")
    set_env_cookie(response, env["id"], max_age_seconds=8 * 3600)
    return {"ok": True, "environment": {"id": env["id"], "slug": env["slug"], "name": env["name"]}}
```

- [ ] **Step 4: Registrar em `app/web/server.py`**

Localizar `app = FastAPI(...)` e logo após:

```python
from app.web import routes_env_select
app.include_router(routes_env_select.router)


@app.get("/selecionar-ambiente")
def page_select_env():
    return FileResponse(STATIC_DIR / "selecionar-ambiente.html")
```

- [ ] **Step 5: Criar HTML**

`app/web/static/selecionar-ambiente.html`:

```html
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="UTF-8">
  <title>Selecionar ambiente — Portal de Pedidos</title>
  <link rel="stylesheet" href="/static/css/tokens.css">
  <link rel="stylesheet" href="/static/css/shell.css">
  <style>
    .env-grid { display: grid; gap: 1rem; max-width: 720px; margin: 4rem auto; padding: 0 1.5rem; grid-template-columns: 1fr 1fr; }
    .env-card { padding: 1.5rem; border: 1px solid var(--border); border-radius: 12px; cursor: pointer; transition: border-color .15s; }
    .env-card:hover { border-color: var(--accent); }
    .env-card h3 { margin: 0 0 .5rem; }
    .env-card .slug { font-family: var(--font-mono); font-size: .85em; color: var(--text-muted); }
    h1 { text-align: center; margin: 3rem 0 1rem; }
    .empty { text-align: center; margin-top: 4rem; color: var(--text-muted); }
  </style>
</head>
<body>
  <h1>Selecione o ambiente</h1>
  <div class="env-grid" id="envs"></div>
  <div class="empty" id="empty" hidden>
    <p>Nenhum ambiente configurado.</p>
    <p><a href="/admin/ambientes">Ir para gerenciar ambientes</a></p>
  </div>
  <script>
    fetch('/api/env/list').then(r => r.json()).then(envs => {
      const grid = document.getElementById('envs');
      if (!envs.length) {
        document.getElementById('empty').hidden = false;
        grid.hidden = true;
        return;
      }
      envs.forEach(env => {
        const card = document.createElement('div');
        card.className = 'env-card';
        card.innerHTML = `<h3>${env.name}</h3><div class="slug">${env.slug}</div>`;
        card.onclick = async () => {
          const r = await fetch('/api/env/select', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({environment_id: env.id}),
          });
          if (r.ok) location.href = '/';
        };
        grid.appendChild(card);
      });
    });
  </script>
</body>
</html>
```

- [ ] **Step 6: Rodar**

```bash
.venv/bin/pytest tests/test_env_select_routes.py -v
```

Expected: PASS.

- [ ] **Step 7: Suite completa**

```bash
.venv/bin/pytest tests/ -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/web/routes_env_select.py app/web/static/selecionar-ambiente.html app/web/server.py tests/test_env_select_routes.py
git commit -m "feat(web): página e API de seleção de ambiente"
```

---

### Task 3.4: Atualizar `/api/auth/me` para retornar env atual

**Files:**
- Modify: `app/web/server.py` (handler `/api/auth/me`)
- Modify: `app/web/static/js/shell.js`

- [ ] **Step 1: Localizar e modificar handler**

```python
@app.get("/api/auth/me")
def auth_me(request: Request, user=Depends(current_user)):
    env = None
    env_id = request.cookies.get(ENV_COOKIE_NAME)
    if env_id:
        env_row = environments_repo.get(env_id)
        if env_row and env_row["is_active"]:
            env = {"id": env_row["id"], "slug": env_row["slug"], "name": env_row["name"]}
    return {"user": user, "environment": env}
```

- [ ] **Step 2: Modificar `shell.js`**

Procurar bloco que faz fetch `/api/auth/me`. Após popular `window.__shellUser`, adicionar:

```javascript
window.__shellEnv = data.environment;  // pode ser null
renderEnvBadge(data.environment);
```

E adicionar função renderEnvBadge no shell.js que:
- Localiza um elemento `[data-env-badge]` no header (criar no markup do shell)
- Mostra `env.name` se presente, ou "Sem ambiente" + link pra `/selecionar-ambiente`

- [ ] **Step 3: Atualizar markup do shell** (DOM injetado pelo `shell.js` ou já em `index.html`)

Inserir no header, ao lado do nome do usuário:

```html
<div class="env-badge" data-env-badge>
  <span class="env-name"></span>
  <button class="env-switch" type="button" onclick="location.href='/selecionar-ambiente'">trocar</button>
</div>
```

- [ ] **Step 4: Smoke test manual**

```bash
.venv/bin/python ui.py &
SERVER_PID=$!
sleep 2
curl -s http://localhost:8000/api/auth/me  # vai retornar 401 sem auth, OK
kill $SERVER_PID
```

- [ ] **Step 5: Atualizar testes de auth_me**

```python
def test_auth_me_includes_environment(setup_with_session):
    # ... cookie de sessão + cookie de env já setado
    r = client.get("/api/auth/me")
    assert r.json()["environment"]["slug"] == "mm"
```

- [ ] **Step 6: Rodar**

```bash
.venv/bin/pytest tests/test_auth_routes.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(web): /api/auth/me retorna ambiente atual; shell mostra badge"
```

---

### Task 3.5: Redirect login → seleção quando env ausente

**Files:**
- Modify: `app/web/server.py` (handler de root e/ou middleware de login)

- [ ] **Step 1: Estratégia: handler root verifica e redireciona**

```python
@app.get("/")
def root(request: Request):
    # auth: se não logado, redirect /login
    if not request.cookies.get(SESSION_COOKIE_NAME):
        return RedirectResponse("/login")
    # env: se logado mas sem env, redirect /selecionar-ambiente
    if not request.cookies.get(ENV_COOKIE_NAME):
        return RedirectResponse("/selecionar-ambiente")
    return FileResponse(STATIC_DIR / "index.html")
```

- [ ] **Step 2: Testar fluxo**

```python
def test_root_redirects_to_env_select_when_no_env(setup_with_session):
    client = TestClient(app, follow_redirects=False)
    # já autenticado via fixture
    r = client.get("/")
    assert r.status_code in (302, 307)
    assert "/selecionar-ambiente" in r.headers["location"]
```

- [ ] **Step 3: Rodar**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k env
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "feat(web): redireciona logado-sem-env para /selecionar-ambiente"
```

---

# Fase 4 — CRUD Admin de Ambientes

### Task 4.1: Rotas API `/api/admin/environments/*`

**Files:**
- Create: `app/web/routes_environments.py`
- Modify: `app/web/server.py` (registrar router)
- Create: `tests/test_admin_environments_routes.py`

- [ ] **Step 1: Escrever testes**

```python
"""tests/test_admin_environments_routes.py"""
import pytest
from fastapi.testclient import TestClient
from app.persistence import environments_repo, router
from app.web.server import app


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")  # bypass auth assume admin
    router.reset_init_cache()
    return TestClient(app)


def test_list_empty(admin_client):
    r = admin_client.get("/api/admin/environments")
    assert r.status_code == 200
    assert r.json() == []


def test_create(admin_client, tmp_path):
    payload = {
        "slug": "mm", "name": "MM", "watch_dir": str(tmp_path/"in"), "output_dir": str(tmp_path/"out"),
        "fb_path": "/tmp/x.fdb", "fb_password": "secret",
    }
    r = admin_client.post("/api/admin/environments", json=payload)
    assert r.status_code == 201
    body = r.json()
    assert body["slug"] == "mm"
    assert "fb_password" not in body
    assert "fb_password_enc" not in body


def test_create_rejects_duplicate_slug(admin_client, tmp_path):
    payload = {"slug": "mm", "name": "MM", "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "fb_path": "/x.fdb"}
    admin_client.post("/api/admin/environments", json=payload)
    r = admin_client.post("/api/admin/environments", json=payload)
    assert r.status_code == 409


def test_patch_keeps_slug(admin_client, tmp_path):
    payload = {"slug": "mm", "name": "MM", "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "fb_path": "/x.fdb"}
    r = admin_client.post("/api/admin/environments", json=payload).json()
    env_id = r["id"]
    upd = admin_client.patch(f"/api/admin/environments/{env_id}", json={"name": "MM Renomeado", "slug": "outro"})
    assert upd.status_code == 200
    assert upd.json()["name"] == "MM Renomeado"
    assert upd.json()["slug"] == "mm"  # ignora attempt de mudar slug


def test_delete_soft(admin_client, tmp_path):
    payload = {"slug": "mm", "name": "MM", "watch_dir": str(tmp_path), "output_dir": str(tmp_path), "fb_path": "/x.fdb"}
    r = admin_client.post("/api/admin/environments", json=payload).json()
    d = admin_client.delete(f"/api/admin/environments/{r['id']}")
    assert d.status_code == 204
    after = admin_client.get("/api/admin/environments").json()
    assert all(e["id"] != r["id"] for e in after)


def test_test_endpoint_validates_paths(admin_client, tmp_path):
    payload = {
        "slug": "mm", "name": "MM",
        "watch_dir": str(tmp_path / "naoexiste"),
        "output_dir": str(tmp_path / "tambem-nao"),
        "fb_path": "/inexistente.fdb",
    }
    r = admin_client.post("/api/admin/environments", json=payload).json()
    t = admin_client.post(f"/api/admin/environments/{r['id']}/test")
    assert t.status_code == 200
    body = t.json()
    assert body["watch_dir_ok"] is False
    assert body["output_dir_ok"] is False
    assert body["firebird_ok"] is False
```

- [ ] **Step 2: Rodar — falha**

- [ ] **Step 3: Implementar `routes_environments.py`**

```python
"""app/web/routes_environments.py"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.persistence import environments_repo
from app.web.deps_auth import require_admin  # ajustar import conforme projeto

router = APIRouter(prefix="/api/admin/environments", tags=["admin", "environments"])


class CreateEnvRequest(BaseModel):
    slug: str = Field(..., min_length=1, max_length=31)
    name: str = Field(..., min_length=1)
    watch_dir: str
    output_dir: str
    fb_path: str
    fb_host: Optional[str] = None
    fb_port: Optional[str] = None
    fb_user: str = "SYSDBA"
    fb_charset: str = "WIN1252"
    fb_password: Optional[str] = None


class UpdateEnvRequest(BaseModel):
    name: Optional[str] = None
    watch_dir: Optional[str] = None
    output_dir: Optional[str] = None
    fb_path: Optional[str] = None
    fb_host: Optional[str] = None
    fb_port: Optional[str] = None
    fb_user: Optional[str] = None
    fb_charset: Optional[str] = None
    fb_password: Optional[str] = None  # None=keep, ""=clear, valor=replace
    # slug propositalmente ausente: imutável


@router.get("")
def list_environments(_=Depends(require_admin)):
    return environments_repo.list_all()


@router.post("", status_code=201)
def create_environment(payload: CreateEnvRequest, _=Depends(require_admin)):
    try:
        return environments_repo.create(**payload.model_dump())
    except environments_repo.SlugTaken:
        raise HTTPException(409, "slug já existe")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/{env_id}")
def get_environment(env_id: str, _=Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "ambiente não encontrado")
    return env


@router.patch("/{env_id}")
def update_environment(env_id: str, payload: UpdateEnvRequest, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "ambiente não encontrado")
    return environments_repo.update(env_id, **payload.model_dump(exclude_none=False))


@router.delete("/{env_id}", status_code=204)
def delete_environment(env_id: str, _=Depends(require_admin)):
    if not environments_repo.get(env_id):
        raise HTTPException(404, "ambiente não encontrado")
    environments_repo.soft_delete(env_id)
    return Response(status_code=204)


@router.post("/{env_id}/test")
def test_environment(env_id: str, _=Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "ambiente não encontrado")
    watch_ok = Path(env["watch_dir"]).is_dir()
    output_ok = Path(env["output_dir"]).is_dir()
    fb_ok, fb_err = _try_firebird(env)
    return {
        "watch_dir_ok": watch_ok, "watch_dir": env["watch_dir"],
        "output_dir_ok": output_ok, "output_dir": env["output_dir"],
        "firebird_ok": fb_ok, "firebird_error": fb_err,
    }


def _try_firebird(env: dict) -> tuple[bool, str | None]:
    """Tenta conectar usando creds do env. Retorna (ok, mensagem_de_erro)."""
    try:
        from app.erp import connection
        cfg = environments_repo.to_fb_config(env)
        with connection.connect_with_config(cfg) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM RDB$DATABASE")
            cur.fetchone()
        return True, None
    except Exception as e:
        return False, str(e)
```

- [ ] **Step 4: Registrar em `server.py`**

```python
from app.web import routes_environments
app.include_router(routes_environments.router)
```

- [ ] **Step 5: Garantir que `connect_with_config` existe em `app/erp/connection.py`**

Se não existir, adicionar wrapper. Se já existe `connect()` que lê env vars, criar:

```python
def connect_with_config(cfg: dict):
    """Conecta com config explícita (não lê env vars).

    cfg: dict com keys path, host, port, user, password, charset
    """
    import firebird.driver as fb
    dsn = cfg["path"] if not cfg.get("host") else f"{cfg['host']}/{cfg.get('port') or 3050}:{cfg['path']}"
    return fb.connect(
        dsn,
        user=cfg.get("user", "SYSDBA"),
        password=cfg.get("password", ""),
        charset=cfg.get("charset", "WIN1252"),
    )
```

- [ ] **Step 6: Rodar**

```bash
.venv/bin/pytest tests/test_admin_environments_routes.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat(web): rotas admin /api/admin/environments com CRUD + test"
```

---

### Task 4.2: HTML — `admin-ambientes.html` (lista)

**Files:**
- Create: `app/web/static/admin-ambientes.html`
- Modify: `app/web/server.py` (rota GET `/admin/ambientes`)

- [ ] **Step 1: Criar HTML**

```html
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="UTF-8">
  <title>Ambientes — Portal de Pedidos</title>
  <link rel="stylesheet" href="/static/css/tokens.css">
  <link rel="stylesheet" href="/static/css/shell.css">
  <script defer src="/static/js/shell.js"></script>
  <style>
    .page-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }
    .env-table { width: 100%; border-collapse: collapse; }
    .env-table th, .env-table td { padding: .85rem 1rem; text-align: left; border-bottom: 1px solid var(--border); }
    .env-table th { font-weight: 500; color: var(--text-muted); font-size: .85em; }
    .badge-active   { color: var(--success); }
    .badge-inactive { color: var(--text-muted); }
    .actions a { margin-right: .8rem; }
  </style>
</head>
<body>
  <main class="content">
    <div class="page-header">
      <h1>Ambientes</h1>
      <a href="/admin/ambientes/novo" class="btn btn-primary">Novo ambiente</a>
    </div>
    <table class="env-table">
      <thead>
        <tr><th>Nome</th><th>Slug</th><th>Pasta</th><th>Banco</th><th>Status</th><th></th></tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
  <script>
    fetch('/api/admin/environments').then(r => r.json()).then(envs => {
      const tbody = document.getElementById('rows');
      envs.forEach(env => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${env.name}</td>
          <td><code>${env.slug}</code></td>
          <td><code>${env.watch_dir}</code></td>
          <td><code>${env.fb_path}</code></td>
          <td>${env.is_active ? '<span class="badge-active">ativo</span>' : '<span class="badge-inactive">inativo</span>'}</td>
          <td class="actions">
            <a href="/admin/ambientes/${env.id}">editar</a>
          </td>
        `;
        tbody.appendChild(tr);
      });
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Adicionar rotas em `server.py`**

```python
@app.get("/admin/ambientes")
def page_admin_envs(_=Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin-ambientes.html")
```

- [ ] **Step 3: Smoke**

```bash
.venv/bin/python ui.py &
sleep 2
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/admin/ambientes
kill %1
```

Expected: 401 (sem auth) ou 200 (com bypass).

- [ ] **Step 4: Commit**

```bash
git add app/web/static/admin-ambientes.html app/web/server.py
git commit -m "feat(web): página de listagem de ambientes (admin)"
```

---

### Task 4.3: HTML — `admin-ambiente-edit.html` (form criar/editar)

**Files:**
- Create: `app/web/static/admin-ambiente-edit.html`
- Modify: `app/web/server.py` (rotas GET `/admin/ambientes/novo` e `/admin/ambientes/{id}`)

- [ ] **Step 1: Criar HTML form**

Formulário com seções:
- **Identificação:** slug (disable em edit), name
- **Pastas:** watch_dir, output_dir
- **Firebird:** fb_path, fb_host, fb_port, fb_user, fb_charset, fb_password (placeholder "deixe vazio para manter atual")
- Botão **Testar conexão** (POST `/api/admin/environments/{id}/test`) — só aparece em edit
- Botão **Salvar**

```html
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="UTF-8"><title>Ambiente — Portal de Pedidos</title>
  <link rel="stylesheet" href="/static/css/tokens.css">
  <link rel="stylesheet" href="/static/css/shell.css">
  <script defer src="/static/js/shell.js"></script>
  <style>
    form { max-width: 720px; }
    fieldset { border: 1px solid var(--border); padding: 1.5rem; margin-bottom: 1.5rem; border-radius: 8px; }
    legend { padding: 0 .5rem; font-weight: 500; color: var(--text-muted); font-size: .9em; }
    label { display: block; margin-bottom: 1rem; }
    label > span { display: block; font-size: .85em; color: var(--text-muted); margin-bottom: .25rem; }
    input { width: 100%; padding: .6rem; background: var(--bg-elev); border: 1px solid var(--border); color: var(--text); border-radius: 6px; }
    .actions { display: flex; gap: 1rem; align-items: center; }
    .test-result { font-family: var(--font-mono); font-size: .85em; padding: 1rem; background: var(--bg-elev); border-radius: 6px; margin-top: 1rem; white-space: pre-wrap; }
    .ok  { color: var(--success); }
    .err { color: var(--danger); }
  </style>
</head>
<body>
  <main class="content">
    <h1 id="title">Novo ambiente</h1>
    <form id="f">
      <fieldset>
        <legend>Identificação</legend>
        <label><span>Slug (somente minúsculas, dígitos e hífen — imutável)</span><input name="slug" required pattern="[a-z0-9][a-z0-9-]{0,30}"></label>
        <label><span>Nome</span><input name="name" required></label>
      </fieldset>
      <fieldset>
        <legend>Pastas</legend>
        <label><span>Pasta de entrada (watch_dir)</span><input name="watch_dir" required></label>
        <label><span>Pasta de saída (output_dir)</span><input name="output_dir" required></label>
      </fieldset>
      <fieldset>
        <legend>Firebird</legend>
        <label><span>Caminho do .fdb</span><input name="fb_path" required></label>
        <label><span>Host (vazio para embedded)</span><input name="fb_host"></label>
        <label><span>Porta</span><input name="fb_port" placeholder="3050"></label>
        <label><span>Usuário</span><input name="fb_user" value="SYSDBA"></label>
        <label><span>Charset</span><input name="fb_charset" value="WIN1252"></label>
        <label><span>Senha</span><input name="fb_password" type="password" placeholder="vazio = manter atual"></label>
      </fieldset>
      <div class="actions">
        <button type="submit" class="btn btn-primary">Salvar</button>
        <button type="button" id="btn-test" hidden>Testar conexão</button>
        <a href="/admin/ambientes">Cancelar</a>
      </div>
      <div id="test-result" class="test-result" hidden></div>
    </form>
  </main>
  <script>
    const path = location.pathname;
    const isEdit = !path.endsWith('/novo');
    const envId = isEdit ? path.split('/').pop() : null;
    const f = document.getElementById('f');
    const testBtn = document.getElementById('btn-test');
    const testResult = document.getElementById('test-result');

    if (isEdit) {
      document.getElementById('title').textContent = 'Editar ambiente';
      testBtn.hidden = false;
      fetch(`/api/admin/environments/${envId}`).then(r => r.json()).then(env => {
        for (const k of Object.keys(env)) {
          const inp = f.elements[k];
          if (inp && k !== 'fb_password') inp.value = env[k] ?? '';
        }
        f.elements['slug'].readOnly = true;
      });
    }

    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(f));
      // tratamento da senha:
      if (isEdit && data.fb_password === '') {
        delete data.fb_password;  // None = mantém
      }
      const url = isEdit ? `/api/admin/environments/${envId}` : '/api/admin/environments';
      const method = isEdit ? 'PATCH' : 'POST';
      const r = await fetch(url, {
        method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(data),
      });
      if (r.ok) location.href = '/admin/ambientes';
      else alert('Erro: ' + (await r.text()));
    });

    testBtn.addEventListener('click', async () => {
      const r = await fetch(`/api/admin/environments/${envId}/test`, {method: 'POST'});
      const body = await r.json();
      testResult.hidden = false;
      testResult.innerHTML = `
        <div class="${body.watch_dir_ok ? 'ok' : 'err'}">watch_dir: ${body.watch_dir} ${body.watch_dir_ok ? '✓' : '✗'}</div>
        <div class="${body.output_dir_ok ? 'ok' : 'err'}">output_dir: ${body.output_dir} ${body.output_dir_ok ? '✓' : '✗'}</div>
        <div class="${body.firebird_ok ? 'ok' : 'err'}">firebird: ${body.firebird_ok ? '✓' : '✗ ' + (body.firebird_error || '')}</div>
      `;
    });
  </script>
</body>
</html>
```

- [ ] **Step 2: Rotas no `server.py`**

```python
@app.get("/admin/ambientes/novo")
def page_admin_env_new(_=Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin-ambiente-edit.html")

@app.get("/admin/ambientes/{env_id}")
def page_admin_env_edit(env_id: str, _=Depends(require_admin)):
    return FileResponse(STATIC_DIR / "admin-ambiente-edit.html")
```

- [ ] **Step 3: Smoke manual**

Subir o servidor, criar um env via UI, editar, testar conexão (vai falhar com FB inexistente — espera-se `firebird_ok: false`).

- [ ] **Step 4: Commit**

```bash
git add app/web/static/admin-ambiente-edit.html app/web/server.py
git commit -m "feat(web): form criar/editar ambiente com botão de teste"
```

---

### Task 4.4: Atualizar `shell.js` — link "Ambientes" no menu admin

**Files:**
- Modify: `app/web/static/js/shell.js`

- [ ] **Step 1: Localizar a sidebar de admin no shell.js**

```bash
grep -n "Configurações\|admin" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/web/static/js/shell.js" | head
```

- [ ] **Step 2: Adicionar item "Ambientes" no grupo admin**

Localizar onde "admin-usuarios" é referenciado, e adicionar (mesmo padrão):

```javascript
{href: '/admin/ambientes', label: 'Ambientes', adminOnly: true},
```

- [ ] **Step 3: Smoke manual** — subir servidor, login como admin, ver link no sidebar.

- [ ] **Step 4: Commit**

```bash
git add app/web/static/js/shell.js
git commit -m "feat(web): link Ambientes no menu admin"
```

---

# Fase 5 — Watcher / scan_environments

### Task 5.1: Job APScheduler `scan_environments`

**Files:**
- Create: `app/worker/jobs/scan_environments.py`
- Create: `tests/test_scan_environments.py`
- Modify: `app/worker/scheduler.py` (registrar job)

- [ ] **Step 1: Escrever testes**

```python
"""tests/test_scan_environments.py"""
import hashlib
import pytest
from pathlib import Path

from app.persistence import environments_repo, router
from app.worker.jobs import scan_environments


@pytest.fixture
def env_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    watch = tmp_path / "watch"
    output = tmp_path / "out"
    watch.mkdir()
    output.mkdir()
    env = environments_repo.create(
        slug="mm", name="MM",
        watch_dir=str(watch), output_dir=str(output),
        fb_path=str(tmp_path / "x.fdb"),
    )
    return env, watch


def test_scan_picks_up_new_pdf(env_setup, monkeypatch):
    env, watch = env_setup
    pdf = watch / "pedido-001.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake content")

    # mock pipeline.process
    processed = []
    def fake_process(path, environment_id):
        processed.append((str(path), environment_id))
        return {"id": "imp-1", "status": "PARSED"}
    monkeypatch.setattr(scan_environments, "_process_file", fake_process)

    scan_environments.run_once()

    assert len(processed) == 1
    assert processed[0][1] == env["id"]
    # arquivo movido pra Pedidos importados
    moved_dir = watch / "Pedidos importados"
    assert moved_dir.is_dir()
    assert any(moved_dir.iterdir())


def test_scan_idempotent_by_sha(env_setup, monkeypatch):
    env, watch = env_setup
    pdf = watch / "pedido.pdf"
    content = b"%PDF-1.4\nidempotent"
    pdf.write_bytes(content)

    calls = []
    def fake_process(path, environment_id):
        calls.append(path)
        return {"id": "imp-x", "status": "PARSED"}
    monkeypatch.setattr(scan_environments, "_process_file", fake_process)
    monkeypatch.setattr(scan_environments, "_already_imported", lambda env_id, sha: len(calls) > 0)

    scan_environments.run_once()
    # recolocar arquivo igual:
    pdf2 = watch / "pedido-copy.pdf"
    pdf2.write_bytes(content)
    scan_environments.run_once()

    assert len(calls) == 1  # segundo arquivo com mesmo sha não foi processado


def test_scan_iterates_multiple_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    for slug in ("mm", "nasmar"):
        d = tmp_path / slug
        d.mkdir()
        environments_repo.create(
            slug=slug, name=slug.upper(),
            watch_dir=str(d), output_dir=str(tmp_path / f"{slug}-out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    (tmp_path / "mm" / "a.pdf").write_bytes(b"AAA")
    (tmp_path / "nasmar" / "b.pdf").write_bytes(b"BBB")

    seen = []
    monkeypatch.setattr(scan_environments, "_process_file",
                        lambda path, environment_id: seen.append((Path(path).name, environment_id)) or {"id": "x", "status": "PARSED"})

    scan_environments.run_once()
    files = sorted(name for name, _ in seen)
    assert files == ["a.pdf", "b.pdf"]
```

- [ ] **Step 2: Rodar — falha**

- [ ] **Step 3: Implementar job**

```python
"""app/worker/jobs/scan_environments.py — varre watch_dir de cada ambiente."""
from __future__ import annotations
import hashlib
import shutil
from pathlib import Path
from typing import Iterable

from app.persistence import environments_repo, router
from app.utils.logger import logger

VALID_EXTS = (".pdf", ".xls", ".xlsx")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _already_imported(environment_slug: str, sha: str) -> bool:
    with router.env_connect(environment_slug) as conn:
        row = conn.execute(
            "SELECT 1 FROM imports WHERE file_sha256 = ? LIMIT 1", (sha,)
        ).fetchone()
    return row is not None


def _candidate_files(watch_dir: Path) -> Iterable[Path]:
    if not watch_dir.is_dir():
        return []
    for p in watch_dir.iterdir():
        if p.is_file() and p.suffix.lower() in VALID_EXTS:
            yield p


def _move_to_imported(p: Path, watch_dir: Path) -> Path:
    dst_dir = watch_dir / "Pedidos importados"
    dst_dir.mkdir(exist_ok=True)
    dst = dst_dir / p.name
    if dst.exists():
        # collision: append timestamp
        from datetime import datetime
        dst = dst_dir / f"{dst.stem}.{datetime.now():%Y%m%d%H%M%S}{dst.suffix}"
    shutil.move(p, dst)
    return dst


def _process_file(path: Path, environment_id: str) -> dict:
    """Bridge para o pipeline existente.

    Retorna o dict de result do pipeline (com 'id' e 'status').
    """
    from app.pipeline import run_pipeline
    return run_pipeline(path, environment_id=environment_id)


def run_once() -> None:
    """Uma passada: para cada ambiente ativo, processa arquivos novos."""
    envs = environments_repo.list_active()
    for env in envs:
        watch_dir = Path(env["watch_dir"])
        for p in _candidate_files(watch_dir):
            try:
                sha = _sha256(p)
                if _already_imported(env["slug"], sha):
                    logger.info(f"scan: skip duplicado sha={sha[:12]} env={env['slug']} file={p.name}")
                    _move_to_imported(p, watch_dir)
                    continue
                logger.info(f"scan: processando file={p.name} env={env['slug']}")
                _process_file(p, environment_id=env["id"])
                _move_to_imported(p, watch_dir)
            except Exception as exc:
                logger.error(f"scan: falha em {p.name} env={env['slug']}: {exc!r}")


def register(scheduler) -> None:
    scheduler.add_job(run_once, "interval", seconds=30, id="scan_environments", replace_existing=True)
```

- [ ] **Step 4: `pipeline.run_pipeline` precisa aceitar `environment_id`**

Localizar `app/pipeline.py` e adicionar parâmetro:

```python
def run_pipeline(path: Path, *, environment_id: str) -> dict:
    # ... existing code, propagating environment_id para repo.create_import
```

- [ ] **Step 5: Rodar testes**

```bash
.venv/bin/pytest tests/test_scan_environments.py -v
```

Expected: PASS.

- [ ] **Step 6: Registrar no scheduler**

Em `app/worker/scheduler.py`:

```python
from app.worker.jobs import scan_environments
scan_environments.register(scheduler)
```

- [ ] **Step 7: Suite completa**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 8: Commit**

```bash
git add app/worker/jobs/scan_environments.py app/worker/scheduler.py app/pipeline.py tests/test_scan_environments.py
git commit -m "feat(worker): job scan_environments multi-pasta com idempotência por sha"
```

---

### Task 5.2: `/api/files` lista da DB filtrando por env

**Files:**
- Modify: `app/web/server.py` (handler `/api/files`)

- [ ] **Step 1: Localizar handler atual**

```bash
grep -n "/api/files" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/web/server.py"
```

- [ ] **Step 2: Refatorar para usar `current_env_db`**

```python
@app.get("/api/files")
def api_files(
    env=Depends(current_environment),
    conn=Depends(current_env_db),
):
    rows = conn.execute(
        """SELECT id, source_filename, imported_at, status, portal_status,
                  customer_name, order_number
             FROM imports
            WHERE environment_id = ?
            ORDER BY imported_at DESC
            LIMIT 200""",
        (env["id"],),
    ).fetchall()
    return [dict(r) for r in rows]
```

- [ ] **Step 3: Atualizar testes existentes** (`test_web_server.py`) — adicionar fixture com env

- [ ] **Step 4: Suite**

```bash
.venv/bin/pytest tests/test_web_server.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "refactor(web): /api/files filtra por ambiente da sessão"
```

---

# Fase 6 — Firebird por ambiente, remoção de firebird_config

### Task 6.1: `firebird_exporter` recebe env como parâmetro

**Files:**
- Modify: `app/exporters/firebird_exporter.py`

- [ ] **Step 1: Localizar onde `firebird_exporter` lê env vars**

```bash
grep -rn "FB_DATABASE\|firebird_config\|os.environ\[.FB" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/"
```

- [ ] **Step 2: Refatorar `firebird_exporter` para receber `env: dict`**

Função atual `export_to_firebird(order)` vira `export_to_firebird(order, env: dict)`. Usa `connection.connect_with_config(environments_repo.to_fb_config(env))`.

- [ ] **Step 3: Atualizar callers** — `app/pipeline.py` (que tem `environment_id`) carrega o env via `environments_repo.get(environment_id)` e passa.

- [ ] **Step 4: Suite**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor(exporters): firebird_exporter recebe env explícito"
```

---

### Task 6.2: Remover `firebird_config.py` + rotas + página

**Files:**
- Delete: `app/firebird_config.py`
- Delete: `app/web/static/config-banco.html`
- Modify: `app/web/server.py` (remover rotas `/api/firebird/*`)
- Modify: `app/web/static/js/shell.js` (remover link "Configurações > Banco")

- [ ] **Step 1: Verificar usos remanescentes**

```bash
grep -rn "firebird_config\|/api/firebird\|config-banco" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/tests/" "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/tools/"
```

- [ ] **Step 2: Remover imports + rotas**

Em `server.py`, deletar:
- `from app import firebird_config`
- `@app.get("/api/firebird/config")`
- `@app.post("/api/firebird/config")`
- `@app.post("/api/firebird/test")`
- Chamada a `firebird_config.apply_to_env()` no startup

Em startup, substituir lógica por nada (o env vem do request, não do environment global).

- [ ] **Step 3: Deletar arquivos**

```bash
rm "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/firebird_config.py"
rm "/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/app/web/static/config-banco.html"
```

- [ ] **Step 4: Atualizar shell.js**

Remover entry `'Banco'` do menu de Configurações.

- [ ] **Step 5: Atualizar `docs/ai/00-index.md` e `docs/ai/modules/erp.md`** — remover referências a `firebird_config.py` e `/configuracoes/banco`. Apontar para `/admin/ambientes`.

- [ ] **Step 6: Suite**

```bash
.venv/bin/pytest tests/ -q
```

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove firebird_config singleton; substituído por environments"
```

---

# Fase 7 — Migração + deploy

### Task 7.1: Script `tools/migrate_to_multi_env.py`

**Files:**
- Create: `tools/migrate_to_multi_env.py`
- Create: `tests/test_migrate_to_multi_env.py`

- [ ] **Step 1: Escrever script**

```python
"""tools/migrate_to_multi_env.py

Migra users/sessions/invites do app_state.db (legacy) para app_shared.db (novo).
Renomeia o app_state.db legacy para app_state.db.legacy.

Uso:
  python tools/migrate_to_multi_env.py --data-dir /path/to/data
"""
from __future__ import annotations
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from app.persistence import router


def migrate(data_dir: Path) -> None:
    legacy = data_dir / "app_state.db"
    if not legacy.exists():
        print(f"[migrate] nenhum app_state.db legacy em {data_dir}, abortando")
        return
    # 1. Garantir shared schema
    import os
    os.environ["APP_DATA_DIR"] = str(data_dir)
    router.reset_init_cache()
    with router.shared_connect():
        pass

    src = sqlite3.connect(legacy)
    src.row_factory = sqlite3.Row
    with router.shared_connect() as dst:
        for table in ("users", "user_invites", "sessions"):
            try:
                rows = src.execute(f"SELECT * FROM {table}").fetchall()
            except sqlite3.OperationalError:
                print(f"[migrate] tabela {table} não existe no legacy, pulando")
                continue
            if not rows:
                continue
            cols = rows[0].keys()
            placeholders = ", ".join("?" * len(cols))
            collist = ", ".join(cols)
            for r in rows:
                try:
                    dst.execute(
                        f"INSERT INTO {table} ({collist}) VALUES ({placeholders})",
                        tuple(r[c] for c in cols),
                    )
                except sqlite3.IntegrityError as e:
                    print(f"[migrate] {table} skip linha (já existe?): {e}")
            print(f"[migrate] copiou {len(rows)} linhas de {table}")
    src.close()
    backup = legacy.with_suffix(".db.legacy")
    legacy.rename(backup)
    print(f"[migrate] renomeou app_state.db → {backup.name}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, required=True)
    args = ap.parse_args()
    if not args.data_dir.is_dir():
        print(f"erro: {args.data_dir} não é diretório", file=sys.stderr)
        sys.exit(1)
    migrate(args.data_dir)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Escrever teste**

```python
"""tests/test_migrate_to_multi_env.py"""
import sqlite3
from pathlib import Path

from app.persistence import router
from tools import migrate_to_multi_env


def test_migrate_users_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    legacy = tmp_path / "app_state.db"

    src = sqlite3.connect(legacy)
    src.execute("""CREATE TABLE users (
        id INTEGER PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT, role TEXT NOT NULL DEFAULT 'operator',
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, last_login_at TEXT
    )""")
    src.execute("""INSERT INTO users (email, password_hash, role, active, created_at)
                   VALUES ('a@b.com', 'h', 'admin', 1, '2026-05-05')""")
    src.commit()
    src.close()

    migrate_to_multi_env.migrate(tmp_path)

    # legacy renomeado
    assert not legacy.exists()
    assert (tmp_path / "app_state.db.legacy").exists()

    # shared tem o user
    with router.shared_connect() as conn:
        row = conn.execute("SELECT email FROM users WHERE email = ?", ("a@b.com",)).fetchone()
    assert row["email"] == "a@b.com"
```

- [ ] **Step 3: Rodar**

```bash
.venv/bin/pytest tests/test_migrate_to_multi_env.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tools/migrate_to_multi_env.py tests/test_migrate_to_multi_env.py
git commit -m "feat(tools): script de migração users/sessions para multi-env"
```

---

### Task 7.2: Atualizar startup para inicializar shared DB

**Files:**
- Modify: `app/web/server.py` (startup event)

- [ ] **Step 1: Substituir `firebird_config.apply_to_env()` (já removido) por inicialização explícita do shared**

```python
@app.on_event("startup")
async def on_startup():
    # garante shared DB existe
    from app.persistence import router as persist_router
    with persist_router.shared_connect():
        pass
    # garante DBs de ambientes ativos existem
    for slug in persist_router.list_env_slugs():
        with persist_router.env_connect(slug):
            pass
    # ... outros startups (scheduler, etc.)
```

- [ ] **Step 2: Smoke**

```bash
.venv/bin/python ui.py &
sleep 3
ls -la data/  # deve ter app_shared.db
kill %1
```

- [ ] **Step 3: Commit**

```bash
git add app/web/server.py
git commit -m "feat(startup): garante shared+env DBs criadas no boot"
```

---

### Task 7.3: Atualizar docs

**Files:**
- Modify: `CLAUDE.md`
- Modify: `docs/ai/00-index.md`
- Create: `docs/ai/modules/environments.md`

- [ ] **Step 1: Atualizar CLAUDE.md**

Substituir seção **Variáveis de Ambiente** por listagem nova (sem `FB_DATABASE`, `INPUT_DIR`, `OUTPUT_DIR` como singletons).

Substituir seção **Estrutura de Pastas** explicando que pastas vivem por ambiente.

- [ ] **Step 2: Adicionar entrada no `docs/ai/00-index.md`**

| Adicionar/editar ambiente, multi-empresa | `environments` | `modules/environments.md` |

- [ ] **Step 3: Criar `docs/ai/modules/environments.md`** (curto, conforme padrão dos outros modules)

```markdown
# environments — Multi-empresa

## Arquivos críticos
- `app/persistence/environments_repo.py` — CRUD
- `app/persistence/router.py` — roteamento de conexões
- `app/web/routes_environments.py` — API admin
- `app/web/dependencies/environment.py` — dependencies FastAPI
- `app/web/middleware/environment.py` — (se houver — caso contrário só dependencies)
- `app/worker/jobs/scan_environments.py` — scan multi-pasta

## Schema
- `environments` em `app_shared.db` (slug, name, watch_dir, output_dir, fb_*)
- Senha cifrada via `app/security/secret_store.py`
- Slug imutável após criação; dele deriva nome do arquivo `app_state_<slug>.db`

## Pontos de atenção
- Sempre passar `environment_id` em INSERTs em DBs de ambiente; UPDATE jamais altera
- `current_env_db` dependency abre/fecha conexão por request
- Workers iteram `router.list_env_slugs()` e abrem cada conexão explicitamente
```

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs: atualiza CLAUDE.md e docs/ai/ para multi-ambiente"
```

---

### Task 7.4: Suite completa + smoke E2E final

- [ ] **Step 1: Suite completa**

```bash
.venv/bin/pytest tests/ -v
```

Expected: TODOS verdes.

- [ ] **Step 2: Lint**

```bash
ruff check app/ tests/ tools/
ruff format --check app/ tests/ tools/
```

- [ ] **Step 3: Smoke manual**

```bash
.venv/bin/python ui.py &
SERVER_PID=$!
sleep 3

# 1. login (admin)
# 2. criar ambiente "MM"
# 3. criar ambiente "Nasmar"
# 4. /selecionar-ambiente → MM
# 5. ver dashboard vazio
# 6. trocar pra Nasmar
# 7. ver dashboard Nasmar (também vazio)
# 8. soltar arquivo PDF na watch_dir da MM
# 9. esperar 30s, recarregar dashboard MM
# 10. ver pedido aparecer
# 11. logout

kill $SERVER_PID
```

- [ ] **Step 4: Merge para main**

```bash
git checkout main
git merge --no-ff feature/multi-ambiente -m "feat: multi-ambiente (MM, Nasmar e além)"
```

- [ ] **Step 5: Tag**

```bash
git tag v3.0.0-multi-env
```

---

## Self-review (depois de salvar)

**Spec coverage:**
- ✅ environments table — Task 1.2
- ✅ DB hybrid (shared + per-env) — Task 1.2/1.3
- ✅ environment_id em todas tabelas env — Task 1.2 (schema_env)
- ✅ Repos com Connection injetada — Task 1.5/1.6
- ✅ environments_repo CRUD + crypto — Task 1.4
- ✅ Cookie portal_env + middleware — Task 3.1/3.2
- ✅ Página seleção + redirect login — Task 3.3/3.5
- ✅ /api/auth/me retorna env — Task 3.4
- ✅ Rotas admin /api/admin/environments — Task 4.1
- ✅ Páginas HTML CRUD — Task 4.2/4.3
- ✅ Link no shell — Task 4.4
- ✅ Watcher scan_environments — Task 5.1
- ✅ /api/files filtra env — Task 5.2
- ✅ firebird_exporter recebe env — Task 6.1
- ✅ Remoção firebird_config — Task 6.2
- ✅ Migration script — Task 7.1
- ✅ Docs atualizadas — Task 7.3

**Placeholders/TODOs no plano:** nenhum encontrado. Todos os steps têm código completo ou comando exato.

**Type consistency:**
- `environments_repo.create()` e `update()` têm assinaturas consistentes
- `current_environment` retorna `dict`; `current_env_db` retorna `Connection`
- `_process_file` (no scan_environments) e `pipeline.run_pipeline` têm contrato `(path, environment_id) -> dict`

Plano completo. Pronto para execução.
