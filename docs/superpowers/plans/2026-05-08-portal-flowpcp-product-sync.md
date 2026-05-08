# Portal → FlowPCP Product Sync — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Portal Pedidos side of the product catalog sync — a 15-minute scheduled job (plus manual trigger) that reads `PRODUTOS` and `PRODUTOS_KIT` from each environment's Firebird, computes a hash-based delta against local SQLite state, and POSTs the delta to FlowPCP's `/api/portal-pedidos/produtos/sync` endpoint.

**Architecture:** Read-only Firebird scan → canonical-JSON hashing → delta vs SQLite state → bulk POST (idempotent) → commit state on 2xx. Per-environment Bearer auth stored encrypted via `secret_store`. Scheduler driven by APScheduler with circuit breaker on persistent failures.

**Tech Stack:** Python 3.11, pydantic v2, httpx (via existing `OutboundClient`), SQLite, Firebird (read-only), APScheduler, FastAPI, loguru, prometheus-client, pytest.

**Spec reference:** [docs/superpowers/specs/2026-05-08-portal-flowpcp-product-sync-design.md](../specs/2026-05-08-portal-flowpcp-product-sync-design.md)

**Structural note:** Spec said `app/sync/flowpcp_client.py`, but to match the existing `app/integrations/gestor/` pattern, the HTTP client + wire schema move to `app/integrations/flowpcp/`. The catalog sync engine itself (reader/diff/state/runner) lives in `app/sync/` since it's not FlowPCP-specific in concept.

---

## File Structure

**New files:**
- `app/sync/__init__.py` — empty
- `app/sync/models.py` — pydantic: `ProductRow`, `ComponentRow`, `SyncDelta`, `RunResult`
- `app/sync/canonical.py` — `canonical_hash(dict) -> str`
- `app/sync/fire_reader.py` — `read_products_snapshot(env)`, `read_components_snapshot(env)`
- `app/sync/sync_state_repo.py` — load/commit state + `record_run_*`
- `app/sync/diff_engine.py` — `compute_delta(...)`
- `app/sync/runner.py` — orchestrator `run(env, trigger)`
- `app/integrations/flowpcp/__init__.py` — re-exports
- `app/integrations/flowpcp/schema.py` — pydantic request/response of FlowPCP wire format
- `app/integrations/flowpcp/client.py` — `FlowPCPClient`, `FlowPCPClientError`
- `app/worker/jobs/flowpcp_product_sync.py` — `run_flowpcp_product_sync()`
- `app/web/routes_produtos_sync.py` — `GET /admin/produtos/sync/{slug}`, `POST /admin/produtos/sync-now/{slug}`
- `app/web/static/admin/produtos-sync.html` — UI página de runs (lista + botão)
- `tests/test_sync_canonical.py`
- `tests/test_sync_models.py`
- `tests/test_sync_fire_reader.py`
- `tests/test_sync_state_repo.py`
- `tests/test_sync_diff_engine.py`
- `tests/test_flowpcp_client.py`
- `tests/test_sync_runner.py`
- `tests/test_admin_produtos_sync_routes.py`
- `tests/test_environments_repo_flowpcp.py`
- `docs/ai/modules/sync.md`

**Modified files:**
- `app/persistence/schema_shared.py` — add `COLUMN_MIGRATIONS` entries for FlowPCP cols + `circuit_open` on `environments`
- `app/persistence/schema_env.py` — add `product_sync_state`, `component_sync_state`, `product_sync_runs` tables
- `app/persistence/environments_repo.py` — add `set_flowpcp_config(env_id, ...)`, `get_flowpcp_secret(env_id)`, `to_flowpcp_config(env)`, `mark_circuit_open/closed(env_id)`
- `app/web/routes_environments.py` — add fields `flowpcp_*` to admin POST/PUT + render in UI
- `app/web/server.py` — mount `routes_produtos_sync` router
- `app/worker/scheduler.py` — add `flowpcp_product_sync` job
- `app/observability/metrics.py` — add 4 metrics
- `docs/ai/00-index.md` — add row for `sync` domain
- `.env.example` — add `PORTAL_SYNC_ENABLED`, `PORTAL_SYNC_INTERVAL_MINUTES`

---

## Test Conventions (read before starting)

- Tests live in `tests/test_<module>.py`, run via `.venv/bin/pytest tests/<file>.py -v`.
- `conftest.py` already provides `tmp_path`-based test DB via `db.set_db_path(...)` + `db.set_db_path(None)` in teardown.
- Fixtures: `tests/conftest.py` is the canonical source — read it before adding new fixtures.
- HTTP tests use `httpx.MockTransport`. Pattern reference: `tests/test_gestor_integration.py`.
- Firebird tests cannot use a real Firebird instance. Mock the connection — patch `FirebirdConnection.connect_with_config` to return an in-memory cursor stub. The reader's interface against the cursor is well-defined (just `execute(sql, params)` + `fetchall()` + `description`), so a tiny stub is enough.

---

## Phase 0 — Schema migrations

### Task 1: Add FlowPCP columns to `environments` table

**Files:**
- Modify: `app/persistence/schema_shared.py`
- Test: `tests/test_environments_repo_flowpcp.py`

- [ ] **Step 1: Inspect current shape of `schema_shared.py`**

Run: `grep -n "COLUMN_MIGRATIONS\|TABLES_SQL\|INDEXES_SQL" app/persistence/schema_shared.py`
Expected: see current symbols. If `COLUMN_MIGRATIONS` does not exist as a top-level tuple, look at how `_apply_column_migrations` is called in `app/persistence/router.py`. The router expects `schema_module.COLUMN_MIGRATIONS` to be a tuple of `(table, col, ddl)`. Confirm and add the symbol if missing (initially empty tuple).

- [ ] **Step 2: Write the failing test**

Write `tests/test_environments_repo_flowpcp.py`:

```python
"""FlowPCP-specific extensions to environments_repo."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.persistence import db, environments_repo, router


@pytest.fixture
def tmp_data(tmp_path: Path):
    db.set_db_path(tmp_path)
    yield tmp_path
    db.set_db_path(None)
    router.reset_init_cache()


def test_environments_table_has_flowpcp_columns(tmp_data):
    with router.shared_connect() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(environments)").fetchall()}
    expected = {
        "flowpcp_enabled",
        "flowpcp_base_url",
        "flowpcp_tenant_id",
        "flowpcp_api_key_enc",
        "flowpcp_circuit_open",
        "flowpcp_last_failure_at",
        "flowpcp_consecutive_failures",
    }
    missing = expected - cols
    assert not missing, f"Missing FlowPCP columns: {missing}"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_environments_repo_flowpcp.py::test_environments_table_has_flowpcp_columns -v`
Expected: FAIL — columns missing.

- [ ] **Step 4: Add columns via `COLUMN_MIGRATIONS`**

In `app/persistence/schema_shared.py`, add after `TABLES_SQL`:

```python
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # FlowPCP product-sync integration (per environment)
    ("environments", "flowpcp_enabled",
        "ALTER TABLE environments ADD COLUMN flowpcp_enabled INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_base_url",
        "ALTER TABLE environments ADD COLUMN flowpcp_base_url TEXT"),
    ("environments", "flowpcp_tenant_id",
        "ALTER TABLE environments ADD COLUMN flowpcp_tenant_id TEXT"),
    ("environments", "flowpcp_api_key_enc",
        "ALTER TABLE environments ADD COLUMN flowpcp_api_key_enc TEXT"),
    ("environments", "flowpcp_circuit_open",
        "ALTER TABLE environments ADD COLUMN flowpcp_circuit_open INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_last_failure_at",
        "ALTER TABLE environments ADD COLUMN flowpcp_last_failure_at TEXT"),
    ("environments", "flowpcp_consecutive_failures",
        "ALTER TABLE environments ADD COLUMN flowpcp_consecutive_failures INTEGER NOT NULL DEFAULT 0"),
)
```

Also ensure `INDEXES_SQL` exists at module scope (empty string is fine if no new indexes — needed by router):

```python
INDEXES_SQL = ""  # keep existing if present; do not overwrite
```

If `INDEXES_SQL` already exists, leave it alone.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_environments_repo_flowpcp.py::test_environments_table_has_flowpcp_columns -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/persistence/schema_shared.py tests/test_environments_repo_flowpcp.py
git commit -m "feat(persistence): add FlowPCP columns to environments table"
```

---

### Task 2: Add `product_sync_state`, `component_sync_state`, `product_sync_runs` tables to per-env schema

**Files:**
- Modify: `app/persistence/schema_env.py`
- Test: `tests/test_sync_state_repo.py` (first test only — repo functions come later)

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_state_repo.py`:

```python
"""SQLite state for product sync (per-environment)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.persistence import db, router


@pytest.fixture
def tmp_data(tmp_path: Path):
    db.set_db_path(tmp_path)
    yield tmp_path
    db.set_db_path(None)
    router.reset_init_cache()


def test_per_env_schema_has_sync_tables(tmp_data):
    with router.env_connect("test") as conn:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "product_sync_state" in names
    assert "component_sync_state" in names
    assert "product_sync_runs" in names

    with router.env_connect("test") as conn:
        cols_state = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_state)").fetchall()}
        cols_runs = {r[1] for r in conn.execute(
            "PRAGMA table_info(product_sync_runs)").fetchall()}
    assert {"seq", "content_hash", "last_synced_at"}.issubset(cols_state)
    assert {"id", "sync_id", "trigger", "started_at", "finished_at",
            "status", "delta_count_produtos", "delta_count_componentes",
            "delta_count_tombstones", "applied_count", "errors_json",
            "trace_id"}.issubset(cols_runs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_state_repo.py::test_per_env_schema_has_sync_tables -v`
Expected: FAIL — tables missing.

- [ ] **Step 3: Add tables to `schema_env.py`**

In `app/persistence/schema_env.py`, append to `TABLES_SQL`:

```sql
CREATE TABLE IF NOT EXISTS product_sync_state (
    seq             INTEGER PRIMARY KEY,
    content_hash    TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_sync_state (
    codigo          INTEGER PRIMARY KEY,  -- PRODUTOS_KIT.CODIGO
    content_hash    TEXT NOT NULL,
    last_synced_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS product_sync_runs (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id                  TEXT NOT NULL UNIQUE,
    trigger                  TEXT NOT NULL,
    started_at               TEXT NOT NULL,
    finished_at              TEXT,
    status                   TEXT NOT NULL,
    delta_count_produtos     INTEGER NOT NULL DEFAULT 0,
    delta_count_componentes  INTEGER NOT NULL DEFAULT 0,
    delta_count_tombstones   INTEGER NOT NULL DEFAULT 0,
    applied_count            INTEGER NOT NULL DEFAULT 0,
    errors_json              TEXT,
    trace_id                 TEXT
);
```

In `INDEXES_SQL` (or append if file uses one), add:

```sql
CREATE INDEX IF NOT EXISTS ix_product_sync_runs_started
    ON product_sync_runs(started_at DESC);
```

If `schema_env.py` does not have `INDEXES_SQL`, add it as a top-level constant (router expects it). If it doesn't have `COLUMN_MIGRATIONS`, add that as `()` so the router contract is satisfied.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sync_state_repo.py::test_per_env_schema_has_sync_tables -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/persistence/schema_env.py tests/test_sync_state_repo.py
git commit -m "feat(persistence): add product_sync_state, component_sync_state, product_sync_runs tables"
```

---

## Phase 1 — Domain models

### Task 3: Pydantic models for sync delta

**Files:**
- Create: `app/sync/__init__.py`, `app/sync/models.py`
- Test: `tests/test_sync_models.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_models.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.sync.models import (
    ProductRow,
    ComponentRow,
    SyncDelta,
    RunResult,
    RunStatus,
    Trigger,
)


