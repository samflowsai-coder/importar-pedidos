# Price Validation (Pedido vs Fire) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Comparar preço do pedido com `PRODUTOS.PRECO_VENDA` no Fire; bloquear hard em divergência ou pedido sem preço; permitir ack explícito (com audit) quando produto está sem preço cadastrado no Fire (estado de transição).

**Architecture:** Estende `app/erp/product_check.py` com `price_status` por item e `is_blocking()` agregado. Sidecar em `imports.sem_preco_ack_*` (padrão `cliente_override_*` que já existe). Re-check + guard no servidor em `_send_one_to_fire` e `_export_one_xlsx` (defesa em profundidade — front gateia botão por UX, server bloqueia por integridade). UI ganha coluna "Fire" consolidando match e preço, banner com estados, modal de ack.

**Tech Stack:** Python 3.11, FastAPI, Pydantic v2, SQLite (sidecar), Firebird via `firebird-driver` (read-only no check), prometheus-client, pytest, vanilla JS.

**Spec de referência:** [docs/superpowers/specs/2026-05-08-price-validation-design.md](docs/superpowers/specs/2026-05-08-price-validation-design.md).

---

### Task 1: Schema — colunas sidecar de ack em `imports`

**Files:**
- Modify: `app/persistence/schema_env.py:11-43` (TABLES_SQL bloco `imports`)
- Modify: `app/persistence/schema_env.py:144` (`COLUMN_MIGRATIONS`)
- Test: `tests/test_persistence_repo.py` (novo teste no fim)

- [ ] **Step 1: Escrever teste falhando — DB nova ganha colunas**

Adicionar ao final de `tests/test_persistence_repo.py`:

```python
def test_schema_includes_sem_preco_ack_columns(sqlite_tmp):
    """schema_env.TABLES_SQL deve incluir as 3 colunas do sidecar de ack."""
    from app.persistence import db
    with db.connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    assert "sem_preco_ack_by" in cols
    assert "sem_preco_ack_at" in cols
    assert "sem_preco_ack_items" in cols
```

- [ ] **Step 2: Rodar teste — confirmar falha**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_schema_includes_sem_preco_ack_columns -v
```

Expected: FAIL — coluna não existe.

- [ ] **Step 3: Adicionar colunas em TABLES_SQL**

Em [app/persistence/schema_env.py:42](app/persistence/schema_env.py#L42), antes do fecha-parênteses da `CREATE TABLE imports`, adicionar:

```python
    file_sha256              TEXT,
    original_path            TEXT,
    sem_preco_ack_by         TEXT,
    sem_preco_ack_at         TEXT,
    sem_preco_ack_items      TEXT
);
```

(Mantenha a ordem; a vírgula vai antes da última coluna — última linha sem vírgula final.)

- [ ] **Step 4: Adicionar entradas em COLUMN_MIGRATIONS**

Em [app/persistence/schema_env.py:144](app/persistence/schema_env.py#L144), substituir `COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = ()` por:

```python
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("imports", "sem_preco_ack_by",
        "ALTER TABLE imports ADD COLUMN sem_preco_ack_by TEXT"),
    ("imports", "sem_preco_ack_at",
        "ALTER TABLE imports ADD COLUMN sem_preco_ack_at TEXT"),
    ("imports", "sem_preco_ack_items",
        "ALTER TABLE imports ADD COLUMN sem_preco_ack_items TEXT"),
)
```

- [ ] **Step 5: Rodar teste — passa**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_schema_includes_sem_preco_ack_columns -v
```

Expected: PASS.

- [ ] **Step 6: Adicionar teste de idempotência da migration**

Em `tests/test_persistence_repo.py`:

```python
def test_column_migration_is_idempotent(tmp_path):
    """Rodar _ensure_schema duas vezes na mesma DB não deve falhar."""
    from app.persistence import db, schema_env
    from app.persistence.router import _ensure_schema

    db_path = tmp_path / "app_state_test.db"
    _ensure_schema(db_path, schema_env)
    _ensure_schema(db_path, schema_env)  # segunda vez é o teste real

    import sqlite3
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(imports)").fetchall()}
    conn.close()
    assert {"sem_preco_ack_by", "sem_preco_ack_at", "sem_preco_ack_items"} <= cols
```

- [ ] **Step 7: Rodar — passa**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_column_migration_is_idempotent -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add app/persistence/schema_env.py tests/test_persistence_repo.py
git commit -m "feat(persistence): add sem_preco_ack_* sidecar columns to imports

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Repo — `set_sem_preco_ack` + extensão de SELECTs

**Files:**
- Modify: `app/persistence/repo.py:230-247` (get_import SELECT + _row_to_entry)
- Modify: `app/persistence/repo.py:189-211` (list_imports SELECT)
- Modify: `app/persistence/repo.py` (adicionar `set_sem_preco_ack` no fim)
- Test: `tests/test_persistence_repo.py`

- [ ] **Step 1: Escrever teste falhando — set_sem_preco_ack persiste**

Adicionar em `tests/test_persistence_repo.py`:

```python
def test_set_sem_preco_ack_persists_and_get_returns_them(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)

    fresh = repo.get_import(e["id"])
    assert fresh["sem_preco_ack_by"] is None
    assert fresh["sem_preco_ack_at"] is None
    assert fresh["sem_preco_ack_items"] is None

    items = [
        {"ean": "7891234567890", "product_code": "ABC123", "fire_product_id": 42},
        {"ean": None, "product_code": "XYZ", "fire_product_id": 7},
    ]
    repo.set_sem_preco_ack(e["id"], by_email="op@example.com", items=items)

    got = repo.get_import(e["id"])
    assert got["sem_preco_ack_by"] == "op@example.com"
    assert got["sem_preco_ack_at"]  # ISO timestamp
    assert got["sem_preco_ack_items"] == items
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_set_sem_preco_ack_persists_and_get_returns_them -v
```

Expected: FAIL com `AttributeError: module 'app.persistence.repo' has no attribute 'set_sem_preco_ack'`.

- [ ] **Step 3: Adicionar `set_sem_preco_ack` em `app/persistence/repo.py`**