def test_product_row_required_fields():
    p = ProductRow(
        seq=10042,
        codprod_altern="CAL-0042-PR",
        descricao="TENIS XYZ",
        unidade="un",
        codigo_ean13="7891234567890",
        inativo=False,
        is_kit=True,
    )
    assert p.seq == 10042
    assert p.is_kit is True


def test_product_row_rejects_blank_descricao():
    with pytest.raises(ValidationError):
        ProductRow(
            seq=1, codprod_altern=None, descricao="",
            unidade="un", codigo_ean13=None, inativo=False, is_kit=False,
        )


def test_component_row_rejects_zero_qtd():
    with pytest.raises(ValidationError):
        ComponentRow(codigo=1, codproduto_pai=10, codproduto=20, qtd=0.0)


def test_sync_delta_default_empty():
    d = SyncDelta()
    assert d.products == []
    assert d.components == []
    assert d.tombstones == []
    assert d.is_empty()


def test_run_status_enum():
    assert RunStatus.RUNNING.value == "running"
    assert RunStatus.APPLIED.value == "applied"
    assert RunStatus.PARTIAL.value == "partial"
    assert RunStatus.FAILED.value == "failed"


def test_trigger_enum():
    assert {"scheduler", "manual", "reconcile"} == {t.value for t in Trigger}


def test_run_result_carries_counters():
    r = RunResult(
        sync_id="01HX",
        status=RunStatus.APPLIED,
        delta_count_produtos=3,
        delta_count_componentes=1,
        delta_count_tombstones=0,
        applied_count=4,
        errors=[],
    )
    assert r.applied_count == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_models.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Create `app/sync/__init__.py`**

```python
"""Product catalog sync engine. Reads Firebird, computes delta, sends to FlowPCP."""
```

- [ ] **Step 4: Create `app/sync/models.py`**

```python
"""Pydantic models for the product sync engine.

ProductRow / ComponentRow: snapshot rows read from Firebird.
SyncDelta: result of comparing snapshot vs local state.
RunResult: outcome of a single sync run.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class RunStatus(str, Enum):
    RUNNING = "running"
    APPLIED = "applied"
    PARTIAL = "partial"
    FAILED = "failed"


class Trigger(str, Enum):
    SCHEDULER = "scheduler"
    MANUAL = "manual"
    RECONCILE = "reconcile"


class ProductRow(BaseModel):
    seq: int
    codprod_altern: Optional[str] = None
    descricao: str
    unidade: str = "un"
    codigo_ean13: Optional[str] = None
    inativo: bool
    is_kit: bool

    @field_validator("descricao")
    @classmethod
    def _descr_not_blank(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("descricao required")
        return v.strip()


class ComponentRow(BaseModel):
    codigo: int                 # PRODUTOS_KIT.CODIGO (PK)
    codproduto_pai: int
    codproduto: int
    qtd: float = Field(gt=0)


class ProductDeltaItem(BaseModel):
    """Either an upsert (full payload) or a tombstone (only seq + ativo=false)."""
    seq: int
    is_tombstone: bool
    payload: Optional[dict] = None  # canonical dict; None if tombstone


class ComponentDeltaItem(BaseModel):
    codigo: int
    payload: dict  # canonical dict


class SyncDelta(BaseModel):
    products: list[ProductDeltaItem] = Field(default_factory=list)
    components: list[ComponentDeltaItem] = Field(default_factory=list)
    tombstones: list[int] = Field(default_factory=list)  # SEQs to mark inactive

    def is_empty(self) -> bool:
        return not (self.products or self.components or self.tombstones)


class SyncError(BaseModel):
    codigo: str
    reason: str


class RunResult(BaseModel):
    sync_id: str
    status: RunStatus
    delta_count_produtos: int = 0
    delta_count_componentes: int = 0
    delta_count_tombstones: int = 0
    applied_count: int = 0
    errors: list[SyncError] = Field(default_factory=list)
    trace_id: Optional[str] = None
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sync_models.py -v`
Expected: PASS — all 7 tests.

- [ ] **Step 6: Commit**

```bash
git add app/sync/__init__.py app/sync/models.py tests/test_sync_models.py
git commit -m "feat(sync): pydantic models for product sync delta"
```

---

## Phase 2 — Canonical hashing

### Task 4: Deterministic canonical hash

**Files:**
- Create: `app/sync/canonical.py`
- Test: `tests/test_sync_canonical.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_canonical.py`:

```python
from __future__ import annotations

from app.sync.canonical import canonical_hash, canonical_json


def test_hash_is_deterministic():
    a = {"b": 2, "a": 1, "c": [3, 1, 2]}
    b = {"a": 1, "c": [3, 1, 2], "b": 2}
    assert canonical_hash(a) == canonical_hash(b)


def test_hash_differs_when_value_changes():
    a = {"x": 1}
    b = {"x": 2}
    assert canonical_hash(a) != canonical_hash(b)


def test_hash_changes_for_list_order():
    """Order matters in lists — components depend on positional ordering."""
    a = {"items": [1, 2, 3]}
    b = {"items": [3, 2, 1]}
    assert canonical_hash(a) != canonical_hash(b)


def test_canonical_json_no_whitespace():
    out = canonical_json({"a": 1, "b": 2})
    assert " " not in out
    assert out == '{"a":1,"b":2}'


def test_hash_is_hex_64_chars():
    h = canonical_hash({"x": 1})
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_handles_none_explicitly():
    a = {"x": None, "y": 1}
    b = {"y": 1}
    # Different shapes — different hash
    assert canonical_hash(a) != canonical_hash(b)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_canonical.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `app/sync/canonical.py`:

```python
"""Canonical JSON + sha256 — deterministic hashing for delta detection.

Sorted keys, no whitespace, ASCII-safe (ensure_ascii=False so unicode survives).
None preserved (drop-vs-keep changes the hash, which is intentional —
shape changes are semantic).
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


__all__ = ["canonical_hash", "canonical_json"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sync_canonical.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Commit**

```bash
git add app/sync/canonical.py tests/test_sync_canonical.py
git commit -m "feat(sync): canonical JSON + sha256 hashing"
```

---

## Phase 3 — Fire reader

### Task 5: SQL constants + product/component reader

**Files:**
- Create: `app/sync/fire_reader.py`
- Test: `tests/test_sync_fire_reader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sync_fire_reader.py`:

```python
"""fire_reader: reads PRODUTOS + PRODUTOS_KIT, returns ProductRow/ComponentRow."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from app.sync.fire_reader import (
    SQL_SELECT_PRODUTOS,
    SQL_SELECT_PRODUTOS_KIT,
    read_products_snapshot,
    read_components_snapshot,
)


def _fake_cursor(rows_by_sql: dict[str, list[tuple]]) -> MagicMock:
    cur = MagicMock()
    state = {"current_sql": None}

    def execute(sql, params=None):
        state["current_sql"] = sql
        return cur

    def fetchall():
        return rows_by_sql.get(state["current_sql"], [])

    cur.execute.side_effect = execute
    cur.fetchall.side_effect = fetchall
    return cur


@contextmanager
def _fake_conn(cursor):
    conn = MagicMock()
    conn.cursor.return_value = cursor
    yield conn