Adicionar **logo depois** de `set_client_override` (após [app/persistence/repo.py:363](app/persistence/repo.py#L363)):

```python
def set_sem_preco_ack(
    import_id: str,
    *,
    by_email: str,
    items: list[dict],
) -> None:
    """Persiste o ack do operador para itens sem preço cadastrado no Fire.

    Sidecar — não toca snapshot. Last-write-wins. `items` é lista de dicts
    {ean, product_code, fire_product_id}; serializado como JSON.
    """
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE imports
            SET sem_preco_ack_by    = ?,
                sem_preco_ack_at    = ?,
                sem_preco_ack_items = ?
            WHERE id = ?
            """,
            (
                by_email,
                datetime.now().isoformat(timespec="seconds"),
                json.dumps(items, ensure_ascii=False),
                import_id,
            ),
        )
```

- [ ] **Step 4: Estender `get_import` SELECT**

Em [app/persistence/repo.py:234-242](app/persistence/repo.py#L234), adicionar as 3 colunas no SELECT (antes do `FROM imports`):

```python
            SELECT id, source_filename, imported_at, order_number,
                   customer_cnpj, customer_name, fire_codigo,
                   snapshot_json, check_json, output_files_json, db_result_json,
                   status, error,
                   portal_status, sent_to_fire_at,
                   production_status, released_at, released_by,
                   trace_id, state_version, gestor_order_id, apontae_order_id,
                   cliente_override_codigo, cliente_override_razao,
                   cliente_override_at, cliente_override_by,
                   sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items
            FROM imports WHERE id = ?
```

- [ ] **Step 5: Estender `list_imports` SELECT**

Em [app/persistence/repo.py:194-203](app/persistence/repo.py#L194), adicionar mesmo trio:

```python
        SELECT id, source_filename, imported_at, order_number,
               customer_cnpj, customer_name, fire_codigo,
               snapshot_json, check_json, output_files_json, db_result_json,
               status, error,
               portal_status, sent_to_fire_at,
               production_status, released_at, released_by,
               trace_id, state_version, gestor_order_id, apontae_order_id,
               cliente_override_codigo, cliente_override_razao,
               cliente_override_at, cliente_override_by,
               sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items
        FROM imports
```

- [ ] **Step 6: Estender `_row_to_entry`**

Em [app/persistence/repo.py:108-141](app/persistence/repo.py#L108), antes de `"output_files":`, adicionar:

```python
        "sem_preco_ack_by":    _get("sem_preco_ack_by"),
        "sem_preco_ack_at":    _get("sem_preco_ack_at"),
        "sem_preco_ack_items": (
            json.loads(_get("sem_preco_ack_items"))
            if _get("sem_preco_ack_items") else None
        ),
```

- [ ] **Step 7: Rodar — passa**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_set_sem_preco_ack_persists_and_get_returns_them -v
```

Expected: PASS.

- [ ] **Step 8: Adicionar teste — INSERT não clobbera ack**

```python
def test_insert_import_does_not_clobber_sem_preco_ack(sqlite_tmp):
    e = _entry()
    repo.insert_import(e)
    repo.set_sem_preco_ack(e["id"], by_email="op@example.com", items=[{"ean": "x", "product_code": "p"}])

    # Re-upsert — não pode limpar ack
    e_again = _entry(id=e["id"], customer="OUTRO")
    repo.insert_import(e_again)

    got = repo.get_import(e["id"])
    assert got["sem_preco_ack_by"] == "op@example.com"
    assert got["sem_preco_ack_items"] == [{"ean": "x", "product_code": "p"}]
```

- [ ] **Step 9: Rodar — passa (sem mudança no INSERT, pois colunas não estão na lista de UPDATE-ON-CONFLICT)**

```bash
.venv/bin/pytest tests/test_persistence_repo.py::test_insert_import_does_not_clobber_sem_preco_ack -v
```

Expected: PASS — INSERT em [app/persistence/repo.py:62-98](app/persistence/repo.py#L62) já não toca essas colunas.

- [ ] **Step 10: Atualizar comentário do INSERT**

Em [app/persistence/repo.py:91-95](app/persistence/repo.py#L91), atualizar o comentário:

```python
                -- portal_status, production_status, state_version,
                -- sent_to_fire_at, released_at, released_by,
                -- cliente_override_codigo, cliente_override_razao,
                -- cliente_override_at, cliente_override_by,
                -- sem_preco_ack_by, sem_preco_ack_at, sem_preco_ack_items
                -- are SM-owned or set via dedicated helpers — never clobbered here.
```

E na docstring de `insert_import` ([app/persistence/repo.py:14-24](app/persistence/repo.py#L14)) adicionar:

```python
    Sidecar do ack de itens sem preço (`sem_preco_ack_*`) é gerenciado
    por `set_sem_preco_ack()` — também nunca clobbado no upsert.
```

- [ ] **Step 11: Rodar suite do repo inteira**

```bash
.venv/bin/pytest tests/test_persistence_repo.py -v
```

Expected: PASS em tudo.

- [ ] **Step 12: Commit**

```bash
git add app/persistence/repo.py tests/test_persistence_repo.py
git commit -m "feat(persistence): set_sem_preco_ack + propagate ack fields in get/list

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `product_check` — campos `unit_price_order`, `price_status`, `price_diff`, `price_summary`

**Files:**
- Modify: `app/erp/product_check.py:26-35` (`_empty_item_result`)
- Modify: `app/erp/product_check.py:38-139` (`check_order` — popular novos campos + summary)
- Test: `tests/test_product_check.py` (novo arquivo)

- [ ] **Step 1: Criar `tests/test_product_check.py` com helper de mock e primeiro teste**

```python
"""Tests for app.erp.product_check — match e price_status."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.erp import product_check
from app.models.order import Order, OrderHeader, OrderItem


def _order(items_kwargs: list[dict], *, customer_cnpj: str = "00000000000100") -> Order:
    return Order(
        header=OrderHeader(order_number="T1", customer_cnpj=customer_cnpj, customer_name="ACME"),
        items=[OrderItem(quantity=1.0, **kw) for kw in items_kwargs],
    )


def _make_fb_ctx(*, client_row=None, product_rows: dict | None = None):
    """Cria um context manager fake que devolve cursor com fetchone() programado.

    `product_rows` mapeia (query_str, bind_value) -> tuple|None.
    """
    cur = MagicMock()
    rows_seq: list = []

    def execute_side_effect(sql, params):
        # Decide o próximo fetchone com base no SQL
        if "FROM CADASTRO" in sql:
            rows_seq.append(client_row)
        elif "FROM PRODUTOS" in sql:
            key = ("ean" if "CODIGO_EAN13" in sql else "code", params[0])
            rows_seq.append((product_rows or {}).get(key))
        else:
            rows_seq.append(None)

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.side_effect = lambda: rows_seq.pop(0)

    conn = MagicMock()
    conn.cursor.return_value = cur

    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_match_exact(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        client_row=(1, "ACME"),
        product_rows={("ean", "7891"): (10, "TENIS", 89.90)},
    )

    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)

    item = report["items"][0]
    assert item["price_status"] == "match"
    assert item["unit_price_order"] == 89.90
    assert item["fire_preco_venda"] == 89.90
    assert item["price_diff"] == 0.0
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_product_check.py -v
```

Expected: FAIL com `KeyError: 'price_status'` (ou similar — não existe ainda).

- [ ] **Step 3: Estender `_empty_item_result`**

Em [app/erp/product_check.py:26-35](app/erp/product_check.py#L26), substituir por:

```python
def _empty_item_result(product_code: Optional[str], ean: Optional[str], unit_price_order: Optional[float]) -> dict:
    return {
        "product_code": product_code,
        "ean": ean,
        "match": False,
        "match_source": None,
        "fire_product_id": None,
        "fire_description": None,
        "fire_preco_venda": None,
        "unit_price_order": unit_price_order,
        "price_status": "no_product_match",
        "price_diff": None,
    }
```

- [ ] **Step 4: Adicionar helper de comparação em centavos**

Após `_cnpj_digits` (linha 23), adicionar:

```python
def _to_cents(value: Optional[float]) -> Optional[int]:
    """Converte reais em centavos (int) para comparação sem drift de float."""
    if value is None:
        return None
    return int(round(float(value) * 100))


def _classify_price(unit_price_order: Optional[float], fire_preco_venda: Optional[float]) -> str:
    """Determina price_status para um item COM match de produto.

    Não chame para itens sem match — use 'no_product_match' diretamente.
    """
    if unit_price_order is None:
        return "no_order_price"
    if fire_preco_venda is None or _to_cents(fire_preco_venda) == 0:
        return "no_price_in_fire"
    if _to_cents(unit_price_order) == _to_cents(fire_preco_venda):
        return "match"
    return "mismatch"
```

- [ ] **Step 5: Atualizar `check_order` para popular novos campos**

Em [app/erp/product_check.py:46-55](app/erp/product_check.py#L46) (no `unavailable`), trocar a list-comp:

```python
        "items": [
            _empty_item_result(it.product_code, it.ean, it.unit_price)
            for it in order.items
        ],
```

E no loop principal ([app/erp/product_check.py:85-113](app/erp/product_check.py#L85)), substituir por:

```python
            items_report: list[dict] = []
            matched = 0
            price_match = 0
            price_mismatch = 0
            price_no_price_in_fire = 0
            price_no_order_price = 0

            for it in order.items:
                entry = _empty_item_result(it.product_code, it.ean, it.unit_price)
                if it.ean:
                    cur.execute(queries.FIND_PRODUCT_BY_EAN, (it.ean,))
                    row = cur.fetchone()
                    if row:
                        entry.update({
                            "match": True,
                            "match_source": "ean",
                            "fire_product_id": row[0],
                            "fire_description": row[1],
                            "fire_preco_venda": float(row[2]) if row[2] is not None else None,
                        })
                if not entry["match"] and it.product_code:
                    cur.execute(queries.FIND_PRODUCT_BY_CODE, (it.product_code,))
                    row = cur.fetchone()
                    if row:
                        entry.update({
                            "match": True,
                            "match_source": "codprod_altern",
                            "fire_product_id": row[0],
                            "fire_description": row[1],
                            "fire_preco_venda": float(row[2]) if row[2] is not None else None,
                        })

                if entry["match"]:
                    matched += 1
                    status = _classify_price(it.unit_price, entry["fire_preco_venda"])
                    entry["price_status"] = status
                    if status == "match":
                        price_match += 1
                    elif status == "mismatch":
                        price_mismatch += 1
                    elif status == "no_price_in_fire":
                        price_no_price_in_fire += 1
                    elif status == "no_order_price":
                        price_no_order_price += 1
                    fire_p = entry["fire_preco_venda"]
                    if fire_p is not None and it.unit_price is not None:
                        entry["price_diff"] = round(float(fire_p) - float(it.unit_price), 2)
                # else: price_status fica 'no_product_match' (default), price_diff None
                items_report.append(entry)

            cur.close()
```

- [ ] **Step 6: Atualizar bloco de retorno final ([app/erp/product_check.py:123-139](app/erp/product_check.py#L123))**

```python
    return {
        "available": True,
        "reason": None,
        "client": {
            "match": client_id is not None,
            "fire_id": client_id,
            "razao_social": razao,
            "cnpj": order.header.customer_cnpj,
        },
        "items": items_report,
        "summary": {
            "items_total": len(order.items),
            "items_matched": matched,
            "items_missing": len(order.items) - matched,
            "client_matched": client_id is not None,
            "price_summary": {
                "items_match": price_match,
                "items_mismatch": price_mismatch,
                "items_no_price_in_fire": price_no_price_in_fire,
                "items_no_order_price": price_no_order_price,
            },
        },
    }
```

- [ ] **Step 7: Rodar — passa**

```bash
.venv/bin/pytest tests/test_product_check.py::test_price_status_match_exact -v
```

Expected: PASS.

- [ ] **Step 8: Adicionar testes para todos os outros estados**

```python
@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_one_cent(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 89.91)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["price_status"] == "mismatch"
    assert item["price_diff"] == 0.01


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_mismatch_round_value(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 100.00)},
    )
    order = _order([{"ean": "7891", "unit_price": 99.00}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "mismatch"
    assert report["items"][0]["price_diff"] == 1.0


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_null(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", None)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"
    assert report["items"][0]["fire_preco_venda"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_price_in_fire_zero(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 0.0)},
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_price_in_fire"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_order_price(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={("ean", "7891"): (10, "TENIS", 50.0)},
    )
    order = _order([{"ean": "7891", "unit_price": None}])
    report = product_check.check_order(order)
    assert report["items"][0]["price_status"] == "no_order_price"


@patch("app.erp.product_check.FirebirdConnection")
def test_price_status_no_product_match(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={},  # nada no Fire
    )
    order = _order([{"ean": "7891", "unit_price": 89.90}])
    report = product_check.check_order(order)
    item = report["items"][0]
    assert item["match"] is False
    assert item["price_status"] == "no_product_match"
    assert item["price_diff"] is None


@patch("app.erp.product_check.FirebirdConnection")
def test_summary_aggregates_price_counts(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_fb.return_value.connect.return_value = _make_fb_ctx(
        product_rows={
            ("ean", "A"): (1, "X", 10.0),  # match
            ("ean", "B"): (2, "Y", 12.0),  # mismatch
            ("ean", "C"): (3, "Z", None),  # no_price_in_fire
            ("ean", "D"): (4, "W", 50.0),  # no_order_price
            # E não cadastrado → no_product_match
        },
    )
    order = _order([
        {"ean": "A", "unit_price": 10.0},
        {"ean": "B", "unit_price": 11.0},
        {"ean": "C", "unit_price": 30.0},
        {"ean": "D", "unit_price": None},
        {"ean": "E", "unit_price": 5.0},
    ])
    summary = product_check.check_order(order)["summary"]["price_summary"]
    assert summary == {
        "items_match": 1,
        "items_mismatch": 1,
        "items_no_price_in_fire": 1,
        "items_no_order_price": 1,
    }
```

- [ ] **Step 9: Rodar — todos passam**

```bash
.venv/bin/pytest tests/test_product_check.py -v
```

Expected: 7 PASS.

- [ ] **Step 10: Commit**

```bash
git add app/erp/product_check.py tests/test_product_check.py
git commit -m "feat(erp): price_status per item + price_summary on product_check

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `product_check.is_blocking()` — agregação para guard

**Files:**
- Modify: `app/erp/product_check.py` (adicionar `is_blocking` no fim)
- Test: `tests/test_product_check.py`

- [ ] **Step 1: Escrever testes falhando**

Adicionar em `tests/test_product_check.py`:

```python
def _check_with(items: list[dict]) -> dict:
    return {"available": True, "items": items, "summary": {}}


def test_is_blocking_passes_match_only():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "match"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is False
    assert detail["items_mismatch"] == []
    assert detail["items_no_order_price"] == []
    assert detail["items_no_price_unacked"] == []


def test_is_blocking_blocks_on_mismatch():
    check = _check_with([
        {"ean": "A", "product_code": "p1", "price_status": "mismatch",
         "unit_price_order": 11.0, "fire_preco_venda": 10.0},
    ])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_mismatch"] == [
        {"ean": "A", "product_code": "p1", "order_price": 11.0, "fire_price": 10.0},
    ]


def test_is_blocking_blocks_on_no_order_price():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_order_price"}])
    blocked, detail = product_check.is_blocking(check)
    assert blocked is True
    assert detail["items_no_order_price"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_blocks_on_no_price_unacked():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, detail = product_check.is_blocking(check, ack_items=None)
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "A", "product_code": "p1"}]


def test_is_blocking_passes_with_ack_by_ean():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": "A", "product_code": None}])
    assert blocked is False


def test_is_blocking_passes_with_ack_by_code():
    check = _check_with([{"ean": None, "product_code": "p1", "price_status": "no_price_in_fire"}])
    blocked, _ = product_check.is_blocking(check, ack_items=[{"ean": None, "product_code": "p1"}])
    assert blocked is False


def test_is_blocking_partial_ack_still_blocks():
    check = _check_with([
        {"ean": "A", "product_code": "p1", "price_status": "no_price_in_fire"},
        {"ean": "B", "product_code": "p2", "price_status": "no_price_in_fire"},
    ])
    blocked, detail = product_check.is_blocking(
        check, ack_items=[{"ean": "A", "product_code": "p1"}],
    )
    assert blocked is True
    assert detail["items_no_price_unacked"] == [{"ean": "B", "product_code": "p2"}]


def test_is_blocking_ignores_no_product_match():
    check = _check_with([{"ean": "A", "product_code": "p1", "price_status": "no_product_match"}])
    blocked, _ = product_check.is_blocking(check)
    assert blocked is False  # comportamento atual mantido — sem match não bloqueia aqui


def test_is_blocking_returns_false_when_check_unavailable():
    blocked, _ = product_check.is_blocking({"available": False, "items": []})
    assert blocked is False  # check off → segue (best-effort)
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_product_check.py::test_is_blocking_passes_match_only -v
```

Expected: FAIL com `AttributeError`.

- [ ] **Step 3: Implementar `is_blocking` no fim de `app/erp/product_check.py`**

```python
def is_blocking(check: dict, ack_items: Optional[list[dict]] = None) -> tuple[bool, dict]:
    """Decide se o estado do check impede envio.

    Bloqueia se:
      - Algum item com price_status='mismatch'
      - Algum item com price_status='no_order_price'
      - Algum item com price_status='no_price_in_fire' não coberto por ack_items

    `ack_items`: lista [{ean, product_code, ...}] vinda de
    imports.sem_preco_ack_items. Item é considerado coberto se EAN bate
    (quando ambos presentes) OU product_code bate.

    Quando check['available'] é False, devolve (False, ...) — best-effort:
    sem dados pra avaliar, não bloqueia.

    Retorna (blocked, detail) onde detail = {
      "items_mismatch": [{ean, product_code, order_price, fire_price}],
      "items_no_order_price": [{ean, product_code}],
      "items_no_price_unacked": [{ean, product_code}],
    }.
    """
    detail = {
        "items_mismatch": [],
        "items_no_order_price": [],
        "items_no_price_unacked": [],
    }
    if not check.get("available"):
        return False, detail

    ack = ack_items or []
    ack_eans = {a.get("ean") for a in ack if a.get("ean")}
    ack_codes = {a.get("product_code") for a in ack if a.get("product_code")}

    def _covered(item: dict) -> bool:
        if item.get("ean") and item["ean"] in ack_eans:
            return True
        if item.get("product_code") and item["product_code"] in ack_codes:
            return True
        return False

    for it in check.get("items", []):
        status = it.get("price_status")
        if status == "mismatch":
            detail["items_mismatch"].append({
                "ean": it.get("ean"),
                "product_code": it.get("product_code"),
                "order_price": it.get("unit_price_order"),
                "fire_price": it.get("fire_preco_venda"),
            })
        elif status == "no_order_price":
            detail["items_no_order_price"].append({
                "ean": it.get("ean"),
                "product_code": it.get("product_code"),
            })
        elif status == "no_price_in_fire" and not _covered(it):
            detail["items_no_price_unacked"].append({
                "ean": it.get("ean"),
                "product_code": it.get("product_code"),
            })

    blocked = bool(
        detail["items_mismatch"]
        or detail["items_no_order_price"]
        or detail["items_no_price_unacked"]
    )
    return blocked, detail
```

- [ ] **Step 4: Rodar todos os testes de is_blocking**

```bash
.venv/bin/pytest tests/test_product_check.py -v -k is_blocking
```

Expected: 9 PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/product_check.py tests/test_product_check.py
git commit -m "feat(erp): is_blocking() helper aggregates price_status + ack

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Endpoint `POST /api/imported/{id}/ack-sem-preco`

**Files:**
- Modify: `app/web/server.py` (rota nova, perto da `/override-cliente` em [app/web/server.py:2070](app/web/server.py#L2070))
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Escrever teste falhando — happy path**

Adicionar no fim de `tests/test_web_server.py`:

```python
def test_ack_sem_preco_persists_and_audits(monkeypatch):
    """POST /api/imported/{id}/ack-sem-preco grava sidecar + audit."""
    from app.persistence import repo
    from app.erp import product_check as pc_mod
    import uuid
    from datetime import datetime

    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "ACK-1",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": "ACK-1", "customer_cnpj": "00000000000100"},
            "items": [{"description": "x", "quantity": 1.0, "ean": "7891", "unit_price": 89.90}],
            "source_file": "",
        },
    })

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "no_price_in_fire", "fire_product_id": 42}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)

    r = client.post(f"/api/imported/{entry_id}/ack-sem-preco")
    assert r.status_code == 200, r.text

    body = r.json()
    assert body["ack_by"] == "test@portal.local"  # do TEST_AUTH_BYPASS
    assert body["items_acked"] == [
        {"ean": "7891", "product_code": None, "fire_product_id": 42},
    ]

    got = repo.get_import(entry_id)
    assert got["sem_preco_ack_by"] == "test@portal.local"
    assert got["sem_preco_ack_items"] == [
        {"ean": "7891", "product_code": None, "fire_product_id": 42},
    ]

    audits = [a["event_type"] for a in repo.list_audit(entry_id)]
    assert "sem_preco_acknowledged" in audits


def test_ack_sem_preco_rejects_wrong_status():
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "ACK-2",
        "status": "success",
        "portal_status": "sent_to_fire",
        "snapshot": {"header": {"order_number": "ACK-2"}, "items": []},
    })
    r = client.post(f"/api/imported/{entry_id}/ack-sem-preco")
    assert r.status_code == 409


def test_ack_sem_preco_returns_404_when_missing():
    r = client.post("/api/imported/does-not-exist/ack-sem-preco")
    assert r.status_code == 404
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_web_server.py::test_ack_sem_preco_persists_and_audits -v
```

Expected: FAIL — endpoint não existe (404).

- [ ] **Step 3: Implementar endpoint em `app/web/server.py`**

Adicionar **logo depois** da rota `/override-cliente` (após [app/web/server.py:2118](app/web/server.py#L2118), antes de `@app.get("/api/imported/{import_id}/preview")`):

```python
@app.post("/api/imported/{import_id}/ack-sem-preco")
def ack_sem_preco(
    import_id: str,
    request: Request,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Registra ack do operador para itens sem preço cadastrado no Fire.

    Re-roda o check_order para coletar a lista atual de itens com
    price_status='no_price_in_fire' e persiste em imports.sem_preco_ack_*.
    Audit log grava a ação. Itens com mismatch ou no_order_price NÃO podem
    ser ack-ados — devolveriam 409 implicitamente porque o guard server-side
    no envio bloqueia mesmo com ack.
    """
    from app.persistence import repo
    from app.models.order import Order
    from app.erp.product_check import check_order
    from app.observability import metrics

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(
            status_code=409,
            detail=f"Pedido não está 'em revisão' (status atual: {entry.get('portal_status')})",
        )
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot indisponível")
    try:
        order = Order.model_validate(snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Snapshot inválido: {exc}") from exc

    request_env = getattr(request.state, "environment", None)
    with with_trace_id(entry.get("trace_id")):
        check = check_order(order, env=request_env)
        if not check.get("available"):
            raise HTTPException(status_code=503, detail="Fire indisponível para validar ack")

        items_acked = [
            {
                "ean": it.get("ean"),
                "product_code": it.get("product_code"),
                "fire_product_id": it.get("fire_product_id"),
            }
            for it in check.get("items", [])
            if it.get("price_status") == "no_price_in_fire"
        ]

        repo.set_sem_preco_ack(import_id, by_email=user.email, items=items_acked)
        repo.append_audit(
            import_id,
            "sem_preco_acknowledged",
            {"user_email": user.email, "user_id": user.id, "items": items_acked},
        )
        metrics.price_check_acks_total.inc()

    fresh = repo.get_import(import_id)
    return JSONResponse({
        "entry_id": import_id,
        "ack_by": fresh["sem_preco_ack_by"],
        "ack_at": fresh["sem_preco_ack_at"],
        "items_acked": items_acked,
    })
```

- [ ] **Step 4: Adicionar `price_check_acks_total` em metrics (placeholder mínimo)**

Em [app/observability/metrics.py](app/observability/metrics.py), adicionar no fim (antes de `def update_outbox_metrics`):

```python
price_check_acks_total: Counter = Counter(
    "portal_price_check_acks_total",
    "Total de ACKs do operador para itens sem preço cadastrado no Fire",
)

price_check_blocks_total: Counter = Counter(
    "portal_price_check_blocks_total",
    "Total de envios/exports bloqueados por validação de preço",
    labelnames=("reason",),  # price_mismatch | missing_order_price | no_price_unacked
)
```

(Definimos `price_check_blocks_total` agora também — vai ser usado nas tasks 7 e 8.)

- [ ] **Step 5: Rodar testes do endpoint**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k ack_sem_preco
```

Expected: 3 PASS.

- [ ] **Step 6: Commit**

```bash
git add app/web/server.py app/observability/metrics.py tests/test_web_server.py
git commit -m "feat(web): POST /api/imported/{id}/ack-sem-preco + Prom counters

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Propagar ack no preview payload (preview novo + rehydrate)

**Files:**
- Modify: `app/web/server.py:182-244` (`_build_preview_payload` — assinatura + payload)
- Modify: `app/web/server.py:2138-2165` (`rehydrate_preview` — passar entry pro builder)

- [ ] **Step 1: Escrever teste falhando — payload do rehydrate inclui ack**

```python
def test_rehydrate_preview_surfaces_sem_preco_ack(monkeypatch):
    from app.persistence import repo
    import uuid
    from datetime import datetime
    entry_id = str(uuid.uuid4())
    repo.insert_import({
        "id": entry_id,
        "source_filename": "x.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": "REHY-ACK",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": "REHY-ACK"},
            "items": [{"description": "x", "quantity": 1.0}],
            "source_file": "",
        },
    })
    repo.set_sem_preco_ack(entry_id, by_email="op@example.com",
                           items=[{"ean": "7891", "product_code": None, "fire_product_id": 1}])

    r = client.get(f"/api/imported/{entry_id}/preview")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sem_preco_ack"]["by"] == "op@example.com"
    assert body["sem_preco_ack"]["items"][0]["ean"] == "7891"
```

- [ ] **Step 2: Rodar — falha**

```bash
.venv/bin/pytest tests/test_web_server.py::test_rehydrate_preview_surfaces_sem_preco_ack -v
```

Expected: FAIL — `sem_preco_ack` ausente no payload.

- [ ] **Step 3: Atualizar `rehydrate_preview` para emitir o campo**

Em [app/web/server.py:2156-2165](app/web/server.py#L2156), depois do bloco `cliente_override`, adicionar:

```python
    payload["sem_preco_ack"] = (
        {
            "by": entry.get("sem_preco_ack_by"),
            "at": entry.get("sem_preco_ack_at"),
            "items": entry.get("sem_preco_ack_items") or [],
        }
        if entry.get("sem_preco_ack_by")
        else None
    )
    return JSONResponse(payload)
```

- [ ] **Step 4: Rodar — passa**

```bash
.venv/bin/pytest tests/test_web_server.py::test_rehydrate_preview_surfaces_sem_preco_ack -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "feat(web): expose sem_preco_ack on rehydrate_preview payload

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Guard server em `_send_one_to_fire`

**Files:**
- Modify: `app/web/server.py:1485-1620` (bloco do `_send_one_to_fire`)
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Escrever testes falhando**

```python
def _seed_parsed_order(entry_id, *, items=None, snapshot_items=None):
    """Helper: cria entry parsed pronto para send-to-fire."""
    from app.persistence import repo
    from datetime import datetime
    repo.insert_import({
        "id": entry_id,
        "source_filename": "p.pdf",
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "order_number": f"GUARD-{entry_id[:4]}",
        "customer": "ACME",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": {
            "header": {"order_number": f"GUARD-{entry_id[:4]}", "customer_name": "ACME"},
            "items": snapshot_items or [{"description": "x", "quantity": 1.0,
                                          "ean": "7891", "unit_price": 89.90}],
            "source_file": "",
        },
    })


def test_send_to_fire_blocked_by_price_mismatch(monkeypatch):
    from app.erp import product_check as pc_mod
    from app.persistence import repo
    from app.exporters import firebird_exporter as fb_mod
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "mismatch",
                   "unit_price_order": 89.90, "fire_preco_venda": 90.00}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)

    called = {"export": False}
    def _no_export(self, order, *, override_client_id=None):
        called["export"] = True
        raise AssertionError("FirebirdExporter.export não pode ser chamado")
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _no_export)

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 409
    assert "preço" in r.json()["detail"].lower() or "price" in r.json()["detail"].lower()
    assert called["export"] is False

    audits = [a["event_type"] for a in repo.list_audit(entry_id)]
    assert "send_to_fire_blocked" in audits


def test_send_to_fire_blocked_by_no_price_unacked(monkeypatch):
    from app.erp import product_check as pc_mod
    from app.exporters import firebird_exporter as fb_mod
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "no_price_in_fire"}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("não pode chamar")))

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 409


def test_send_to_fire_passes_with_ack(monkeypatch):
    from app.erp import product_check as pc_mod
    from app.exporters import firebird_exporter as fb_mod
    from app.persistence import repo
    from app import config as app_config
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)
    repo.set_sem_preco_ack(entry_id, by_email="op@example.com",
                           items=[{"ean": "7891", "product_code": None}])

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "no_price_in_fire"}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export",
                        lambda self, order, **kw: fb_mod.FirebirdExportResult(
                            order_number=order.header.order_number,
                            items_inserted=1, fire_codigo=999,
                        ))
    monkeypatch.setattr(app_config, "load",
                        lambda: {"watch_dir": ".", "output_dir": ".", "export_mode": "db"})

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 200, r.text
    assert r.json()["fire_codigo"] == 999


def test_send_to_fire_passes_when_check_unavailable(monkeypatch):
    """Fire offline → check best-effort, não bloqueia."""
    from app.erp import product_check as pc_mod
    from app.exporters import firebird_exporter as fb_mod
    from app import config as app_config
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)

    monkeypatch.setattr(pc_mod, "check_order",
                        lambda order, **kw: {"available": False, "items": [], "summary": {}})
    monkeypatch.setattr(fb_mod.FirebirdExporter, "export",
                        lambda self, order, **kw: fb_mod.FirebirdExportResult(
                            order_number=order.header.order_number,
                            items_inserted=1, fire_codigo=111,
                        ))
    monkeypatch.setattr(app_config, "load",
                        lambda: {"watch_dir": ".", "output_dir": ".", "export_mode": "db"})

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert r.status_code == 200
    assert r.json()["fire_codigo"] == 111
```

- [ ] **Step 2: Rodar — falham**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k "send_to_fire_blocked or send_to_fire_passes"
```

Expected: 4 FAIL (sem guard).

- [ ] **Step 3: Implementar guard em `_send_one_to_fire`**

Em [app/web/server.py:1517-1540](app/web/server.py#L1517), depois do bloco `try: order = Order.model_validate(snapshot)` e ANTES do `with with_trace_id(...)`, adicionar:

```python
    # Defesa em profundidade: re-checar preço contra o Fire antes de enviar.
    # Se Fire offline, segue (best-effort).
    from app.erp.product_check import check_order, is_blocking
    from app.observability import metrics

    check = check_order(order, env=request_env)
    ack_items = entry.get("sem_preco_ack_items") or []
    blocked, block_detail = is_blocking(check, ack_items=ack_items)
    if blocked:
        repo.append_audit(import_id, "send_to_fire_blocked", block_detail)
        if block_detail["items_mismatch"]:
            metrics.price_check_blocks_total.labels(reason="price_mismatch").inc()
        if block_detail["items_no_order_price"]:
            metrics.price_check_blocks_total.labels(reason="missing_order_price").inc()
        if block_detail["items_no_price_unacked"]:
            metrics.price_check_blocks_total.labels(reason="no_price_unacked").inc()

        parts = []
        if block_detail["items_mismatch"]:
            parts.append(f"{len(block_detail['items_mismatch'])} item(ns) com preço divergente do Fire")
        if block_detail["items_no_order_price"]:
            parts.append(f"{len(block_detail['items_no_order_price'])} item(ns) sem preço no pedido")
        if block_detail["items_no_price_unacked"]:
            parts.append(f"{len(block_detail['items_no_price_unacked'])} item(ns) sem preço no cadastro do Fire (sem confirmação)")
        return _FireSendOutcome(
            False,
            reason="price_check_failed",
            http_status=409,
            detail="Pedido bloqueado: " + "; ".join(parts) + ".",
        )
```

- [ ] **Step 4: Rodar — passam**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k "send_to_fire"
```

Expected: todos PASS (incluindo o já existente `test_send_to_fire_inserts_when_success` — confirma que não regrediu; pode precisar mockar `check_order` lá tb, ver Step 5).

- [ ] **Step 5: Se `test_send_to_fire_inserts_when_success` quebrou**

O teste antigo não mocka `check_order` — vai chamar Firebird real. Adicionar mock no início dele:

```python
def test_send_to_fire_inserts_when_success(monkeypatch):
    from app.erp import product_check as pc_mod
    monkeypatch.setattr(pc_mod, "check_order",
                        lambda order, **kw: {"available": False, "items": [], "summary": {}})
    # ... resto do teste como está
```

(O `available=False` faz `is_blocking` devolver `(False, ...)` e o envio segue.)

Mesma coisa para `test_batch_send_to_fire_mixed_outcomes`.

- [ ] **Step 6: Rodar suite web inteira pra confirmar que nada regrediu**

```bash
.venv/bin/pytest tests/test_web_server.py -v
```

Expected: tudo PASS.

- [ ] **Step 7: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "feat(web): guard _send_one_to_fire on price_status (defense in depth)

Re-roda check_order e bloqueia 409 em mismatch / no_order_price /
no_price_in_fire sem ack. Fire offline = segue (best-effort). Audit
+ Prom counters em cada bloqueio.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Guard server em `_export_one_xlsx`

**Files:**
- Modify: `app/web/server.py:1645-1687` (`_export_one_xlsx`)
- Test: `tests/test_web_server.py`

- [ ] **Step 1: Escrever testes falhando**

```python
def test_export_xlsx_blocked_by_price_mismatch(monkeypatch):
    from app.erp import product_check as pc_mod
    from app.persistence import repo
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "mismatch",
                   "unit_price_order": 89.90, "fire_preco_venda": 90.00}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")
    assert r.status_code == 409

    audits = [a["event_type"] for a in repo.list_audit(entry_id)]
    assert "xlsx_export_blocked" in audits


def test_export_xlsx_passes_with_ack(monkeypatch, tmp_path):
    from app.erp import product_check as pc_mod
    from app.persistence import repo
    from app import config as app_config
    import uuid

    entry_id = str(uuid.uuid4())
    _seed_parsed_order(entry_id)
    repo.set_sem_preco_ack(entry_id, by_email="op@example.com",
                           items=[{"ean": "7891", "product_code": None}])

    fake_check = {
        "available": True,
        "items": [{"ean": "7891", "product_code": None,
                   "price_status": "no_price_in_fire"}],
        "summary": {},
    }
    monkeypatch.setattr(pc_mod, "check_order", lambda order, **kw: fake_check)
    monkeypatch.setattr(app_config, "load",
                        lambda: {"watch_dir": str(tmp_path), "output_dir": str(tmp_path),
                                 "export_mode": "xlsx"})

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")
    assert r.status_code == 200, r.text
```

- [ ] **Step 2: Rodar — falham**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k "export_xlsx"
```

Expected: 2 FAIL.

- [ ] **Step 3: Implementar guard em `_export_one_xlsx`**

Em [app/web/server.py:1671](app/web/server.py#L1671), antes do `with with_trace_id(...)`, adicionar:

```python
    # Defesa em profundidade: re-checar preço — mesma lógica de _send_one_to_fire.
    from app.erp.product_check import check_order, is_blocking
    from app.observability import metrics

    check = check_order(order)  # _export_one_xlsx não tem request_env
    ack_items = entry.get("sem_preco_ack_items") or []
    blocked, block_detail = is_blocking(check, ack_items=ack_items)
    if blocked:
        repo.append_audit(import_id, "xlsx_export_blocked", block_detail)
        if block_detail["items_mismatch"]:
            metrics.price_check_blocks_total.labels(reason="price_mismatch").inc()
        if block_detail["items_no_order_price"]:
            metrics.price_check_blocks_total.labels(reason="missing_order_price").inc()
        if block_detail["items_no_price_unacked"]:
            metrics.price_check_blocks_total.labels(reason="no_price_unacked").inc()
        parts = []
        if block_detail["items_mismatch"]:
            parts.append(f"{len(block_detail['items_mismatch'])} item(ns) com preço divergente do Fire")
        if block_detail["items_no_order_price"]:
            parts.append(f"{len(block_detail['items_no_order_price'])} item(ns) sem preço no pedido")
        if block_detail["items_no_price_unacked"]:
            parts.append(f"{len(block_detail['items_no_price_unacked'])} item(ns) sem preço no cadastro do Fire (sem confirmação)")
        return _XlsxExportOutcome(
            False,
            reason="price_check_failed",
            http_status=409,
            detail="Pedido bloqueado: " + "; ".join(parts) + ".",
        )
```

> Observação: o caminho do XLSX hoje não recebe `request_env` no chamador `_export_one_xlsx(import_id, cfg)`. Isso significa que o `check_order(order)` aqui usa env vars `FB_*`. Em produção multi-ambiente isso é uma limitação a corrigir em outra task — para esta feature, manter o comportamento atual e documentar como follow-up no docs/ai/modules/web.md.

- [ ] **Step 4: Rodar — passam**

```bash
.venv/bin/pytest tests/test_web_server.py -v -k "export_xlsx"
```

Expected: PASS.

- [ ] **Step 5: Suite web inteira**

```bash
.venv/bin/pytest tests/test_web_server.py -v
```

Expected: tudo PASS. Se outros testes de `export-xlsx` quebraram (similar ao Step 5 da Task 7), mockar `check_order` neles para devolver `available=False`.

- [ ] **Step 6: Commit**

```bash
git add app/web/server.py tests/test_web_server.py
git commit -m "feat(web): guard _export_one_xlsx on price_status

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: UI — coluna "Fire" com price_status

**Files:**
- Modify: `app/web/static/index.html:1700-1800` (renderização da tabela do preview)

- [ ] **Step 1: Atualizar mapeamento de match para incluir todos os items do check**

Em [app/web/static/index.html:1703-1711](app/web/static/index.html#L1703), substituir as 8 linhas (`const matchByEan ... }`) por:

```javascript
  const checkByEan = new Map();
  const checkByCode = new Map();
  if (check && check.items) {
    check.items.forEach(m => {
      if (m.ean) checkByEan.set(m.ean, m);
      if (m.product_code) checkByCode.set(m.product_code, m);
    });
  }
  function _checkOf(it) {
    return (it.ean && checkByEan.get(it.ean))
        || (it.product_code && checkByCode.get(it.product_code));
  }
```

- [ ] **Step 2: Substituir o cálculo da célula "Fire" no loop de itens**

Em [app/web/static/index.html:1775-1784](app/web/static/index.html#L1775), substituir as linhas que constroem `cell` por:

```javascript
        const m = _checkOf(it);
        let cell;
        if (!check) {
          cell = '<span style="color:var(--text-muted)">—</span>';
        } else if (!check.available) {
          cell = '<span style="color:var(--text-muted)" title="check indisponível">—</span>';
        } else if (!m || !m.match) {
          cell = '<span style="color:var(--error);font-weight:600" title="sem match no Fire">✗</span>';
        } else {
          const ps = m.price_status;
          const idTitle = `#${m.fire_product_id} · ${m.fire_description || ''} · via ${m.match_source}`;
          if (ps === 'match') {
            cell = `<span style="color:var(--success);font-weight:600" title="${esc(idTitle)}">✓</span>`;
          } else if (ps === 'mismatch') {
            const fp = m.fire_preco_venda != null ? fmtBRL(m.fire_preco_venda) : '?';
            cell = `<span style="color:var(--error);font-weight:600"
                     title="${esc(idTitle)} · cadastro Fire: ${esc(fp)}">✗ ${esc(fp)}</span>`;
          } else if (ps === 'no_price_in_fire') {
            cell = `<span style="color:var(--warn);font-weight:600"
                     title="${esc(idTitle)} · sem preço cadastrado no Fire">⚠ sem preço</span>`;
          } else if (ps === 'no_order_price') {
            cell = `<span style="color:var(--error);font-weight:600"
                     title="${esc(idTitle)} · pedido sem preço">✗ pedido sem preço</span>`;
          } else {
            cell = `<span style="color:var(--success);font-weight:600" title="${esc(idTitle)}">✓</span>`;
          }
        }
```

- [ ] **Step 3: Verificação manual**

```bash
TEST_AUTH_BYPASS=1 python ui.py
```

1. Abrir http://localhost:8000.
2. Importar um sample que esteja em `samples/` (qualquer um para começar).
3. Confirmar que o preview abre e a coluna "Fire" mostra ícones; abrir DevTools e injetar mocks no console se Fire não estiver configurado:
   ```js
   // simula 4 estados num preview já aberto:
   // (sem check real, ajuste pelo console se preciso só para inspecionar visual)
   ```
4. Sem Fire: deve mostrar "—" (cinza) — comportamento atual preservado.

- [ ] **Step 4: Commit**

```bash
git add app/web/static/index.html
git commit -m "feat(ui): coluna Fire renderiza price_status (mismatch/no_price/no_order_price)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: UI — banner reativo + estados de bloqueio/ack

**Files:**
- Modify: `app/web/static/index.html:1713-1745` (renderização do banner)

- [ ] **Step 1: Adicionar lógica de banner com novos estados**

Em [app/web/static/index.html:1713-1745](app/web/static/index.html#L1713), substituir o bloco `const banner = ... banner.innerHTML = ...` por:

```javascript
  const banner = document.getElementById('pvCheckBanner');
  if (banner && check) {
    const s = check.summary || {};
    const ps = s.price_summary || {};
    const ack = data.sem_preco_ack;  // {by, at, items} | null
    const ackedEans = new Set((ack && ack.items || []).map(i => i.ean).filter(Boolean));
    const ackedCodes = new Set((ack && ack.items || []).map(i => i.product_code).filter(Boolean));
    const noPriceItems = (check.items || []).filter(i => i.price_status === 'no_price_in_fire');
    const noPriceUnacked = noPriceItems.filter(
      i => !(i.ean && ackedEans.has(i.ean)) && !(i.product_code && ackedCodes.has(i.product_code))
    );

    let html = '';
    if (!check.available) {
      html = `<span style="color:var(--text-muted)">⚠ Validação Fire indisponível — preview sem checagem.</span>`;
    } else if ((ps.items_mismatch || 0) > 0) {
      html = `<span style="color:var(--error);font-weight:600">
        ✗ ${ps.items_mismatch} item(ns) com preço divergente do Fire — ajuste o cadastro ou a planilha e reimporte.
      </span>`;
    } else if ((ps.items_no_order_price || 0) > 0) {
      html = `<span style="color:var(--error);font-weight:600">
        ✗ ${ps.items_no_order_price} item(ns) sem preço no pedido — corrija a planilha e reimporte.
      </span>`;
    } else if (noPriceUnacked.length > 0) {
      html = `<span style="color:var(--warn);font-weight:600">
        ⚠ ${noPriceUnacked.length} item(ns) sem preço cadastrado no Fire.
      </span>
      <button class="btn" id="pvAckSemPrecoBtn"
              onclick="openAckSemPrecoModal('${esc(data.preview_id)}')">
        Confirmar e prosseguir
      </button>`;
    } else if (ack) {
      html = `<span style="color:var(--text-muted)">
        ✓ Confirmado por <strong>${esc(ack.by)}</strong> em ${esc(ack.at)}: ${ack.items.length} item(ns) sem preço serão importados sem validação.
      </span>`;
    } else {
      // Banner padrão de itens sem match (comportamento atual)
      html = (s.items_missing || 0) > 0
        ? `<span style="color:var(--warn)">⚠ ${s.items_matched}/${s.items_total} itens com match · ${s.items_missing} sem correspondência</span>`
        : `<span style="color:var(--success)">✓ ${s.items_matched}/${s.items_total} itens com match no Fire</span>`;
    }

    banner.innerHTML = html;
    banner.classList.remove('hidden');
  }
```

- [ ] **Step 2: Verificação manual**

```bash
TEST_AUTH_BYPASS=1 python ui.py
```

1. Caso Fire não configurado: banner mostra "Validação Fire indisponível".
2. Caso com Fire: importar um pedido onde há produto cadastrado com preço diferente — esperar banner vermelho com texto de divergência.

- [ ] **Step 3: Commit**

```bash
git add app/web/static/index.html
git commit -m "feat(ui): banner reativo a price_summary (mismatch/no_order/no_price/ack)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: UI — gating do botão primário + modal de ack

**Files:**
- Modify: `app/web/static/index.html` (`renderPreviewFooter` em ~1804, e adicionar funções no fim do script)

- [ ] **Step 1: Adicionar helper JS `_isBlockedByPriceCheck(data)` no script**

No bloco script principal de `index.html`, adicionar (perto das outras helpers, antes de `renderPreviewFooter`):

```javascript
function _isBlockedByPriceCheck(data) {
  const check = data.check;
  if (!check || !check.available) return false;
  const items = check.items || [];
  const ack = data.sem_preco_ack;
  const ackedEans = new Set((ack && ack.items || []).map(i => i.ean).filter(Boolean));
  const ackedCodes = new Set((ack && ack.items || []).map(i => i.product_code).filter(Boolean));
  for (const it of items) {
    if (it.price_status === 'mismatch') return true;
    if (it.price_status === 'no_order_price') return true;
    if (it.price_status === 'no_price_in_fire') {
      const covered = (it.ean && ackedEans.has(it.ean)) || (it.product_code && ackedCodes.has(it.product_code));
      if (!covered) return true;
    }
  }
  return false;
}

function _blockReasonText(data) {
  const check = data.check; if (!check) return '';
  const ps = check.summary && check.summary.price_summary || {};
  const ack = data.sem_preco_ack;
  const ackedEans = new Set((ack && ack.items || []).map(i => i.ean).filter(Boolean));
  const ackedCodes = new Set((ack && ack.items || []).map(i => i.product_code).filter(Boolean));
  const unacked = (check.items || []).filter(i =>
    i.price_status === 'no_price_in_fire'
    && !(i.ean && ackedEans.has(i.ean))
    && !(i.product_code && ackedCodes.has(i.product_code))
  ).length;
  if ((ps.items_mismatch || 0) > 0) return `${ps.items_mismatch} divergência(s) de preço — corrija antes de enviar`;
  if ((ps.items_no_order_price || 0) > 0) return `${ps.items_no_order_price} item(ns) sem preço no pedido`;
  if (unacked > 0) return `Confirme ${unacked} item(ns) sem preço cadastrado`;
  return '';
}
```

- [ ] **Step 2: Aplicar gating no botão primário em `renderPreviewFooter`**

Em [app/web/static/index.html:1819-1832](app/web/static/index.html#L1819), no bloco `else if (ps === 'parsed')`, substituir o `actions.innerHTML = ...` por:

```javascript
    const blocked = _isBlockedByPriceCheck(data);
    const reason = blocked ? _blockReasonText(data) : '';
    const disabledAttr = blocked ? 'disabled' : '';
    const titleAttr = blocked ? `title="${esc(reason)}"` : '';
    actions.innerHTML = `
      <button class="btn" onclick="cancelImport('${esc(data.preview_id)}')" style="color:var(--error)">Cancelar pedido</button>
      <button class="btn" onclick="closePreviewModal()">Fechar</button>
      <button class="btn btn-primary" id="pvCommitBtn" data-primary-btn ${disabledAttr} ${titleAttr}
              onclick="${action.fn}('${esc(data.preview_id)}')">
        ${action.label}
      </button>`;
```

- [ ] **Step 3: Adicionar `openAckSemPrecoModal` e `confirmAckSemPreco` (chamadas pelo banner)**

No fim do script, antes do fecha-`</script>`, adicionar:

```javascript
function openAckSemPrecoModal(previewId) {
  const data = currentPreviewData;  // injetado em refreshPreview
  if (!data) return;
  const items = (data.check && data.check.items || []).filter(i => i.price_status === 'no_price_in_fire');
  const list = items.map(i => {
    const desc = i.fire_description || '(sem descrição)';
    const ident = i.ean || i.product_code || '?';
    return `<li><code>${esc(ident)}</code> · ${esc(desc)}</li>`;
  }).join('');

  const modal = document.createElement('div');
  modal.className = 'modal-backdrop';
  modal.id = 'ackSemPrecoModal';
  modal.innerHTML = `
    <div class="modal" style="max-width:520px">
      <h3>Confirmar itens sem preço</h3>
      <p>Você está confirmando que <strong>${items.length} produto(s) sem preço cadastrado no Fire</strong>
         podem ser importados sem validação. Esta ação será registrada com seu email e horário.</p>
      <ul style="max-height:200px;overflow:auto;padding-left:20px">${list}</ul>
      <div style="display:flex;justify-content:flex-end;gap:8px;margin-top:16px">
        <button class="btn" onclick="document.getElementById('ackSemPrecoModal').remove()">Cancelar</button>
        <button class="btn btn-primary" onclick="confirmAckSemPreco('${esc(previewId)}')">Confirmar</button>
      </div>
    </div>`;
  document.body.appendChild(modal);
}

async function confirmAckSemPreco(previewId) {
  try {
    const r = await api(`/api/imported/${encodeURIComponent(previewId)}/ack-sem-preco`, {method: 'POST'});
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    document.getElementById('ackSemPrecoModal')?.remove();
    // Re-render do preview com ack já registrado
    await refreshPreview(previewId);
    if (window.appShell) window.appShell.showSuccess('Confirmação registrada.');
  } catch (e) {
    if (window.appShell) window.appShell.showError('Falha ao confirmar', null);
  }
}
```

> **Nota:** verifique antes se já existe `currentPreviewData` (variável que segura o último payload do preview). Se não, criar uma com escopo do script e atribuir em `refreshPreview` / `openPreviewModal`. Idem `refreshPreview`: pode chamar diretamente `GET /api/imported/{id}/preview` e reusar o renderizador. Se essas helpers não existirem com esses nomes, adapte mantendo a intenção.

- [ ] **Step 4: Verificação manual**

```bash
TEST_AUTH_BYPASS=1 python ui.py
```

1. Importar pedido com produto sem preço no Fire.
2. Confirmar banner amarelo + botão "Confirmar e prosseguir".
3. Clicar — modal aparece com lista correta.
4. Cancelar — modal fecha sem chamar API.
5. Confirmar — banner vira cinza, botão primário habilita, audit log aparece em `/imported/{id}` (ou via `repo.list_audit`).

- [ ] **Step 5: Commit**

```bash
git add app/web/static/index.html
git commit -m "feat(ui): gating do botão primário + modal de ack sem preço

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 12: Atualizar docs/ai/modules

**Files:**
- Modify: `docs/ai/modules/erp.md`
- Modify: `docs/ai/modules/web.md`
- Modify: `docs/ai/modules/persistence.md` (se relevante; senão pular)

- [ ] **Step 1: Atualizar `docs/ai/modules/erp.md`**

Adicionar seção depois de "Cliente override (CLIENT_NOT_FOUND recovery)":

```markdown
## Validação de preço (pedido vs Fire)

`product_check.check_order` agora popula por item:
- `unit_price_order`, `fire_preco_venda` (já existente), `price_diff`
- `price_status ∈ {match, mismatch, no_price_in_fire, no_order_price, no_product_match}` — comparação em centavos.

E `summary.price_summary` agrega contagens.

`product_check.is_blocking(check, ack_items=None)` decide se o estado bloqueia
envio. Bloqueia em `mismatch`, `no_order_price`, ou `no_price_in_fire` não
coberto por `ack_items`. `available=False` → não bloqueia (best-effort).

Os guards vivem em `_send_one_to_fire` e `_export_one_xlsx` (web). Audit
events: `send_to_fire_blocked`, `xlsx_export_blocked`, `sem_preco_acknowledged`.
Métricas: `portal_price_check_blocks_total{reason}`, `portal_price_check_acks_total`.
```

- [ ] **Step 2: Atualizar `docs/ai/modules/web.md`**

Adicionar à lista de rotas:

```markdown
- `POST /api/imported/{id}/ack-sem-preco` → operador confirma itens sem preço cadastrado no Fire (`require_user`).
  Body vazio. Pre: `portal_status='parsed'`. Re-roda check, persiste lista
  em `imports.sem_preco_ack_*`, audit `sem_preco_acknowledged`. 503 se Fire offline.
- Guards de preço em `_send_one_to_fire` / `_export_one_xlsx`: re-roda
  `check_order` + `is_blocking`; bloqueia 409 com audit `send_to_fire_blocked` /
  `xlsx_export_blocked` quando há mismatch / no_order_price / no_price_unacked.
  Fire offline = best-effort, segue.
```

E adicionar à seção "Armadilhas":

```markdown
- `_export_one_xlsx` re-roda `check_order` SEM passar `request_env` (caminho
  legado). Em deploy multi-ambiente isso usa env vars `FB_*`. Follow-up:
  passar env do request quando essa rota também adotar `getattr(request.state, "environment")`.
```

- [ ] **Step 3: Rodar suite completa antes do commit final**

```bash
.venv/bin/pytest tests/ -v
```

Expected: tudo PASS. Se algo regrediu nos testes que já existiam, voltar e ajustar (provavelmente os testes de send-to-fire / export-xlsx que precisam mockar `check_order` para não consultarem Firebird real — ver Task 7 Step 5 e Task 8 Step 5).

- [ ] **Step 4: Lint + format**

```bash
.venv/bin/ruff check app/ tests/
.venv/bin/ruff format app/ tests/
```

Expected: zero issues.

- [ ] **Step 5: Commit final**

```bash
git add docs/ai/modules/erp.md docs/ai/modules/web.md
git commit -m "docs(ai): document price validation in erp + web modules

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```