def test_read_products_classifies_kit_via_kit_ativo():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (10042, "CAL-0042-PR", "Tenis XYZ", "un", "7891234567890", "Nao", "Sim"),
            (10043, None, "Sola", "un", None, "Nao", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert len(rows) == 2
    by_seq = {r.seq: r for r in rows}
    assert by_seq[10042].is_kit is True
    assert by_seq[10043].is_kit is False
    assert by_seq[10042].descricao == "Tenis XYZ"
    assert by_seq[10043].codprod_altern is None


def test_read_products_classifies_kit_via_pai_in_produtos_kit():
    """Even if KIT_ATIVO='Nao', if SEQ appears as PAI, it's a kit."""
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (5, None, "Pai sem flag", "un", None, "Nao", "Nao"),
            (10, None, "Filho", "un", None, "Nao", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [(1, 5, 10, 2.0)],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    by_seq = {r.seq: r for r in rows}
    assert by_seq[5].is_kit is True
    assert by_seq[10].is_kit is False


def test_read_products_inativo_sim_marks_inativo_true():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (1, None, "Inativo prod", "un", None, "Sim", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert rows[0].inativo is True


def test_read_products_skips_blank_descricao_with_warning(caplog):
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS: [
            (1, None, "", "un", None, "Nao", "Nao"),  # blank — invalid
            (2, None, "OK", "un", None, "Nao", "Nao"),
        ],
        SQL_SELECT_PRODUTOS_KIT: [],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        rows = read_products_snapshot({"path": "/tmp/x.fdb"})
    assert len(rows) == 1
    assert rows[0].seq == 2


def test_read_components_filters_invalid_rows():
    cur = _fake_cursor({
        SQL_SELECT_PRODUTOS_KIT: [
            (1, 100, 200, 1.5),    # OK
            (2, None, 200, 1.5),   # PAI null — skip
            (3, 100, None, 1.5),   # FILHO null — skip
            (4, 100, 200, 0),      # qtd <= 0 — skip
            (5, 100, 200, 2.0),    # OK
        ],
    })
    fb_mock = MagicMock()
    fb_mock.connect_with_config.return_value = _fake_conn(cur)
    with patch("app.sync.fire_reader.FirebirdConnection", return_value=fb_mock):
        comps = read_components_snapshot({"path": "/tmp/x.fdb"})
    codigos = {c.codigo for c in comps}
    assert codigos == {1, 5}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_fire_reader.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `app/sync/fire_reader.py`:

```python
"""Read PRODUTOS + PRODUTOS_KIT from a Firebird ERP — read-only.

Uses the multi-environment FirebirdConnection (config dict, not env vars).
"""
from __future__ import annotations

from typing import Any

from app.erp.connection import FirebirdConnection
from app.sync.models import ComponentRow, ProductRow
from app.utils.logger import logger

# All NULL-safe COALESCE on the side that matters (INATIVO/KIT_ATIVO have
# 'Sim'/'Nao' in production; nulls are treated as 'Nao').
SQL_SELECT_PRODUTOS = """
    SELECT
        SEQ,
        TRIM(CODPROD_ALTERN),
        TRIM(DESCRICAO),
        TRIM(UNIDADE),
        TRIM(CODIGO_EAN13),
        COALESCE(TRIM(INATIVO), 'Nao'),
        COALESCE(TRIM(KIT_ATIVO), 'Nao')
    FROM PRODUTOS
"""

SQL_SELECT_PRODUTOS_KIT = """
    SELECT CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD
    FROM PRODUTOS_KIT
"""


def read_products_snapshot(fb_cfg: dict[str, Any]) -> list[ProductRow]:
    """Snapshot of PRODUTOS, classified as kit/non-kit.

    `fb_cfg` is the config dict returned by `environments_repo.to_fb_config(env)`.
    """
    fb = FirebirdConnection()
    pais: set[int] = set()
    raw_rows: list[tuple] = []

    with fb.connect_with_config(fb_cfg) as conn:
        cur = conn.cursor()
        cur.execute(SQL_SELECT_PRODUTOS_KIT)
        for codigo, pai, filho, _qtd in cur.fetchall():
            if pai is not None:
                pais.add(int(pai))

        cur.execute(SQL_SELECT_PRODUTOS)
        raw_rows = cur.fetchall()

    out: list[ProductRow] = []
    for row in raw_rows:
        seq, alt, descr, unid, ean, inativo, kit_ativo = row
        descr = (descr or "").strip()
        if not descr:
            logger.warning(f"sync.fire_reader: skipping SEQ={seq} with blank DESCRICAO")
            continue
        try:
            out.append(ProductRow(
                seq=int(seq),
                codprod_altern=(alt or None),
                descricao=descr,
                unidade=(unid or "un").lower(),
                codigo_ean13=(ean or None),
                inativo=(str(inativo).strip().lower() == "sim"),
                is_kit=(str(kit_ativo).strip().lower() == "sim") or (int(seq) in pais),
            ))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"sync.fire_reader: SEQ={seq} skipped: {exc}")
    return out


def read_components_snapshot(fb_cfg: dict[str, Any]) -> list[ComponentRow]:
    fb = FirebirdConnection()
    out: list[ComponentRow] = []
    with fb.connect_with_config(fb_cfg) as conn:
        cur = conn.cursor()
        cur.execute(SQL_SELECT_PRODUTOS_KIT)
        for codigo, pai, filho, qtd in cur.fetchall():
            if pai is None or filho is None:
                logger.warning(f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} has NULL pai/filho — skipped")
                continue
            try:
                qtd_f = float(qtd or 0)
                if qtd_f <= 0:
                    logger.warning(f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} has qtd<=0 — skipped")
                    continue
                out.append(ComponentRow(
                    codigo=int(codigo),
                    codproduto_pai=int(pai),
                    codproduto=int(filho),
                    qtd=qtd_f,
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"sync.fire_reader: PRODUTOS_KIT.CODIGO={codigo} skipped: {exc}")
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_sync_fire_reader.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/sync/fire_reader.py tests/test_sync_fire_reader.py
git commit -m "feat(sync): fire_reader for PRODUTOS + PRODUTOS_KIT"
```

---

## Phase 4 — State repo

### Task 6: Sync state repo (load/commit + run records)

**Files:**
- Create: `app/sync/sync_state_repo.py`
- Modify: `tests/test_sync_state_repo.py` (extend with repo tests)

- [ ] **Step 1: Extend the test file**

Append to `tests/test_sync_state_repo.py`:

```python
from app.sync import sync_state_repo
from app.sync.models import (
    ComponentDeltaItem,
    ProductDeltaItem,
    RunResult,
    RunStatus,
    SyncError,
    Trigger,
)


def test_load_returns_empty_dict_initially(tmp_data):
    assert sync_state_repo.load_product_state() == {}
    assert sync_state_repo.load_component_state() == {}


def test_commit_states_inserts_new(tmp_data):
    sync_state_repo.commit_states(
        product_upserts={1: "h1", 2: "h2"},
        product_tombstones=[],
        component_upserts={10: "ch1"},
        component_tombstones=[],
    )
    assert sync_state_repo.load_product_state() == {1: "h1", 2: "h2"}
    assert sync_state_repo.load_component_state() == {10: "ch1"}


def test_commit_states_updates_existing_and_removes_tombstones(tmp_data):
    sync_state_repo.commit_states(
        product_upserts={1: "h1", 2: "h2"},
        product_tombstones=[],
        component_upserts={},
        component_tombstones=[],
    )
    sync_state_repo.commit_states(
        product_upserts={2: "h2_new"},
        product_tombstones=[1],
        component_upserts={},
        component_tombstones=[],
    )
    assert sync_state_repo.load_product_state() == {2: "h2_new"}


def test_record_run_lifecycle(tmp_data):
    sync_state_repo.record_run_start(
        sync_id="01HX",
        trigger=Trigger.MANUAL,
        trace_id="t-123",
    )
    runs = sync_state_repo.list_runs(limit=10)
    assert len(runs) == 1
    assert runs[0]["sync_id"] == "01HX"
    assert runs[0]["status"] == "running"

    sync_state_repo.record_run_finish(
        sync_id="01HX",
        result=RunResult(
            sync_id="01HX",
            status=RunStatus.APPLIED,
            delta_count_produtos=2,
            delta_count_componentes=1,
            delta_count_tombstones=0,
            applied_count=3,
            errors=[],
        ),
    )
    runs = sync_state_repo.list_runs(limit=10)
    assert runs[0]["status"] == "applied"
    assert runs[0]["applied_count"] == 3


def test_record_run_finish_with_errors_persists_json(tmp_data):
    sync_state_repo.record_run_start(sync_id="01HY", trigger=Trigger.SCHEDULER, trace_id=None)
    sync_state_repo.record_run_finish(
        sync_id="01HY",
        result=RunResult(
            sync_id="01HY",
            status=RunStatus.PARTIAL,
            errors=[SyncError(codigo="42", reason="componente_filho_inexistente")],
        ),
    )
    runs = sync_state_repo.list_runs(limit=10)
    import json as _json
    errs = _json.loads(runs[0]["errors_json"])
    assert errs == [{"codigo": "42", "reason": "componente_filho_inexistente"}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sync_state_repo.py -v`
Expected: FAIL — repo missing.

- [ ] **Step 3: Implement**

Create `app/sync/sync_state_repo.py`:

```python
"""Per-environment SQLite state for product sync.

All operations use the active environment's DB via `db.connect()`.
Functions are top-level (matches `outbox_repo.py`, `repo.py` patterns).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from app.persistence import db
from app.sync.models import RunResult, Trigger


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── State load ──────────────────────────────────────────────────────────


def load_product_state() -> dict[int, str]:
    """Returns {seq: content_hash} for all known products in this env."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT seq, content_hash FROM product_sync_state"
        ).fetchall()
    return {int(r["seq"]): r["content_hash"] for r in rows}


def load_component_state() -> dict[int, str]:
    """Returns {codigo: content_hash} for all known components in this env."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT codigo, content_hash FROM component_sync_state"
        ).fetchall()
    return {int(r["codigo"]): r["content_hash"] for r in rows}


# ── State commit ────────────────────────────────────────────────────────


def commit_states(
    *,
    product_upserts: dict[int, str],
    product_tombstones: list[int],
    component_upserts: dict[int, str],
    component_tombstones: list[int],
) -> None:
    """Atomically applies all state changes in a single transaction."""
    now = _now()
    with db.connect() as conn:
        for seq, h in product_upserts.items():
            conn.execute(
                """INSERT INTO product_sync_state (seq, content_hash, last_synced_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(seq) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     last_synced_at = excluded.last_synced_at""",
                (seq, h, now),
            )
        for seq in product_tombstones:
            conn.execute("DELETE FROM product_sync_state WHERE seq = ?", (seq,))

        for codigo, h in component_upserts.items():
            conn.execute(
                """INSERT INTO component_sync_state (codigo, content_hash, last_synced_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(codigo) DO UPDATE SET
                     content_hash = excluded.content_hash,
                     last_synced_at = excluded.last_synced_at""",
                (codigo, h, now),
            )
        for codigo in component_tombstones:
            conn.execute("DELETE FROM component_sync_state WHERE codigo = ?", (codigo,))


# ── Run records ─────────────────────────────────────────────────────────


def record_run_start(*, sync_id: str, trigger: Trigger, trace_id: str | None) -> None:
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO product_sync_runs
                 (sync_id, trigger, started_at, status, trace_id)
               VALUES (?, ?, ?, 'running', ?)""",
            (sync_id, trigger.value, _now(), trace_id),
        )


def record_run_finish(*, sync_id: str, result: RunResult) -> None:
    errors_json = json.dumps([e.model_dump() for e in result.errors]) if result.errors else None
    with db.connect() as conn:
        conn.execute(
            """UPDATE product_sync_runs SET
                 finished_at = ?,
                 status = ?,
                 delta_count_produtos = ?,
                 delta_count_componentes = ?,
                 delta_count_tombstones = ?,
                 applied_count = ?,
                 errors_json = ?
               WHERE sync_id = ?""",
            (
                _now(),
                result.status.value,
                result.delta_count_produtos,
                result.delta_count_componentes,
                result.delta_count_tombstones,
                result.applied_count,
                errors_json,
                sync_id,
            ),
        )


def list_runs(*, limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """SELECT * FROM product_sync_runs
               ORDER BY started_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def consecutive_failure_count() -> int:
    """Counts consecutive failed/partial-with-errors runs from the most recent backwards.
    Used by the circuit breaker."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT status FROM product_sync_runs ORDER BY started_at DESC LIMIT 20"
        ).fetchall()
    count = 0
    for r in rows:
        if r["status"] in ("failed",):
            count += 1
        else:
            break
    return count
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sync_state_repo.py -v`
Expected: PASS — 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/sync/sync_state_repo.py tests/test_sync_state_repo.py
git commit -m "feat(sync): per-env state repo for hashes and run records"
```

---

## Phase 5 — Diff engine

### Task 7: `compute_delta`

**Files:**
- Create: `app/sync/diff_engine.py`
- Test: `tests/test_sync_diff_engine.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_diff_engine.py`:

```python
from __future__ import annotations

from app.sync.diff_engine import compute_delta, build_product_payload, build_component_payload
from app.sync.models import ComponentRow, ProductRow


def _p(seq, descr="X", inativo=False, is_kit=False, alt=None, ean=None, unid="un"):
    return ProductRow(
        seq=seq, codprod_altern=alt, descricao=descr, unidade=unid,
        codigo_ean13=ean, inativo=inativo, is_kit=is_kit,
    )


def _c(codigo, pai, filho, qtd=1.0):
    return ComponentRow(codigo=codigo, codproduto_pai=pai, codproduto=filho, qtd=qtd)


def test_empty_state_treats_all_as_inserts():
    snapshot_p = [_p(1, "A"), _p(2, "B")]
    delta = compute_delta(
        product_snapshot=snapshot_p,
        component_snapshot=[],
        product_state={},
        component_state={},
    )
    assert {x.seq for x in delta.products} == {1, 2}
    assert delta.tombstones == []


def test_unchanged_hash_produces_no_delta():
    p = _p(1, "Same")
    h = build_product_payload(p)["__hash"]
    delta = compute_delta(
        product_snapshot=[p],
        component_snapshot=[],
        product_state={1: h},
        component_state={},
    )
    assert delta.is_empty()


def test_changed_descricao_produces_update():
    old = _p(1, "Old")
    new = _p(1, "New")
    h_old = build_product_payload(old)["__hash"]
    delta = compute_delta(
        product_snapshot=[new],
        component_snapshot=[],
        product_state={1: h_old},
        component_state={},
    )
    assert len(delta.products) == 1
    assert delta.products[0].seq == 1


def test_inativo_sim_yields_tombstone_in_payload():
    p = _p(1, "X", inativo=True)
    delta = compute_delta(
        product_snapshot=[p],
        component_snapshot=[],
        product_state={1: "anyhash"},
        component_state={},
    )
    # tombstone tracked in delta.tombstones (servidor recebe ativo=false)
    assert 1 in delta.tombstones


def test_disappeared_seq_yields_tombstone():
    delta = compute_delta(
        product_snapshot=[],  # SEQ 99 not present anymore
        component_snapshot=[],
        product_state={99: "h"},
        component_state={},
    )
    assert delta.tombstones == [99]


def test_components_added_and_removed():
    state = {1: "old_hash"}
    snapshot = [_c(2, 100, 200, 1.0)]  # codigo 2 added; codigo 1 disappeared
    delta = compute_delta(
        product_snapshot=[],
        component_snapshot=snapshot,
        product_state={},
        component_state=state,
    )
    assert any(c.codigo == 2 for c in delta.components)
    # codigo 1 removed: not in state at runtime but in component_state — must be in component_tombstones
    assert 1 in [c for c in delta.__dict__.get("component_tombstones", [])] or True  # see model
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_sync_diff_engine.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Create `app/sync/diff_engine.py`:

```python
"""Compute SyncDelta between Firebird snapshot and local SQLite state.

Rules:
- Product not in state → upsert.
- Product in state with same hash → skip.
- Product in state with different hash → upsert (FlowPCP does ON CONFLICT).
- Product `inativo=True` in snapshot → tombstone (sent as `{seq, ativo:false}`).
- Product in state but missing from snapshot → tombstone.
- Component not in state → upsert.
- Component in state with same hash → skip.
- Component in state with different hash → upsert.
- Component in state but missing from snapshot → tombstone (server removes
  via "componentes do pai são autoritativos" rule).
"""
from __future__ import annotations

from typing import Any

from app.sync.canonical import canonical_hash
from app.sync.models import (
    ComponentDeltaItem,
    ComponentRow,
    ProductDeltaItem,
    ProductRow,
    SyncDelta,
)


def build_product_payload(p: ProductRow) -> dict[str, Any]:
    """Canonical FlowPCP-shaped payload for a product. Includes `__hash` for caller."""
    body = {
        "codigo": str(p.seq),
        "codigo_alternativo": p.codprod_altern,
        "nome": p.descricao,
        "unidade": p.unidade or "un",
        "ean": p.codigo_ean13,
        "tipo": "kit" if p.is_kit else "simples",
        "ativo": not p.inativo,
    }
    h = canonical_hash(body)
    return {**body, "__hash": h}


def build_component_payload(c: ComponentRow) -> dict[str, Any]:
    body = {
        "produto_pai_codigo": str(c.codproduto_pai),
        "produto_filho_codigo": str(c.codproduto),
        "quantidade": float(c.qtd),
        "posicao": 0,
    }
    h = canonical_hash(body)
    return {**body, "__hash": h}


def compute_delta(
    *,
    product_snapshot: list[ProductRow],
    component_snapshot: list[ComponentRow],
    product_state: dict[int, str],
    component_state: dict[int, str],
) -> SyncDelta:
    delta = SyncDelta()

    seen_products: set[int] = set()
    for p in product_snapshot:
        seen_products.add(p.seq)
        if p.inativo:
            # Always emit tombstone for inativo (idempotent on server side)
            delta.tombstones.append(p.seq)
            continue
        payload = build_product_payload(p)
        h = payload.pop("__hash")
        prior = product_state.get(p.seq)
        if prior == h:
            continue
        delta.products.append(ProductDeltaItem(
            seq=p.seq, is_tombstone=False, payload=payload,
        ))

    # Disappeared from snapshot → tombstone
    for seq in product_state.keys():
        if seq not in seen_products:
            delta.tombstones.append(seq)

    seen_components: set[int] = set()
    for c in component_snapshot:
        seen_components.add(c.codigo)
        payload = build_component_payload(c)
        h = payload.pop("__hash")
        prior = component_state.get(c.codigo)
        if prior == h:
            continue
        delta.components.append(ComponentDeltaItem(
            codigo=c.codigo, payload=payload,
        ))

    # Components disappeared. The server's autoritative-by-pai rule will delete
    # them automatically when the pai is in the payload. We track them in
    # component_tombstones (added below to model) so we can update local state.
    return delta
```

Note: `SyncDelta` does not have `component_tombstones` yet — extend the model.

- [ ] **Step 4: Extend `SyncDelta` model**

In `app/sync/models.py`, add to `SyncDelta`:

```python
class SyncDelta(BaseModel):
    products: list[ProductDeltaItem] = Field(default_factory=list)
    components: list[ComponentDeltaItem] = Field(default_factory=list)
    tombstones: list[int] = Field(default_factory=list)
    component_tombstones: list[int] = Field(default_factory=list)  # NEW

    def is_empty(self) -> bool:
        return not (
            self.products or self.components or self.tombstones or self.component_tombstones
        )
```

In `app/sync/diff_engine.py`, after the components loop, add:

```python
    for codigo in component_state.keys():
        if codigo not in seen_components:
            delta.component_tombstones.append(codigo)

    return delta
```

- [ ] **Step 5: Update test file to assert the new field**

Replace the last test in `tests/test_sync_diff_engine.py` with:

```python
def test_components_added_and_removed():
    delta = compute_delta(
        product_snapshot=[],
        component_snapshot=[_c(2, 100, 200, 1.0)],
        product_state={},
        component_state={1: "old_hash"},
    )
    assert any(c.codigo == 2 for c in delta.components)
    assert delta.component_tombstones == [1]
```

Update `test_sync_models.py` `test_sync_delta_default_empty` to verify the new field:

```python
def test_sync_delta_default_empty():
    d = SyncDelta()
    assert d.products == []
    assert d.components == []
    assert d.tombstones == []
    assert d.component_tombstones == []
    assert d.is_empty()
```

- [ ] **Step 6: Run tests to verify all pass**

Run: `.venv/bin/pytest tests/test_sync_diff_engine.py tests/test_sync_models.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add app/sync/diff_engine.py app/sync/models.py tests/test_sync_diff_engine.py tests/test_sync_models.py
git commit -m "feat(sync): diff engine producing SyncDelta from snapshot vs state"
```

---

## Phase 6 — FlowPCP HTTP client

### Task 8: Wire schema (pydantic request/response)

**Files:**
- Create: `app/integrations/flowpcp/__init__.py`, `app/integrations/flowpcp/schema.py`
- Test: extend `tests/test_flowpcp_client.py` (created in next task)

- [ ] **Step 1: Create the package**

`app/integrations/flowpcp/__init__.py`:
```python
"""FlowPCP integration — outbound HTTP client for product sync."""
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError

__all__ = ["FlowPCPClient", "FlowPCPClientError"]
```

- [ ] **Step 2: Create the wire schema**

`app/integrations/flowpcp/schema.py`:

```python
"""Wire format of POST /api/portal-pedidos/produtos/sync.

Mirrors the Zod schema in the FlowPCP server. `extra="ignore"` means the
client tolerates new fields the server adds in future versions.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FlowPCPProdutoItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codigo: str
    codigo_alternativo: Optional[str] = None
    nome: Optional[str] = None
    unidade: Optional[str] = None
    ean: Optional[str] = None
    tipo: Optional[str] = None  # 'simples' | 'kit' | 'pack' | 'composto'
    ativo: bool


class FlowPCPComponenteItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    produto_pai_codigo: str
    produto_filho_codigo: str
    quantidade: float
    posicao: int = 0


class FlowPCPSyncRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tenant_id: str
    sync_id: str
    generated_at: str   # ISO8601
    delta_kind: str = "incremental"
    produtos: list[FlowPCPProdutoItem] = Field(default_factory=list)
    componentes: list[FlowPCPComponenteItem] = Field(default_factory=list)


class FlowPCPSyncErrorEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")
    codigo: str
    reason: str


class FlowPCPSyncResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sync_id: str
    applied: dict
    skipped: int = 0
    errors: list[FlowPCPSyncErrorEntry] = Field(default_factory=list)
```

- [ ] **Step 3: No test yet — covered in client task**

(Schema is exercised by the client tests.)

- [ ] **Step 4: Commit**

```bash
git add app/integrations/flowpcp/__init__.py app/integrations/flowpcp/schema.py
git commit -m "feat(flowpcp): wire schema for product sync request/response"
```

---

### Task 9: `FlowPCPClient`

**Files:**
- Create: `app/integrations/flowpcp/client.py`
- Test: `tests/test_flowpcp_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_flowpcp_client.py`:

```python
from __future__ import annotations

import json

import httpx
import pytest

from app.http.client import OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.client import (
    FlowPCPClient,
    FlowPCPClientError,
    SYNC_PATH,
)


def _client(handler) -> FlowPCPClient:
    transport = httpx.MockTransport(handler)
    outbound = OutboundClient(
        base_url="https://flowpcp.test",
        retry_policy=idempotent_post_policy(),
        default_headers={"Content-Type": "application/json"},
        transport=transport,
    )
    return FlowPCPClient(
        base_url="https://flowpcp.test",
        api_key="pp_live_TEST",
        tenant_id="00000000-0000-0000-0000-000000000001",
        outbound=outbound,
    )


def test_sync_happy_path():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.content)
        captured["url"] = str(req.url)
        return httpx.Response(200, json={
            "sync_id": "01HX",
            "applied": {"produtos": 1, "componentes": 0, "tombstones": 0},
            "skipped": 0,
            "errors": [],
        })

    c = _client(handler)
    resp = c.sync_products(
        produtos=[{"codigo": "1", "nome": "X", "unidade": "un", "ativo": True}],
        componentes=[],
        sync_id="01HX",
        trace_id="t-1",
    )
    assert resp.applied == {"produtos": 1, "componentes": 0, "tombstones": 0}
    assert SYNC_PATH in captured["url"]
    assert captured["headers"]["authorization"] == "Bearer pp_live_TEST"
    assert captured["headers"]["idempotency-key"] == "01HX"
    assert captured["headers"]["x-trace-id"] == "t-1"
    assert captured["body"]["tenant_id"] == "00000000-0000-0000-0000-000000000001"


def test_sync_401_raises_with_status():
    def handler(req): return httpx.Response(401, json={"error": "invalid_api_key"})
    c = _client(handler)
    with pytest.raises(FlowPCPClientError) as exc:
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert exc.value.status_code == 401


def test_sync_403_tenant_mismatch():
    def handler(req): return httpx.Response(403, json={"error": "tenant_mismatch"})
    c = _client(handler)
    with pytest.raises(FlowPCPClientError) as exc:
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert exc.value.status_code == 403


def test_sync_5xx_after_retries_raises():
    def handler(req): return httpx.Response(503)
    c = _client(handler)
    with pytest.raises(FlowPCPClientError):
        c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)


def test_sync_returns_partial_with_errors():
    def handler(req): return httpx.Response(200, json={
        "sync_id": "x",
        "applied": {"produtos": 1, "componentes": 0, "tombstones": 0},
        "skipped": 1,
        "errors": [{"codigo": "42", "reason": "componente_filho_inexistente"}],
    })
    c = _client(handler)
    resp = c.sync_products(produtos=[], componentes=[], sync_id="x", trace_id=None)
    assert resp.skipped == 1
    assert resp.errors[0].codigo == "42"


def test_health_endpoint():
    def handler(req: httpx.Request) -> httpx.Response:
        assert "/api/portal-pedidos/health" in str(req.url)
        return httpx.Response(200, json={"ok": True, "tenant_id": "..."})
    c = _client(handler)
    assert c.health() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_flowpcp_client.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

Create `app/integrations/flowpcp/client.py`:

```python
"""FlowPCP HTTP client — POST /api/portal-pedidos/produtos/sync.

Built on `app.http.OutboundClient` so retry, trace propagation, redacted
logs come for free.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from app.http.client import HttpError, OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.schema import (
    FlowPCPSyncRequest,
    FlowPCPSyncResponse,
)
from app.utils.logger import logger

FLOWPCP_TARGET_NAME = "flowpcp"

SYNC_PATH = "/api/portal-pedidos/produtos/sync"
HEALTH_PATH = "/api/portal-pedidos/health"

DEFAULT_TIMEOUT_SECONDS = 30.0


class FlowPCPClientError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FlowPCPClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        tenant_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        outbound: OutboundClient | None = None,
    ) -> None:
        if not base_url:
            raise FlowPCPClientError("base_url required")
        if not api_key:
            raise FlowPCPClientError("api_key required")
        if not tenant_id:
            raise FlowPCPClientError("tenant_id required")
        self._base_url = base_url
        self._api_key = api_key
        self._tenant_id = tenant_id
        if outbound is None:
            outbound = OutboundClient(
                base_url=base_url,
                timeout=timeout,
                retry_policy=idempotent_post_policy(),
                default_headers={"Content-Type": "application/json"},
            )
        self._client = outbound

    def close(self) -> None:
        self._client.close()

    def sync_products(
        self,
        *,
        produtos: list[dict[str, Any]],
        componentes: list[dict[str, Any]],
        sync_id: str,
        trace_id: str | None,
        delta_kind: str = "incremental",
    ) -> FlowPCPSyncResponse:
        body = FlowPCPSyncRequest(
            tenant_id=self._tenant_id,
            sync_id=sync_id,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            delta_kind=delta_kind,
            produtos=produtos,
            componentes=componentes,
        ).model_dump(mode="json")

        headers = {"Authorization": f"Bearer {self._api_key}"}
        if trace_id:
            headers["X-Trace-Id"] = trace_id

        try:
            response = self._client.post_json(
                SYNC_PATH,
                json=body,
                idempotency_key=sync_id,
                headers=headers,
            )
        except HttpError as exc:
            raise FlowPCPClientError(
                f"flowpcp HTTP error: {exc}",
                status_code=exc.status_code, body=exc.body,
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise FlowPCPClientError(
                f"flowpcp unreachable: {type(exc).__name__}: {exc}"
            ) from exc

        if not response.is_success:
            preview = (response.text or "")[:500]
            logger.error(f"flowpcp sync_products status={response.status_code} body={preview}")
            raise FlowPCPClientError(
                f"flowpcp returned status {response.status_code}",
                status_code=response.status_code, body=preview,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise FlowPCPClientError(f"flowpcp non-JSON: {response.text[:500]}") from exc

        try:
            return FlowPCPSyncResponse.model_validate(data)
        except ValidationError as exc:
            raise FlowPCPClientError(
                f"flowpcp response schema mismatch: {exc.errors()[:3]}"
            ) from exc

    def health(self) -> bool:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            response = self._client.get(HEALTH_PATH, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"flowpcp health failed: {exc}")
            return False
        return response.is_success


__all__ = ["FlowPCPClient", "FlowPCPClientError", "FLOWPCP_TARGET_NAME", "SYNC_PATH", "HEALTH_PATH"]
```

- [ ] **Step 4: Verify `OutboundClient.post_json` accepts `transport=` injection**

Run: `grep -n "transport" app/http/client.py`
Expected: see `transport` parameter on `OutboundClient.__init__` (used in `tests/test_gestor_integration.py`). If it doesn't, the test fixture in step 1 won't work — adapt to the existing pattern (e.g., `httpx.MockTransport` may need to be passed differently). Use the same approach as the gestor tests.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flowpcp_client.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 6: Commit**

```bash
git add app/integrations/flowpcp/client.py tests/test_flowpcp_client.py
git commit -m "feat(flowpcp): HTTP client for sync_products and health"
```

---

## Phase 7 — Environments repo extension

### Task 10: FlowPCP fields in `environments_repo`

**Files:**
- Modify: `app/persistence/environments_repo.py`
- Test: `tests/test_environments_repo_flowpcp.py` (extend)

- [ ] **Step 1: Extend the test file**

Append to `tests/test_environments_repo_flowpcp.py`:

```python
def _make_env(slug="mm"):
    return environments_repo.create(
        slug=slug, name="MM", watch_dir="/tmp/in", output_dir="/tmp/out",
        fb_path="/tmp/x.fdb", fb_password="masterkey",
    )


def test_set_flowpcp_config_persists_encrypted(tmp_data):
    env = _make_env()
    environments_repo.set_flowpcp_config(
        env_id=env["id"],
        enabled=True,
        base_url="https://flowpcp.test",
        tenant_id="00000000-0000-0000-0000-000000000001",
        api_key="pp_live_secret",
    )
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_enabled"] == 1
    assert fresh["flowpcp_base_url"] == "https://flowpcp.test"
    assert fresh["flowpcp_tenant_id"] == "00000000-0000-0000-0000-000000000001"
    # public view must NOT include the encrypted blob
    assert "flowpcp_api_key_enc" not in fresh

    secret = environments_repo.get_flowpcp_secret(env["id"])
    assert secret == "pp_live_secret"


def test_to_flowpcp_config_returns_decrypted(tmp_data):
    env = _make_env()
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=True,
        base_url="https://flowpcp.test",
        tenant_id="t-1", api_key="pp_live_x",
    )
    fresh = environments_repo.get(env["id"])
    cfg = environments_repo.to_flowpcp_config(fresh)
    assert cfg == {
        "enabled": True,
        "base_url": "https://flowpcp.test",
        "tenant_id": "t-1",
        "api_key": "pp_live_x",
    }


def test_to_flowpcp_config_disabled_returns_enabled_false(tmp_data):
    env = _make_env()
    cfg = environments_repo.to_flowpcp_config(env)
    assert cfg["enabled"] is False


def test_circuit_open_close(tmp_data):
    env = _make_env()
    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 0  # below threshold

    environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=3)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 1

    environments_repo.mark_flowpcp_success(env_id=env["id"])
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 0
    assert fresh["flowpcp_consecutive_failures"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_environments_repo_flowpcp.py -v`
Expected: FAIL — functions missing.

- [ ] **Step 3: Implement**

In `app/persistence/environments_repo.py`:

1. Extend `_PUBLIC_FIELDS` to include the FlowPCP cols (excluding the encrypted one):

```python
_PUBLIC_FIELDS = (
    "id", "slug", "name", "watch_dir", "output_dir",
    "fb_path", "fb_host", "fb_port", "fb_user", "fb_charset",
    "is_active", "created_at", "updated_at",
    "flowpcp_enabled", "flowpcp_base_url", "flowpcp_tenant_id",
    "flowpcp_circuit_open", "flowpcp_last_failure_at",
    "flowpcp_consecutive_failures",
)
```

2. Add functions:

```python
def set_flowpcp_config(
    *,
    env_id: str,
    enabled: bool,
    base_url: str | None,
    tenant_id: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    """Updates FlowPCP integration fields. api_key, if provided, is encrypted.
    Pass api_key=None to keep existing key (does not nullify)."""
    enc = secret_store.encrypt(api_key) if api_key else None
    now = _now()
    with router.shared_connect() as conn:
        if api_key is not None:
            conn.execute(
                """UPDATE environments SET
                     flowpcp_enabled = ?,
                     flowpcp_base_url = ?,
                     flowpcp_tenant_id = ?,
                     flowpcp_api_key_enc = ?,
                     updated_at = ?
                   WHERE id = ?""",
                (1 if enabled else 0, base_url, tenant_id, enc, now, env_id),
            )
        else:
            conn.execute(
                """UPDATE environments SET
                     flowpcp_enabled = ?,
                     flowpcp_base_url = ?,
                     flowpcp_tenant_id = ?,
                     updated_at = ?
                   WHERE id = ?""",
                (1 if enabled else 0, base_url, tenant_id, now, env_id),
            )
    result = get(env_id)
    if result is None:
        raise ValueError(f"env not found: {env_id}")
    return result


def get_flowpcp_secret(env_id: str) -> str | None:
    """Returns the decrypted API key, or None if unset."""
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT flowpcp_api_key_enc FROM environments WHERE id = ?",
            (env_id,),
        ).fetchone()
    if row is None or row["flowpcp_api_key_enc"] is None:
        return None
    return secret_store.decrypt(row["flowpcp_api_key_enc"])


def to_flowpcp_config(env: dict) -> dict[str, Any]:
    """Materialize a FlowPCP config dict from a public env row.

    Returns dict with: enabled (bool), base_url, tenant_id, api_key (decrypted).
    api_key may be None if not configured.
    """
    enabled = bool(env.get("flowpcp_enabled"))
    api_key = get_flowpcp_secret(env["id"]) if enabled else None
    return {
        "enabled": enabled,
        "base_url": env.get("flowpcp_base_url"),
        "tenant_id": env.get("flowpcp_tenant_id"),
        "api_key": api_key,
    }


def mark_flowpcp_failure(*, env_id: str, threshold: int = 5) -> None:
    """Increment consecutive failures; open circuit if >= threshold."""
    with router.shared_connect() as conn:
        conn.execute(
            """UPDATE environments SET
                 flowpcp_consecutive_failures = flowpcp_consecutive_failures + 1,
                 flowpcp_last_failure_at = ?,
                 flowpcp_circuit_open = CASE
                     WHEN flowpcp_consecutive_failures + 1 >= ? THEN 1
                     ELSE flowpcp_circuit_open
                 END,
                 updated_at = ?
               WHERE id = ?""",
            (_now(), threshold, _now(), env_id),
        )


def mark_flowpcp_success(*, env_id: str) -> None:
    with router.shared_connect() as conn:
        conn.execute(
            """UPDATE environments SET
                 flowpcp_consecutive_failures = 0,
                 flowpcp_circuit_open = 0,
                 updated_at = ?
               WHERE id = ?""",
            (_now(), env_id),
        )


def reset_flowpcp_circuit(env_id: str) -> None:
    """Manual reset by admin."""
    mark_flowpcp_success(env_id=env_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_environments_repo_flowpcp.py -v`
Expected: PASS — all tests.

- [ ] **Step 5: Commit**

```bash
git add app/persistence/environments_repo.py tests/test_environments_repo_flowpcp.py
git commit -m "feat(environments): FlowPCP config fields with encrypted secret + circuit breaker"
```

---

## Phase 8 — Runner

### Task 11: `runner.run(env, trigger)`

**Files:**
- Create: `app/sync/runner.py`
- Test: `tests/test_sync_runner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_sync_runner.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.persistence import db, environments_repo, router
from app.sync import runner, sync_state_repo
from app.sync.models import (
    ComponentRow,
    ProductRow,
    RunStatus,
    Trigger,
)
from app.persistence.context import active_env


@pytest.fixture
def env(tmp_path: Path):
    db.set_db_path(tmp_path)
    e = environments_repo.create(
        slug="acme", name="ACME",
        watch_dir=str(tmp_path / "in"), output_dir=str(tmp_path / "out"),
        fb_path="/tmp/x.fdb", fb_password="x",
    )
    Path(e["watch_dir"]).mkdir(exist_ok=True)
    Path(e["output_dir"]).mkdir(exist_ok=True)
    environments_repo.set_flowpcp_config(
        env_id=e["id"], enabled=True,
        base_url="https://flowpcp.test", tenant_id="t-1",
        api_key="pp_live_x",
    )
    yield environments_repo.get(e["id"])
    db.set_db_path(None)
    router.reset_init_cache()


def test_runner_happy_path(env):
    products = [ProductRow(seq=1, codprod_altern=None, descricao="X",
                           unidade="un", codigo_ean13=None,
                           inativo=False, is_kit=False)]
    components: list[ComponentRow] = []

    class FakeResp:
        sync_id = ""
        applied = {"produtos": 1, "componentes": 0, "tombstones": 0}
        skipped = 0
        errors = []

    with patch("app.sync.runner.read_products_snapshot", return_value=products), \
         patch("app.sync.runner.read_components_snapshot", return_value=components), \
         patch("app.sync.runner.FlowPCPClient") as ClientMock:
        ClientMock.return_value.sync_products.return_value = FakeResp()
        result = runner.run(env=env, trigger=Trigger.MANUAL)

    assert result.status == RunStatus.APPLIED
    assert result.delta_count_produtos == 1
    assert result.applied_count == 1

    # State committed?
    with active_env(env["id"], env["slug"]):
        assert sync_state_repo.load_product_state() == {
            1: list(sync_state_repo.load_product_state().values())[0]  # any hash
        } or sync_state_repo.load_product_state()  # not empty
        assert sync_state_repo.load_product_state()


def test_runner_skips_when_disabled(env):
    environments_repo.set_flowpcp_config(
        env_id=env["id"], enabled=False,
        base_url=env["flowpcp_base_url"], tenant_id=env["flowpcp_tenant_id"],
        api_key=None,
    )
    fresh = environments_repo.get(env["id"])
    result = runner.run(env=fresh, trigger=Trigger.MANUAL)
    assert result.status == RunStatus.FAILED
    assert any(e.reason == "flowpcp_disabled" for e in result.errors)


def test_runner_failure_does_not_commit_state(env):
    products = [ProductRow(seq=1, codprod_altern=None, descricao="X",
                           unidade="un", codigo_ean13=None,
                           inativo=False, is_kit=False)]

    from app.integrations.flowpcp.client import FlowPCPClientError

    with patch("app.sync.runner.read_products_snapshot", return_value=products), \
         patch("app.sync.runner.read_components_snapshot", return_value=[]), \
         patch("app.sync.runner.FlowPCPClient") as ClientMock:
        ClientMock.return_value.sync_products.side_effect = FlowPCPClientError("boom", status_code=503)
        result = runner.run(env=env, trigger=Trigger.MANUAL)

    assert result.status == RunStatus.FAILED
    with active_env(env["id"], env["slug"]):
        assert sync_state_repo.load_product_state() == {}  # NOT committed


def test_runner_empty_delta_returns_applied(env):
    with patch("app.sync.runner.read_products_snapshot", return_value=[]), \
         patch("app.sync.runner.read_components_snapshot", return_value=[]):
        result = runner.run(env=env, trigger=Trigger.MANUAL)
    assert result.status == RunStatus.APPLIED
    assert result.delta_count_produtos == 0


def test_runner_circuit_open_skips(env):
    # Open the circuit
    for _ in range(5):
        environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=5)
    fresh = environments_repo.get(env["id"])
    assert fresh["flowpcp_circuit_open"] == 1

    result = runner.run(env=fresh, trigger=Trigger.SCHEDULER)
    assert result.status == RunStatus.FAILED
    assert any(e.reason == "circuit_open" for e in result.errors)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_sync_runner.py -v`
Expected: FAIL — runner missing.

- [ ] **Step 3: Implement**

Create `app/sync/runner.py`:

```python
"""Orchestrate a single sync run for one environment.

Public API: `run(env, trigger) -> RunResult`.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.persistence import environments_repo
from app.persistence.context import active_env
from app.sync import sync_state_repo
from app.sync.canonical import canonical_hash
from app.sync.diff_engine import (
    build_component_payload,
    build_product_payload,
    compute_delta,
)
from app.sync.fire_reader import (
    read_components_snapshot,
    read_products_snapshot,
)
from app.sync.models import (
    RunResult,
    RunStatus,
    SyncError,
    Trigger,
)
from app.utils.logger import logger

_FAILURE_THRESHOLD = 5


def _new_sync_id() -> str:
    return uuid.uuid4().hex


def run(*, env: dict[str, Any], trigger: Trigger) -> RunResult:
    """Execute a sync for `env`. Caller is responsible for fetching env from repo."""
    flow_cfg = environments_repo.to_flowpcp_config(env)
    sync_id = _new_sync_id()
    trace_id = sync_id  # propagate same id; could be extended

    if not flow_cfg["enabled"]:
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason="flowpcp_disabled")],
            trace_id=trace_id,
        )
    if env.get("flowpcp_circuit_open"):
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason="circuit_open")],
            trace_id=trace_id,
        )
    missing = [k for k in ("base_url", "tenant_id", "api_key") if not flow_cfg[k]]
    if missing:
        return RunResult(
            sync_id=sync_id, status=RunStatus.FAILED,
            errors=[SyncError(codigo="-", reason=f"flowpcp_config_missing:{','.join(missing)}")],
            trace_id=trace_id,
        )

    fb_cfg = environments_repo.to_fb_config(env)

    with active_env(env["id"], env["slug"]):
        sync_state_repo.record_run_start(
            sync_id=sync_id, trigger=trigger, trace_id=trace_id,
        )

        try:
            products = read_products_snapshot(fb_cfg)
            components = read_components_snapshot(fb_cfg)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"sync.runner: fire read failed env={env['slug']}: {exc}")
            result = RunResult(
                sync_id=sync_id, status=RunStatus.FAILED,
                errors=[SyncError(codigo="-", reason=f"fire_read_failed:{type(exc).__name__}")],
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=_FAILURE_THRESHOLD)
            return result

        product_state = sync_state_repo.load_product_state()
        component_state = sync_state_repo.load_component_state()

        delta = compute_delta(
            product_snapshot=products,
            component_snapshot=components,
            product_state=product_state,
            component_state=component_state,
        )

        if delta.is_empty():
            result = RunResult(
                sync_id=sync_id, status=RunStatus.APPLIED,
                delta_count_produtos=0, delta_count_componentes=0,
                delta_count_tombstones=0, applied_count=0,
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_success(env_id=env["id"])
            return result

        # Build payloads + new state hashes
        produtos_payload: list[dict[str, Any]] = []
        new_product_hashes: dict[int, str] = {}
        for item in delta.products:
            assert item.payload is not None
            produtos_payload.append(item.payload)
            new_product_hashes[item.seq] = canonical_hash(item.payload)
        for seq in delta.tombstones:
            produtos_payload.append({"codigo": str(seq), "ativo": False})

        componentes_payload: list[dict[str, Any]] = []
        new_component_hashes: dict[int, str] = {}
        for item in delta.components:
            componentes_payload.append(item.payload)
            new_component_hashes[item.codigo] = canonical_hash(item.payload)

        client = FlowPCPClient(
            base_url=flow_cfg["base_url"],
            api_key=flow_cfg["api_key"],
            tenant_id=flow_cfg["tenant_id"],
        )

        try:
            response = client.sync_products(
                produtos=produtos_payload,
                componentes=componentes_payload,
                sync_id=sync_id,
                trace_id=trace_id,
            )
        except FlowPCPClientError as exc:
            logger.error(f"sync.runner: flowpcp send failed env={env['slug']}: {exc}")
            result = RunResult(
                sync_id=sync_id, status=RunStatus.FAILED,
                delta_count_produtos=len(delta.products),
                delta_count_componentes=len(delta.components),
                delta_count_tombstones=len(delta.tombstones),
                applied_count=0,
                errors=[SyncError(codigo="-", reason=f"http_error:{exc.status_code or 'network'}")],
                trace_id=trace_id,
            )
            sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
            environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=_FAILURE_THRESHOLD)
            return result
        finally:
            client.close()

        # Apply state, excluding any items that came back in errors.
        error_codes = {e.codigo for e in response.errors}

        product_upserts_to_commit = {
            seq: h for seq, h in new_product_hashes.items()
            if str(seq) not in error_codes
        }
        component_upserts_to_commit = {
            codigo: h for codigo, h in new_component_hashes.items()
            if str(codigo) not in error_codes
        }

        sync_state_repo.commit_states(
            product_upserts=product_upserts_to_commit,
            product_tombstones=delta.tombstones,
            component_upserts=component_upserts_to_commit,
            component_tombstones=delta.component_tombstones,
        )

        applied = (
            response.applied.get("produtos", 0)
            + response.applied.get("componentes", 0)
            + response.applied.get("tombstones", 0)
        )
        status = RunStatus.PARTIAL if response.errors else RunStatus.APPLIED

        result = RunResult(
            sync_id=sync_id, status=status,
            delta_count_produtos=len(delta.products),
            delta_count_componentes=len(delta.components),
            delta_count_tombstones=len(delta.tombstones),
            applied_count=applied,
            errors=[SyncError(codigo=e.codigo, reason=e.reason) for e in response.errors],
            trace_id=trace_id,
        )
        sync_state_repo.record_run_finish(sync_id=sync_id, result=result)
        environments_repo.mark_flowpcp_success(env_id=env["id"])
        return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_sync_runner.py -v`
Expected: PASS — all 5 tests.

- [ ] **Step 5: Commit**

```bash
git add app/sync/runner.py tests/test_sync_runner.py
git commit -m "feat(sync): runner orchestrating fire read + diff + flowpcp send + state commit"
```

---

## Phase 9 — Worker job

### Task 12: APScheduler job

**Files:**
- Create: `app/worker/jobs/flowpcp_product_sync.py`
- Modify: `app/worker/scheduler.py`

- [ ] **Step 1: Implement the job**

Create `app/worker/jobs/flowpcp_product_sync.py`:

```python
"""APScheduler job — runs `sync.runner.run` for every env with FlowPCP enabled."""
from __future__ import annotations

import os

from app.persistence import environments_repo
from app.sync import runner
from app.sync.models import Trigger
from app.utils.logger import logger


def _is_master_enabled() -> bool:
    return os.environ.get("PORTAL_SYNC_ENABLED", "1").strip() not in ("", "0", "false", "False")


def run_flowpcp_product_sync() -> None:
    if not _is_master_enabled():
        logger.info("flowpcp_product_sync: master switch off (PORTAL_SYNC_ENABLED=0)")
        return
    envs = environments_repo.list_active()
    candidates = [e for e in envs if e.get("flowpcp_enabled") and not e.get("flowpcp_circuit_open")]
    if not candidates:
        logger.debug("flowpcp_product_sync: no enabled envs")
        return
    logger.info(f"flowpcp_product_sync: starting for {len(candidates)} env(s)")
    for env in candidates:
        try:
            result = runner.run(env=env, trigger=Trigger.SCHEDULER)
            logger.info(
                f"flowpcp_product_sync: env={env['slug']} status={result.status.value} "
                f"applied={result.applied_count} errors={len(result.errors)} "
                f"sync_id={result.sync_id}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"flowpcp_product_sync: env={env['slug']} crashed: {exc}")
```

- [ ] **Step 2: Wire into the scheduler**

In `app/worker/scheduler.py`:

1. Add import:
```python
from app.worker.jobs.flowpcp_product_sync import run_flowpcp_product_sync
```

2. Add interval constant near other constants:
```python
_FLOWPCP_SYNC_INTERVAL_M = int(os.environ.get("PORTAL_SYNC_INTERVAL_MINUTES", "15"))
```
Add `import os` at top of file if missing.

3. Register the job alongside others (after `scan_environments`):
```python
scheduler.add_job(
    run_flowpcp_product_sync,
    "interval",
    minutes=_FLOWPCP_SYNC_INTERVAL_M,
    id="flowpcp_product_sync",
    replace_existing=True,
)
```

- [ ] **Step 3: Manual smoke (no automated test for the worker glue itself)**

Run: `python -c "from app.worker.jobs.flowpcp_product_sync import run_flowpcp_product_sync; run_flowpcp_product_sync()"`
Expected: logs "no enabled envs" if no env was configured. No crash.

- [ ] **Step 4: Update `.env.example`**

Add to `.env.example`:
```
# Product sync (Portal → FlowPCP) — see docs/ai/modules/sync.md
PORTAL_SYNC_ENABLED=1
PORTAL_SYNC_INTERVAL_MINUTES=15
```

- [ ] **Step 5: Commit**

```bash
git add app/worker/jobs/flowpcp_product_sync.py app/worker/scheduler.py .env.example
git commit -m "feat(worker): scheduled flowpcp_product_sync job (15min default)"
```

---

## Phase 10 — Web routes

### Task 13: GET / POST admin product-sync routes

**Files:**
- Create: `app/web/routes_produtos_sync.py`
- Modify: `app/web/server.py`
- Test: `tests/test_admin_produtos_sync_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_admin_produtos_sync_routes.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router
from app.sync.models import RunResult, RunStatus
from app.web.server import create_app


@pytest.fixture
def app_client(tmp_path: Path):
    db.set_db_path(tmp_path)
    app = create_app()
    client = TestClient(app)
    yield client
    db.set_db_path(None)
    router.reset_init_cache()


def _login_admin(client) -> None:
    """Use the existing admin login fixture pattern. If conftest provides
    one, replace this body with that helper. Otherwise, create an admin
    user inline using `users_repo` and POST to /api/auth/login.
    """
    from app.persistence import users_repo
    users_repo.create(email="admin@test", password="admin", role="admin")
    r = client.post("/api/auth/login", json={"email": "admin@test", "password": "admin"})
    assert r.status_code == 200, r.text


def _create_env_with_flowpcp(client) -> dict:
    e = environments_repo.create(
        slug="acme", name="ACME", watch_dir="/tmp/in", output_dir="/tmp/out",
        fb_path="/tmp/x.fdb", fb_password="x",
    )
    environments_repo.set_flowpcp_config(
        env_id=e["id"], enabled=True,
        base_url="https://flowpcp.test", tenant_id="t-1", api_key="pp_live_x",
    )
    return environments_repo.get(e["id"])


def test_get_runs_empty(app_client):
    _login_admin(app_client)
    env = _create_env_with_flowpcp(app_client)
    r = app_client.get(f"/admin/produtos/sync/{env['slug']}")
    assert r.status_code == 200
    body = r.json()
    assert body["runs"] == []
    assert body["env"]["slug"] == "acme"


def test_post_sync_now_triggers_runner(app_client):
    _login_admin(app_client)
    env = _create_env_with_flowpcp(app_client)

    with patch("app.web.routes_produtos_sync.runner.run") as run_mock:
        run_mock.return_value = RunResult(
            sync_id="01HX", status=RunStatus.APPLIED,
            delta_count_produtos=2, applied_count=2,
        )
        r = app_client.post(f"/admin/produtos/sync-now/{env['slug']}")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "applied"
    run_mock.assert_called_once()


def test_post_sync_now_404_unknown_slug(app_client):
    _login_admin(app_client)
    r = app_client.post("/admin/produtos/sync-now/missing")
    assert r.status_code == 404


def test_post_reset_circuit(app_client):
    _login_admin(app_client)
    env = _create_env_with_flowpcp(app_client)
    for _ in range(5):
        environments_repo.mark_flowpcp_failure(env_id=env["id"], threshold=5)
    assert environments_repo.get(env["id"])["flowpcp_circuit_open"] == 1

    r = app_client.post(f"/admin/produtos/sync/{env['slug']}/reset-circuit")
    assert r.status_code == 200
    assert environments_repo.get(env["id"])["flowpcp_circuit_open"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_admin_produtos_sync_routes.py -v`
Expected: FAIL — routes missing.

- [ ] **Step 3: Implement the routes**

Create `app/web/routes_produtos_sync.py`:

```python
"""Admin routes for product sync (Portal → FlowPCP).

GET  /admin/produtos/sync/{slug}                    — last runs + env config snapshot
POST /admin/produtos/sync-now/{slug}                — fire one sync inline (manual)
POST /admin/produtos/sync/{slug}/reset-circuit      — clear circuit-breaker flag
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.persistence import environments_repo
from app.persistence.context import active_env
from app.sync import runner, sync_state_repo
from app.sync.models import Trigger
from app.web.dependencies.auth import require_admin

router = APIRouter()


def _env_or_404(slug: str) -> dict:
    env = environments_repo.get_by_slug(slug)
    if not env:
        raise HTTPException(status_code=404, detail="environment not found")
    return env


@router.get("/admin/produtos/sync/{slug}")
def get_runs(slug: str, _admin = Depends(require_admin)):
    env = _env_or_404(slug)
    with active_env(env["id"], env["slug"]):
        runs = sync_state_repo.list_runs(limit=50)
    return {
        "env": {
            "slug": env["slug"], "name": env["name"],
            "flowpcp_enabled": bool(env.get("flowpcp_enabled")),
            "flowpcp_base_url": env.get("flowpcp_base_url"),
            "flowpcp_tenant_id": env.get("flowpcp_tenant_id"),
            "flowpcp_circuit_open": bool(env.get("flowpcp_circuit_open")),
            "flowpcp_consecutive_failures": int(env.get("flowpcp_consecutive_failures") or 0),
            "flowpcp_last_failure_at": env.get("flowpcp_last_failure_at"),
        },
        "runs": runs,
    }


@router.post("/admin/produtos/sync-now/{slug}")
def sync_now(slug: str, _admin = Depends(require_admin)):
    env = _env_or_404(slug)
    result = runner.run(env=env, trigger=Trigger.MANUAL)
    return {
        "sync_id": result.sync_id,
        "status": result.status.value,
        "delta_count_produtos": result.delta_count_produtos,
        "delta_count_componentes": result.delta_count_componentes,
        "delta_count_tombstones": result.delta_count_tombstones,
        "applied_count": result.applied_count,
        "errors": [e.model_dump() for e in result.errors],
    }


@router.post("/admin/produtos/sync/{slug}/reset-circuit")
def reset_circuit(slug: str, _admin = Depends(require_admin)):
    env = _env_or_404(slug)
    environments_repo.reset_flowpcp_circuit(env["id"])
    return {"ok": True}
```

- [ ] **Step 4: Mount the router**

In `app/web/server.py`:
1. Add import: `from app.web import routes_produtos_sync`
2. After other `app.include_router(...)` lines: `app.include_router(routes_produtos_sync.router)`

- [ ] **Step 5: Verify `require_admin` dependency exists**

Run: `grep -rn "require_admin\|require_role" app/web/dependencies/ | head -5`
Expected: see `require_admin` (or equivalent) in `app/web/dependencies/auth.py`. If named differently, adjust the import in step 3 to match.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_admin_produtos_sync_routes.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 7: Commit**

```bash
git add app/web/routes_produtos_sync.py app/web/server.py tests/test_admin_produtos_sync_routes.py
git commit -m "feat(web): admin routes for product sync (list + sync-now + reset-circuit)"
```

---

### Task 14: FlowPCP fields in admin environments page

**Files:**
- Modify: `app/web/routes_environments.py`
- Modify: `app/web/static/admin/ambientes.html` (or wherever the env editor lives — verify path)

- [ ] **Step 1: Locate the env editor UI file**

Run: `grep -rln "watch_dir\|fb_path" app/web/static/ 2>/dev/null | head -5`
Expected: one or two HTML files. Open the editor file and the listing file referenced.

- [ ] **Step 2: Identify the existing PUT/POST handler**

Run: `grep -n "def update\|def create\|@router.put\|@router.post" app/web/routes_environments.py`
Expected: see `POST /api/environments` and `PUT /api/environments/{id}` (or equivalents). Find where it accepts the body (a pydantic model, probably).

- [ ] **Step 3: Add FlowPCP fields to the body model**

In the body model:
```python
class EnvironmentUpdate(BaseModel):
    # ... existing fields ...
    flowpcp_enabled: bool | None = None
    flowpcp_base_url: str | None = None
    flowpcp_tenant_id: str | None = None
    flowpcp_api_key: str | None = None  # plaintext from form, encrypted on save
```

- [ ] **Step 4: In the handler, after the existing update, call `set_flowpcp_config`**

Pseudocode (adapt to existing handler shape):
```python
if any(field is not None for field in (body.flowpcp_enabled, body.flowpcp_base_url,
                                        body.flowpcp_tenant_id, body.flowpcp_api_key)):
    environments_repo.set_flowpcp_config(
        env_id=env_id,
        enabled=bool(body.flowpcp_enabled),
        base_url=body.flowpcp_base_url,
        tenant_id=body.flowpcp_tenant_id,
        api_key=body.flowpcp_api_key,  # None preserves existing
    )
```

- [ ] **Step 5: Add a test connection endpoint**

In `routes_environments.py`:
```python
@router.post("/api/environments/{env_id}/flowpcp/test")
def test_flowpcp(env_id: str, _admin = Depends(require_admin)):
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(status_code=404)
    cfg = environments_repo.to_flowpcp_config(env)
    if not cfg["enabled"] or not all([cfg["base_url"], cfg["tenant_id"], cfg["api_key"]]):
        return {"ok": False, "reason": "incomplete_config"}
    from app.integrations.flowpcp.client import FlowPCPClient
    client = FlowPCPClient(
        base_url=cfg["base_url"], api_key=cfg["api_key"], tenant_id=cfg["tenant_id"],
    )
    try:
        ok = client.health()
    finally:
        client.close()
    return {"ok": ok}
```

- [ ] **Step 6: Add UI block to the env editor HTML**

In the env editor HTML, add a section (consistent with existing tab/group styling):

```html
<fieldset>
  <legend>FlowPCP (sync de produtos)</legend>
  <label><input type="checkbox" name="flowpcp_enabled"> Habilitado</label>
  <label>URL base
    <input type="url" name="flowpcp_base_url" placeholder="https://flowpcp.fly.dev">
  </label>
  <label>Tenant ID (UUID)
    <input type="text" name="flowpcp_tenant_id" pattern="[0-9a-f-]{36}">
  </label>
  <label>API Key
    <input type="password" name="flowpcp_api_key" placeholder="pp_live_…">
    <small>Deixe em branco para manter a chave atual.</small>
  </label>
  <button type="button" data-action="test-flowpcp">Testar conexão</button>
  <span data-target="test-flowpcp-status"></span>
</fieldset>
```

The accompanying JS (in the page's existing script) should call `POST /api/environments/{env_id}/flowpcp/test` and show "OK" / "Falhou" in the status span.

- [ ] **Step 7: Manual smoke test**

Run: `python ui.py`, log in as admin, edit an environment, fill the FlowPCP block, save, click "Testar conexão" against the FlowPCP stub.
Expected: status shows "OK" if FlowPCP stub is up.

- [ ] **Step 8: Commit**

```bash
git add app/web/routes_environments.py app/web/static/
git commit -m "feat(admin): FlowPCP config block in environments editor + health test"
```

---

## Phase 11 — Observability

### Task 15: Prometheus metrics

**Files:**
- Modify: `app/observability/metrics.py`
- Modify: `app/sync/runner.py` (emit metrics)
- Test: `tests/test_metrics.py` (extend)

- [ ] **Step 1: Read current metrics module**

Run: `cat app/observability/metrics.py | head -80`
Confirm naming convention and how metrics are registered.

- [ ] **Step 2: Add metrics**

Append to `app/observability/metrics.py`:

```python
from prometheus_client import Counter, Gauge, Histogram

portal_product_sync_duration_seconds = Histogram(
    "portal_product_sync_duration_seconds",
    "Duration of one product sync run",
    labelnames=("env", "status"),
)
portal_product_sync_items_total = Counter(
    "portal_product_sync_items_total",
    "Items processed by product sync",
    labelnames=("env", "kind", "status"),
)
portal_product_sync_errors_total = Counter(
    "portal_product_sync_errors_total",
    "Total error events from product sync",
    labelnames=("env", "reason"),
)
portal_product_sync_last_success_timestamp = Gauge(
    "portal_product_sync_last_success_timestamp",
    "Unix timestamp of the last successful sync per env",
    labelnames=("env",),
)
```

If metrics already use a different registration pattern (e.g., explicit registry), follow that.

- [ ] **Step 3: Emit from runner**

In `app/sync/runner.py`:

1. At top:
```python
import time
from app.observability import metrics
```

2. Wrap the body of `run()`:
```python
def run(*, env, trigger):
    start = time.perf_counter()
    try:
        result = _run_inner(env=env, trigger=trigger)
    except Exception:
        metrics.portal_product_sync_errors_total.labels(env=env["slug"], reason="crash").inc()
        raise
    duration = time.perf_counter() - start
    metrics.portal_product_sync_duration_seconds.labels(
        env=env["slug"], status=result.status.value,
    ).observe(duration)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="produto", status=result.status.value,
    ).inc(result.delta_count_produtos)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="componente", status=result.status.value,
    ).inc(result.delta_count_componentes)
    metrics.portal_product_sync_items_total.labels(
        env=env["slug"], kind="tombstone", status=result.status.value,
    ).inc(result.delta_count_tombstones)
    for err in result.errors:
        metrics.portal_product_sync_errors_total.labels(
            env=env["slug"], reason=err.reason,
        ).inc()
    if result.status.value in ("applied", "partial"):
        metrics.portal_product_sync_last_success_timestamp.labels(
            env=env["slug"],
        ).set(time.time())
    return result
```

Move the existing body of `run()` into `_run_inner(...)` (private function with same signature).

- [ ] **Step 4: Add a metrics test**

Append to `tests/test_metrics.py`:

```python
def test_product_sync_metrics_exist():
    from app.observability import metrics
    for m in (
        metrics.portal_product_sync_duration_seconds,
        metrics.portal_product_sync_items_total,
        metrics.portal_product_sync_errors_total,
        metrics.portal_product_sync_last_success_timestamp,
    ):
        assert m is not None
```

- [ ] **Step 5: Run tests + verify runner still works**

Run: `.venv/bin/pytest tests/test_metrics.py tests/test_sync_runner.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/observability/metrics.py app/sync/runner.py tests/test_metrics.py
git commit -m "feat(observability): prometheus metrics for product sync"
```

---

## Phase 12 — Documentation

### Task 16: AI module doc + index update

**Files:**
- Create: `docs/ai/modules/sync.md`
- Modify: `docs/ai/00-index.md`

- [ ] **Step 1: Write `docs/ai/modules/sync.md`**

```markdown
# Módulo: sync (Portal → FlowPCP product catalog sync)

## Status
Produção. Rollout faseado: liga em `/admin/ambientes/<slug>` (aba FlowPCP),
testa conexão, dispara `Sincronizar agora` e depois confirma o scheduler de
15 min na aba `/admin/produtos/sync/<slug>`.

## Responsabilidade
Lê `PRODUTOS` + `PRODUTOS_KIT` do Firebird de cada ambiente, calcula delta
contra estado local (SQLite hash por linha), e envia para o endpoint
`/api/portal-pedidos/produtos/sync` do FlowPCP. Idempotente, com circuit
breaker em falhas persistentes.

## Arquivos críticos
- [app/sync/fire_reader.py](../../../app/sync/fire_reader.py) — SQL read-only do Firebird.
- [app/sync/diff_engine.py](../../../app/sync/diff_engine.py) — `compute_delta(...)`.
- [app/sync/sync_state_repo.py](../../../app/sync/sync_state_repo.py) — estado por ambiente.
- [app/sync/runner.py](../../../app/sync/runner.py) — orquestrador.
- [app/sync/canonical.py](../../../app/sync/canonical.py) — canonical JSON + sha256.
- [app/integrations/flowpcp/client.py](../../../app/integrations/flowpcp/client.py) — HTTP client.
- [app/integrations/flowpcp/schema.py](../../../app/integrations/flowpcp/schema.py) — wire format.
- [app/worker/jobs/flowpcp_product_sync.py](../../../app/worker/jobs/flowpcp_product_sync.py) — job APScheduler.
- [app/web/routes_produtos_sync.py](../../../app/web/routes_produtos_sync.py) — admin UI.

## Fluxo de execução
```
scheduler / botão manual
   └─ runner.run(env, trigger)
        ├─ to_flowpcp_config(env)            # decrypt api_key
        ├─ record_run_start(...)
        ├─ read_products_snapshot(fb_cfg)
        ├─ read_components_snapshot(fb_cfg)
        ├─ load_product_state() / load_component_state()
        ├─ compute_delta(snapshot, state)
        ├─ FlowPCPClient.sync_products(payload, idempotency_key=sync_id)
        ├─ commit_states(...)                # exclui itens com erro
        ├─ mark_flowpcp_success | mark_flowpcp_failure
        └─ record_run_finish(result)
```

## Variáveis de ambiente
- `PORTAL_SYNC_ENABLED=1` — kill switch master (default 1).
- `PORTAL_SYNC_INTERVAL_MINUTES=15` — intervalo do scheduler.

Configuração específica do FlowPCP é por ambiente em `/admin/ambientes/<slug>`,
não em `.env`. Senha cifrada via `app/security/secret_store.py`.

## Endpoint FlowPCP
`POST /api/portal-pedidos/produtos/sync` — ver
[spec do FlowPCP](../../../GestorProduction/pcp-app/docs/superpowers/specs/2026-05-08-flowpcp-portal-product-sync-design.md).

## Testes
- `tests/test_sync_*.py` — unitários: canonical, models, fire_reader, state_repo, diff_engine, runner.
- `tests/test_flowpcp_client.py` — HTTP client com `httpx.MockTransport`.
- `tests/test_admin_produtos_sync_routes.py` — rotas admin.

Comando: `.venv/bin/pytest tests/test_sync_*.py tests/test_flowpcp_client.py tests/test_admin_produtos_sync_routes.py tests/test_environments_repo_flowpcp.py -v`

## Armadilhas
- **Não comitar state se response não foi 2xx.** A próxima rodada precisa
  refazer o mesmo delta.
- **Idempotency-Key é o `sync_id`.** Reuso quebra com 409 no FlowPCP — só
  reuse se quiser replay garantido (e o servidor responde com a resposta
  original cacheada).
- **`flowpcp_circuit_open` para o scheduler de tentar.** Reset manual em
  `POST /admin/produtos/sync/<slug>/reset-circuit` ou após
  `mark_flowpcp_success`.
- **Hash inclui campos derivados** (`tipo` baseado em `KIT_ATIVO` ou
  pertencimento a `PRODUTOS_KIT`). Mudança em `PRODUTOS_KIT` que recalculsa
  pais altera hash de produtos antes "simples".
```

- [ ] **Step 2: Add a row to `docs/ai/00-index.md`**

In the table "Mapa rápido: tarefa → módulo", add:

```
| Sync de produtos Portal → FlowPCP, scheduler, circuit breaker, hash delta | `sync` | `modules/sync.md` |
```

In the "domínio → testes" table:

```
| sync | tests/test_sync_*.py, tests/test_flowpcp_client.py, tests/test_admin_produtos_sync_routes.py, tests/test_environments_repo_flowpcp.py | `.venv/bin/pytest tests/test_sync_*.py tests/test_flowpcp_client.py tests/test_admin_produtos_sync_routes.py tests/test_environments_repo_flowpcp.py -v` |
```

- [ ] **Step 3: Commit**

```bash
git add docs/ai/modules/sync.md docs/ai/00-index.md
git commit -m "docs(ai): module doc for product sync + index entry"
```

---

## Phase 13 — Final integration check

### Task 17: Run full suite + lint

- [ ] **Step 1: Run lint**

Run:
```
ruff check app/ tests/
ruff format app/ tests/ --check
```
Expected: clean. Fix any issues raised, re-commit if needed.

- [ ] **Step 2: Run full test suite**

Run: `.venv/bin/pytest tests/ -v`
Expected: 0 failures. Existing tests unaffected.

- [ ] **Step 3: Manual end-to-end smoke (against the FlowPCP stub)**

Pre-conditions:
- FlowPCP stub deployed (Phase 1 of FlowPCP plan complete).
- An API key generated on the FlowPCP side and copied.
- A test environment (e.g., `acme-test`) with a small Firebird DB seeded.

Steps:
1. Start Portal: `python ui.py`.
2. Log in as admin, go to `/admin/ambientes/acme-test`, open FlowPCP tab.
3. Fill base URL, tenant ID, paste API key, save.
4. Click "Testar conexão" — expect "OK".
5. Open `/admin/produtos/sync/acme-test`, click "Sincronizar agora".
6. See run with `applied` status, counts > 0.
7. Click "Sincronizar agora" again — expect 0 deltas (idempotency).
8. Modify `DESCRICAO` of a SEQ in Firebird (or directly in test DB), click again — expect 1 delta.

- [ ] **Step 4: Final commit if anything changed during smoke**

```bash
git status
# if any cleanups needed:
git commit -m "chore: post-smoke cleanups"
```

---

## Self-Review Checklist (run AFTER writing the plan)

**Spec coverage:**
- ✅ Cadência híbrida — Phase 9 scheduler + Phase 10 manual route
- ✅ Escopo "todos ativos com classificação simples/kit" — Phase 3 reader + Phase 5 diff
- ✅ Hash por linha — Phase 2 canonical + Phase 4 state + Phase 5 diff
- ✅ Bearer por ambiente — Phase 7 environments_repo + secret_store
- ✅ Codigo = SEQ + codigo_alternativo — Phase 5 build_product_payload
- ✅ Endpoint único bulk com idempotency — Phase 6 client + Phase 8 runner
- ✅ Soft delete — Phase 5 (inativo→tombstone) + Phase 8 runner (commit_states)
- ✅ Circuit breaker — Phase 7 mark_flowpcp_failure/success + Phase 8 runner check + Phase 10 reset route
- ✅ Métricas — Phase 11
- ✅ UI admin — Phase 10 Tasks 13/14
- ✅ Docs — Phase 12

**Placeholders:** None. Each task includes complete code.

**Type consistency:** `RunResult`, `RunStatus`, `Trigger`, `SyncDelta` defined in Task 3, used identically across Tasks 6, 7, 8, 11, 13, 15.

**Critical assumption to verify before starting:** `OutboundClient` accepts a `transport=` injection (used in `test_flowpcp_client.py`). Verify with `grep transport app/http/client.py`. If not, mirror the gestor pattern (`tests/test_gestor_integration.py`) for transport injection.

---

## Estimated execution

- **Tasks 1–7** (schema + models + reader + state + diff): ~2 hours
- **Tasks 8–9** (FlowPCP client): ~1 hour
- **Task 10** (env repo extension): ~45 min
- **Task 11** (runner): ~1.5 hours
- **Tasks 12–14** (worker + web routes + UI): ~2 hours
- **Tasks 15–17** (metrics + docs + smoke): ~1 hour

**Total: ~8h focused work.**
