# De-para de produto + Latência — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dar ao Importador memória de match de produto por cliente (a referência do varejista que não casa vira uma decisão feita uma vez e lembrada para sempre) e cortar a latência percebida do preview/export.

**Architecture:** Três frentes. (1) Latência: `check_order` batelado (2N+1 → ~4 queries) e push pro Flow movido do request path para o outbox. (2) De-para: tabela nova `produto_depara` (por ambiente, chaveada por CNPJ do cliente), 3º degrau no match, rotas de busca/vínculo/undo espelhando o padrão de `override-cliente`. (3) Assistência: ranking dos candidatos no picker (sugere, nunca aplica) + enriquecimento do XLS com a identidade do Fire.

**Tech Stack:** Python 3.11+, FastAPI, pydantic v2, SQLite (por-ambiente via `app/persistence/router.py`), Firebird (firebird-driver), openpyxl, vanilla JS (`index.html`), pytest.

## Global Constraints

- **Python 3.11+** — union `X | Y` e `match` liberados.
- **Contrato de `check_order` inalterado** — os 5 callsites (`app/web/server.py:1397,1445,1619,1826,2369`) não podem ser tocados; só muda a implementação interna e a adição do `match_source='depara'`.
- **De-para chaveado por `cliente_cnpj` (digits-only), nunca global** — evita colisão da mesma referência entre varejistas.
- **`_norm_key` idêntica na gravação e na leitura** — chave normalizada divergente = vínculo fantasma.
- **Ranking sugere, nunca aplica** — gravação sempre por clique explícito.
- **Best-effort no push ao Flow** — nunca derruba o fluxo; falha vira outbox/retry.
- **Firebird via IN (...)**: chunk de **200** valores por statement; dedup antes.
- **Reusar helpers existentes**: `_cnpj_digits` (product_check), `db.connect()` (ambiente ativo), padrão `override-cliente` para rotas, `_make_fb_ctx` (mock de teste do product_check).
- **Idempotência do outbox**: `idempotency_key = f"send-{import_id}"`; `OutboxDuplicateError` é no-op.
- Lint/format antes de cada commit: `ruff check app/ tests/ && ruff format app/ tests/`.

---

## File Structure

**Criar:**
- `app/persistence/produto_depara_repo.py` — CRUD do de-para (SQLite por ambiente) + `_norm_key`.
- `app/erp/depara_apply.py` — aplica o de-para no `Order` antes do XLS (mutação transiente).
- `app/erp/product_ranking.py` — heurística de ranking sobre `catalogo_fire`.
- `tests/test_produto_depara.py`
- `tests/test_depara_apply.py`
- `tests/test_product_ranking.py`

**Modificar:**
- `app/persistence/schema_env.py` — tabela `produto_depara` + índice.
- `app/erp/queries.py` — `FIND_PRODUCT_BY_SEQ` + builders de IN (eans/codes/seqs).
- `app/erp/product_check.py` — batelamento + 3º degrau de-para.
- `app/integrations/flowpcp/exporter.py` — enqueue-only (sem HTTP inline).
- `app/integrations/flowpcp/hook.py` — não constrói mais client no request path.
- `app/web/server.py` — 3 rotas novas + wire do `depara_apply` no `_export_one_xlsx`.
- `app/web/static/index.html` — selo "vínculo", picker de produto, progresso no botão.
- `tests/test_product_check.py` — mock atualizado p/ batelamento + degrau de-para.
- `tests/test_flowpcp_hook.py`, `tests/test_flowpcp_exporter.py` — enqueue em vez de HTTP.
- `tests/test_web_server.py` — rotas novas + push via outbox.
- `docs/ai/modules/erp.md`, `docs/ai/modules/web.md`, `docs/ai/modules/exporters.md` — seções afetadas.

---

# FASE A — Latência (sem dependência de produto)

### Task 1: `check_order` batelado (2N+1 → ~4 queries)

**Files:**
- Modify: `app/erp/queries.py`
- Modify: `app/erp/product_check.py:112-190` (corpo do `with open_conn()`)
- Test: `tests/test_product_check.py`

**Interfaces:**
- Consumes: `queries.FIND_CLIENT_BY_CNPJ`, `queries.FIND_PRODUCT_BY_EAN`/`FIND_PRODUCT_BY_CODE` (referência de colunas).
- Produces: `check_order(order, *, env=None) -> dict` — **mesmo shape de saída de hoje** (campos por item e `summary` idênticos). Muda só a contagem de queries e a implementação.

- [ ] **Step 1: Escrever o teste que conta execuções de cursor**

Adicionar em `tests/test_product_check.py`. O mock hoje (`_make_fb_ctx`) responde `fetchone`; o batelamento usa `fetchall`. Adicionar um helper novo que programa `fetchall` por tipo de query e conta `execute`:

```python
def _make_fb_ctx_batched(*, client_row=None, ean_rows=None, code_rows=None, seq_rows=None):
    """Cursor fake para o check batelado.

    ean_rows/code_rows/seq_rows: listas de tuplas já no formato SELECT
    (key primeiro). client_row: tupla (CODIGO, RAZAO) ou None.
    Conta execute() em cur.execute.call_count.
    """
    cur = MagicMock()
    next_fetchall = []

    def execute_side_effect(sql, params=None):
        nonlocal next_fetchall
        if "FROM CADASTRO" in sql:
            next_fetchall = []  # cliente usa fetchone
        elif "CODIGO_EAN13 IN" in sql:
            next_fetchall = list(ean_rows or [])
        elif "CODPROD_ALTERN" in sql and " IN " in sql:
            next_fetchall = list(code_rows or [])
        elif "SEQ IN" in sql:
            next_fetchall = list(seq_rows or [])
        else:
            next_fetchall = []

    cur.execute.side_effect = execute_side_effect
    cur.fetchone.side_effect = lambda: client_row
    cur.fetchall.side_effect = lambda: next_fetchall

    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__.return_value = conn
    ctx.__exit__.return_value = False
    return ctx, cur


@patch("app.erp.product_check.FirebirdConnection")
def test_check_order_batches_queries(mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[("7891", 10, "TENIS A", 89.90), ("7892", 11, "TENIS B", 50.0)],
        code_rows=[("ABC", 12, "TENIS C", 30.0)],
    )
    mock_fb.return_value.connect.return_value = ctx

    order = _order([
        {"ean": "7891", "unit_price": 89.90},
        {"ean": "7892", "unit_price": 50.0},
        {"product_code": "ABC", "unit_price": 30.0},
        {"ean": "9999", "unit_price": 1.0},  # sem match
    ])
    report = product_check.check_order(order)

    # 1 cliente + 1 eans + 1 codes = 3 execute (sem depara nesta fase)
    assert cur.execute.call_count <= 4
    assert report["summary"]["items_matched"] == 3
    assert report["items"][0]["match_source"] == "ean"
    assert report["items"][2]["match_source"] == "codprod_altern"
    assert report["items"][3]["match"] is False
```

- [ ] **Step 2: Rodar o teste — deve FALHAR**

Run: `.venv/bin/pytest tests/test_product_check.py::test_check_order_batches_queries -v`
Expected: FAIL (hoje o check chama `fetchone` por item; `cur.execute.call_count` será ~5 e `fetchall` não é usado).

- [ ] **Step 3: Adicionar queries bateladas em `app/erp/queries.py`**

Depois de `FIND_PRODUCT_BY_CODE` (linha ~152), adicionar:

```python
# Product lookup by SEQ (usado na resolução de de-para).
FIND_PRODUCT_BY_SEQ = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS
    WHERE SEQ = ?
    ROWS 1
"""


def find_products_by_eans_sql(n: int) -> str:
    """SELECT batelado por EAN. Retorna (CODIGO_EAN13, SEQ, DESCRICAO, PRECO_VENDA)."""
    placeholders = ", ".join(["?"] * n)
    return (
        "SELECT CODIGO_EAN13, SEQ, DESCRICAO, PRECO_VENDA "
        f"FROM PRODUTOS WHERE CODIGO_EAN13 IN ({placeholders})"
    )


def find_products_by_codes_sql(n: int) -> str:
    """SELECT batelado por CODPROD_ALTERN. Retorna (CODPROD_ALTERN_TRIM, SEQ, DESCRICAO, PRECO_VENDA)."""
    placeholders = ", ".join(["?"] * n)
    return (
        "SELECT TRIM(CODPROD_ALTERN), SEQ, DESCRICAO, PRECO_VENDA "
        f"FROM PRODUTOS WHERE TRIM(CODPROD_ALTERN) IN ({placeholders})"
    )


def find_products_by_seqs_sql(n: int) -> str:
    """SELECT batelado por SEQ. Retorna (SEQ, DESCRICAO, PRECO_VENDA)."""
    placeholders = ", ".join(["?"] * n)
    return f"SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS WHERE SEQ IN ({placeholders})"
```

- [ ] **Step 4: Reescrever o corpo batelado em `app/erp/product_check.py`**

Adicionar helper de chunk no topo do módulo (após imports):

```python
def _chunked(values: list, size: int = 200):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _fetch_map_by_key(cur, sql_builder, values: list) -> dict:
    """Roda o SELECT batelado (chunk de 200) e devolve {key: (seq, desc, preco)}.

    O SELECT tem a chave como 1ª coluna. Valores deduplicados pelo chamador.
    """
    out: dict = {}
    for chunk in _chunked(values):
        cur.execute(sql_builder(len(chunk)), tuple(chunk))
        for row in cur.fetchall():
            key = row[0]
            out[key] = (row[1], row[2], row[3])
    return out
```

Substituir o loop `for it in order.items:` (linhas ~134-179) pela versão batelada.
O bloco `try: with open_conn() as conn:` passa a ser:

```python
    try:
        with open_conn() as conn:
            cur = conn.cursor()

            # Client lookup (inalterado)
            digits = _cnpj_digits(order.header.customer_cnpj)
            client_id: int | None = None
            razao: str | None = None
            if digits:
                cur.execute(queries.FIND_CLIENT_BY_CNPJ, (digits,))
                row = cur.fetchone()
                if row:
                    client_id = row[0]
                    razao = row[1]

            # Coleta chaves (dedup) e resolve em lote
            eans = list({it.ean for it in order.items if it.ean})
            codes = list({it.product_code for it in order.items if it.product_code})
            ean_map = _fetch_map_by_key(cur, queries.find_products_by_eans_sql, eans) if eans else {}
            code_map = (
                _fetch_map_by_key(cur, queries.find_products_by_codes_sql, codes) if codes else {}
            )

            items_report: list[dict] = []
            matched = 0
            price_match = price_mismatch = price_no_price_in_fire = price_no_order_price = 0

            for it in order.items:
                entry = _empty_item_result(it.product_code, it.ean, it.unit_price)
                hit = None
                source = None
                if it.ean and it.ean in ean_map:
                    hit, source = ean_map[it.ean], "ean"
                elif it.product_code and it.product_code in code_map:
                    hit, source = code_map[it.product_code], "codprod_altern"

                if hit is not None:
                    seq, desc, preco = hit
                    entry.update(
                        {
                            "match": True,
                            "match_source": source,
                            "fire_product_id": seq,
                            "fire_description": desc,
                            "fire_preco_venda": float(preco) if preco is not None else None,
                        }
                    )
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
                items_report.append(entry)

            cur.close()
```

O `return {...}` final (linhas ~192-214) fica **inalterado** (usa `matched`, os contadores e `items_report`).

- [ ] **Step 5: Rodar o teste — deve PASSAR**

Run: `.venv/bin/pytest tests/test_product_check.py -v`
Expected: PASS. Os testes antigos (`test_price_status_match_exact` etc.) usam `_make_fb_ctx` com `fetchone` por PRODUTOS — **vão quebrar** com o batelamento. Atualizá-los para usar `_make_fb_ctx_batched` (mesmo mapeamento, agora via `fetchall`). Manter as asserções de `price_status` idênticas.

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/erp/ tests/test_product_check.py && ruff format app/erp/ tests/test_product_check.py
git add app/erp/queries.py app/erp/product_check.py tests/test_product_check.py
git commit -m "perf(erp): batela check_order (2N+1 -> ~4 queries), contrato inalterado"
```

---

### Task 2: Push pro Flow via outbox (tira a cauda de 30s)

**Files:**
- Modify: `app/integrations/flowpcp/exporter.py`
- Modify: `app/integrations/flowpcp/hook.py:17-40`
- Test: `tests/test_flowpcp_exporter.py`, `tests/test_flowpcp_hook.py`

**Interfaces:**
- Consumes: `outbox_repo.enqueue(...)`, `outbox_repo.OutboxDuplicateError`, `FLOWPCP_TARGET_NAME`, `RECEBIMENTO_PATH`, `build_recebimento_payload`.
- Produces: `FlowPCPExporter(*, tenant_id).enqueue(order, *, import_id) -> bool`; `push_new_order(order, *, import_id, slug) -> bool` (assinatura preservada).

- [ ] **Step 1: Reescrever o teste do exporter (enqueue, não HTTP)**

Substituir `tests/test_flowpcp_exporter.py` pelo comportamento novo:

```python
from __future__ import annotations

from unittest.mock import patch

from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import outbox_repo

TENANT = "uuid-mm"


def _order() -> Order:
    return Order(
        header=OrderHeader(order_number="AW097", customer_name="MM", customer_cnpj="123"),
        items=[OrderItem(description="meia", quantity=10)],
    )


@patch("app.integrations.flowpcp.exporter.outbox_repo.enqueue")
def test_export_enqueues_to_outbox(mock_enqueue):
    sent = FlowPCPExporter(tenant_id=TENANT).enqueue(_order(), import_id="imp-1")
    assert sent is True
    _, kwargs = mock_enqueue.call_args
    assert kwargs["target"] == FLOWPCP_TARGET_NAME
    assert kwargs["endpoint"] == RECEBIMENTO_PATH
    assert kwargs["idempotency_key"] == "send-imp-1"


@patch("app.integrations.flowpcp.exporter.outbox_repo.enqueue",
       side_effect=outbox_repo.OutboxDuplicateError("dup"))
def test_export_duplicate_is_noop(mock_enqueue):
    # já enfileirado (re-export) — não é erro
    assert FlowPCPExporter(tenant_id=TENANT).enqueue(_order(), import_id="imp-1") is True
```

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_flowpcp_exporter.py -v`
Expected: FAIL (hoje `FlowPCPExporter.__init__` exige `client` e o método é `export` com HTTP inline).

- [ ] **Step 3: Reescrever `app/integrations/flowpcp/exporter.py`**

```python
from __future__ import annotations

from app.integrations.flowpcp.client import FLOWPCP_TARGET_NAME, RECEBIMENTO_PATH
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order
from app.persistence import outbox_repo
from app.utils.logger import logger


class FlowPCPExporter:
    """Enfileira o pedido no outbox (target=flowpcp). O worker (drain_outbox)
    entrega e faz retry. Sem HTTP no request path — a UI não espera o Flow."""

    def __init__(self, *, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def enqueue(self, order: Order, *, import_id: str) -> bool:
        req = build_recebimento_payload(
            import_id=import_id, order=order, tenant_id=self._tenant_id
        )
        try:
            outbox_repo.enqueue(
                import_id=import_id,
                target=FLOWPCP_TARGET_NAME,
                endpoint=RECEBIMENTO_PATH,
                payload=req.model_dump(by_alias=True),
                idempotency_key=f"send-{import_id}",
            )
            return True
        except outbox_repo.OutboxDuplicateError:
            logger.info(f"flowpcp já enfileirado (import={import_id}) — no-op")
            return True
```

- [ ] **Step 4: Atualizar `app/integrations/flowpcp/hook.py`**

```python
from __future__ import annotations

from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.models.order import Order
from app.utils.logger import logger


def push_new_order(order: Order, *, import_id: str, slug: str) -> bool:
    """Enfileira o pedido pro FlowPCP (outbox). Retorna True se enfileirado;
    False se o ambiente não tem FlowPCP habilitado ou se o enqueue falhou.
    Best-effort: nunca levanta — o send-to-fire já teve sucesso."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None:
        return False
    try:
        return FlowPCPExporter(tenant_id=cfg.tenant_id).enqueue(order, import_id=import_id)
    except Exception as exc:  # noqa: BLE001 — best-effort; nunca derruba o send-to-fire
        logger.warning(f"flowpcp enqueue falhou (import={import_id} slug={slug}): {exc}")
        return False
```

- [ ] **Step 5: Atualizar `tests/test_flowpcp_hook.py`**

`test_push_skips_when_env_not_flowpcp` já passa (cfg None). Trocar `test_push_exports_when_enabled` e `test_push_swallows_errors_best_effort` para mockar `FlowPCPExporter` sem `client`:

```python
def test_push_enqueues_when_enabled(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    fake_exporter = MagicMock()
    fake_exporter.enqueue.return_value = True
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *, tenant_id: fake_exporter)

    assert hook.push_new_order(_order(), import_id="imp-1", slug="mm") is True
    _, kwargs = fake_exporter.enqueue.call_args
    assert kwargs["import_id"] == "imp-1"


def test_push_swallows_errors_best_effort(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    boom = MagicMock()
    boom.enqueue.side_effect = RuntimeError("kaboom")
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *, tenant_id: boom)
    assert hook.push_new_order(_order(), import_id="imp-1", slug="mm") is False
```

- [ ] **Step 6: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_flowpcp_exporter.py tests/test_flowpcp_hook.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
ruff check app/integrations/flowpcp/ tests/test_flowpcp_*.py && ruff format app/integrations/flowpcp/ tests/test_flowpcp_*.py
git add app/integrations/flowpcp/exporter.py app/integrations/flowpcp/hook.py tests/test_flowpcp_exporter.py tests/test_flowpcp_hook.py
git commit -m "perf(flowpcp): push de pedido vira enqueue no outbox (tira HTTP do request path)"
```

---

### Task 3: Progresso real no botão

**Files:**
- Modify: `app/web/static/index.html`

**Interfaces:**
- Consumes: `#pvCommitBtn`, `#batchSendBtn`, `cfg.exportMode` (já existentes).
- Produces: nada consumido por outras tasks (UI-only).

- [ ] **Step 1: Localizar o handler do botão de commit**

Run: `grep -n "pvCommitBtn\|batchSendBtn\|export-xlsx" app/web/static/index.html`
Ler o handler que dispara `POST /api/imported/{id}/export-xlsx`.

- [ ] **Step 2: Trocar o texto do botão por etapa**

No handler, antes do `fetch`, setar `btn.disabled = true; btn.dataset.prev = btn.textContent; btn.textContent = 'Gerando XLS…';` e, no `finally`, restaurar `btn.textContent = btn.dataset.prev; btn.disabled = false;`. Para o lote (`batchSendBtn`), usar `Enviando… (${done}/${total})` atualizado a cada resposta.

- [ ] **Step 3: Verificação manual**

Run: `python ui.py` → abrir `http://localhost:8000` → subir um sample → "Gerar XLS". Confirmar que o botão mostra "Gerando XLS…", fica desabilitado e restaura ao terminar. (Sem teste automatizado: mudança puramente visual.)

- [ ] **Step 4: Commit**

```bash
git add app/web/static/index.html
git commit -m "feat(web): feedback de progresso no botao de gerar XLS"
```

---

# FASE B — De-para (memória por cliente)

### Task 4: Schema + repo `produto_depara`

**Files:**
- Modify: `app/persistence/schema_env.py:110-131` (junto de `catalogo_fire`), `:133+` (índices)
- Create: `app/persistence/produto_depara_repo.py`
- Test: `tests/test_produto_depara.py`

**Interfaces:**
- Consumes: `sqlite3.Connection` (recebida aberta, padrão de `catalogo_fire_repo`).
- Produces:
  - `produto_depara_repo._norm_key(tipo: str, valor: str) -> str`
  - `produto_depara_repo._norm_cnpj(cnpj: str | None) -> str`
  - `produto_depara_repo.upsert(conn, *, cliente_cnpj, chave_tipo, chave_valor, fire_produto_id, fire_codigo, fire_ean, fire_nome, criado_em, criado_por) -> None`
  - `produto_depara_repo.lookup(conn, cliente_cnpj: str, *, codigos: list[str], eans: list[str]) -> dict[tuple[str, str], dict]` — chave `(chave_tipo, chave_valor_normalizada)`.
  - `produto_depara_repo.delete(conn, id: int) -> None`
  - `produto_depara_repo.list_for_client(conn, cliente_cnpj: str) -> list[dict]`

- [ ] **Step 1: Escrever `tests/test_produto_depara.py`**

```python
"""De-para de produto por cliente (produto_depara, db do ambiente)."""
from __future__ import annotations

import pytest

from app.persistence import produto_depara_repo as repo
from app.persistence import router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _upsert(conn, **over):
    base = dict(
        cliente_cnpj="12.345.678/0001-99", chave_tipo="codigo", chave_valor=" abc ",
        fire_produto_id="10", fire_codigo="10", fire_ean="789", fire_nome="TENIS",
        criado_em="2026-07-24T10:00:00", criado_por="grazi@mm",
    )
    base.update(over)
    repo.upsert(conn, **base)


def test_norm_key_codigo_e_ean():
    assert repo._norm_key("codigo", " abc ") == "ABC"
    assert repo._norm_key("ean", "7.89-0") == "7890"
    assert repo._norm_cnpj("12.345.678/0001-99") == "12345678000199"


def test_upsert_e_lookup_por_codigo(env_conn):
    _upsert(env_conn)
    got = repo.lookup(env_conn, "12345678000199", codigos=["ABC"], eans=[])
    assert ("codigo", "ABC") in got
    assert got[("codigo", "ABC")]["fire_codigo"] == "10"


def test_lookup_normaliza_a_chave_de_busca(env_conn):
    _upsert(env_conn, chave_valor="ABC")
    # busca com valor sujo casa com o gravado normalizado
    got = repo.lookup(env_conn, "12345678000199", codigos=[" abc "], eans=[])
    assert ("codigo", "ABC") in got


def test_colisao_entre_varejistas_resolve_diferente(env_conn):
    _upsert(env_conn, cliente_cnpj="11111111000100", chave_valor="1234",
            fire_produto_id="50", fire_codigo="50", fire_nome="RIACHUELO X")
    _upsert(env_conn, cliente_cnpj="22222222000200", chave_valor="1234",
            fire_produto_id="60", fire_codigo="60", fire_nome="CENTAURO Y")
    r1 = repo.lookup(env_conn, "11111111000100", codigos=["1234"], eans=[])
    r2 = repo.lookup(env_conn, "22222222000200", codigos=["1234"], eans=[])
    assert r1[("codigo", "1234")]["fire_codigo"] == "50"
    assert r2[("codigo", "1234")]["fire_codigo"] == "60"


def test_upsert_substitui_mesmo_vinculo(env_conn):
    _upsert(env_conn, chave_valor="ABC", fire_codigo="10")
    _upsert(env_conn, chave_valor="ABC", fire_codigo="99", fire_nome="OUTRO")
    rows = repo.list_for_client(env_conn, "12345678000199")
    assert len(rows) == 1
    assert rows[0]["fire_codigo"] == "99"


def test_delete_desfaz(env_conn):
    _upsert(env_conn)
    rows = repo.list_for_client(env_conn, "12345678000199")
    repo.delete(env_conn, rows[0]["id"])
    assert repo.list_for_client(env_conn, "12345678000199") == []
```

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_produto_depara.py -v`
Expected: FAIL (módulo e tabela não existem).

- [ ] **Step 3: Adicionar a tabela em `app/persistence/schema_env.py`**

Após o bloco `clientes_fire` (antes do fechamento `"""` na linha 131):

```sql
-- De-para de produto por cliente: a referência do varejista que não casa no
-- Fire vira um vínculo persistente (feito uma vez, lembrado para sempre).
-- Chaveado por CNPJ do cliente para não colidir entre varejistas.
CREATE TABLE IF NOT EXISTS produto_depara (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_cnpj    TEXT NOT NULL,
    chave_tipo      TEXT NOT NULL,   -- 'codigo' | 'ean'
    chave_valor     TEXT NOT NULL,   -- normalizado
    fire_produto_id TEXT NOT NULL,
    fire_codigo     TEXT NOT NULL,
    fire_ean        TEXT,
    fire_nome       TEXT NOT NULL,
    criado_em       TEXT NOT NULL,
    criado_por      TEXT,
    UNIQUE (cliente_cnpj, chave_tipo, chave_valor)
);
```

E em `INDEXES_SQL` (após a linha 139):

```sql
CREATE INDEX IF NOT EXISTS idx_depara_cliente ON produto_depara(cliente_cnpj);
```

- [ ] **Step 4: Criar `app/persistence/produto_depara_repo.py`**

```python
"""De-para de produto por cliente (`produto_depara`, db do ambiente).

A referência do varejista que não casa no Fire vira um vínculo persistente,
chaveado por CNPJ do cliente. `_norm_key` DEVE ser idêntica na gravação e na
leitura — chave divergente = vínculo fantasma.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

_COLS = (
    "id", "cliente_cnpj", "chave_tipo", "chave_valor",
    "fire_produto_id", "fire_codigo", "fire_ean", "fire_nome",
    "criado_em", "criado_por",
)


def _norm_cnpj(cnpj: str | None) -> str:
    return re.sub(r"\D", "", cnpj or "")


def _norm_key(tipo: str, valor: str) -> str:
    if tipo == "ean":
        return re.sub(r"\D", "", valor or "")
    return (valor or "").strip().upper()


def upsert(
    conn: sqlite3.Connection,
    *,
    cliente_cnpj: str,
    chave_tipo: str,
    chave_valor: str,
    fire_produto_id: str,
    fire_codigo: str,
    fire_ean: str | None,
    fire_nome: str,
    criado_em: str,
    criado_por: str | None,
) -> None:
    """Grava (ou substitui) um vínculo. Last-write-wins na chave única."""
    conn.execute(
        """
        INSERT INTO produto_depara
            (cliente_cnpj, chave_tipo, chave_valor,
             fire_produto_id, fire_codigo, fire_ean, fire_nome, criado_em, criado_por)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (cliente_cnpj, chave_tipo, chave_valor) DO UPDATE SET
            fire_produto_id = excluded.fire_produto_id,
            fire_codigo     = excluded.fire_codigo,
            fire_ean        = excluded.fire_ean,
            fire_nome       = excluded.fire_nome,
            criado_em       = excluded.criado_em,
            criado_por      = excluded.criado_por
        """,
        (
            _norm_cnpj(cliente_cnpj), chave_tipo, _norm_key(chave_tipo, chave_valor),
            fire_produto_id, fire_codigo, fire_ean, fire_nome, criado_em, criado_por,
        ),
    )
    conn.commit()


def lookup(
    conn: sqlite3.Connection,
    cliente_cnpj: str,
    *,
    codigos: list[str],
    eans: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Resolve vínculos do cliente para as chaves dadas. Chave do dict:
    (chave_tipo, chave_valor_normalizada). Batelado (uma query)."""
    cnpj = _norm_cnpj(cliente_cnpj)
    wanted: list[tuple[str, str]] = []
    wanted += [("codigo", _norm_key("codigo", c)) for c in codigos if c]
    wanted += [("ean", _norm_key("ean", e)) for e in eans if e]
    wanted = list({w for w in wanted if w[1]})
    if not cnpj or not wanted:
        return {}

    out: dict[tuple[str, str], dict] = {}
    # (tipo, valor) pares via OR de igualdades — poucos itens por pedido.
    clause = " OR ".join(["(chave_tipo = ? AND chave_valor = ?)"] * len(wanted))
    params: list[str] = [cnpj]
    for tipo, val in wanted:
        params += [tipo, val]
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara "
        f"WHERE cliente_cnpj = ? AND ({clause})",
        params,
    ).fetchall()
    for r in rows:
        d = dict(zip(_COLS, r, strict=True))
        out[(d["chave_tipo"], d["chave_valor"])] = d
    return out


def delete(conn: sqlite3.Connection, id: int) -> None:
    conn.execute("DELETE FROM produto_depara WHERE id = ?", (id,))
    conn.commit()


def list_for_client(conn: sqlite3.Connection, cliente_cnpj: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM produto_depara "
        f"WHERE cliente_cnpj = ? ORDER BY id",
        (_norm_cnpj(cliente_cnpj),),
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]
```

- [ ] **Step 5: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_produto_depara.py -v`
Expected: PASS (7 testes).

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/persistence/ tests/test_produto_depara.py && ruff format app/persistence/ tests/test_produto_depara.py
git add app/persistence/schema_env.py app/persistence/produto_depara_repo.py tests/test_produto_depara.py
git commit -m "feat(persistence): tabela e repo produto_depara (de-para por cliente)"
```

---

### Task 5: 3º degrau no match (de-para → FIND_PRODUCT_BY_SEQ)

**Files:**
- Modify: `app/erp/product_check.py` (bloco batelado da Task 1)
- Test: `tests/test_product_check.py`

**Interfaces:**
- Consumes: `produto_depara_repo.lookup(...)`, `queries.find_products_by_seqs_sql`, `db.connect()`.
- Produces: itens com `match_source='depara'` no mesmo shape; de-para órfão (SEQ sumiu) → `match=False`.

- [ ] **Step 1: Escrever o teste do degrau de-para**

Adicionar em `tests/test_product_check.py`. Mockar o de-para via patch no lookup e o `db.connect`:

```python
@patch("app.erp.product_check.produto_depara_repo")
@patch("app.erp.product_check.FirebirdConnection")
def test_check_order_terceiro_degrau_depara(mock_fb, mock_depara):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(
        client_row=(1, "ACME"),
        ean_rows=[], code_rows=[],
        seq_rows=[(77, "TENIS DEPARA", 120.0)],  # resolve SEQ do vínculo
    )
    mock_fb.return_value.connect.return_value = ctx
    # de-para: código "REF-X" do cliente → SEQ 77
    mock_depara.lookup.return_value = {
        ("codigo", "REF-X"): {"fire_produto_id": "77", "fire_codigo": "77"},
    }

    order = _order([{"product_code": "REF-X", "unit_price": 120.0}])
    report = product_check.check_order(order)

    item = report["items"][0]
    assert item["match"] is True
    assert item["match_source"] == "depara"
    assert item["fire_product_id"] == 77
    assert item["price_status"] == "match"


@patch("app.erp.product_check.produto_depara_repo")
@patch("app.erp.product_check.FirebirdConnection")
def test_depara_orfao_vira_sem_match(mock_fb, mock_depara):
    mock_fb.return_value.is_configured.return_value = True
    ctx, cur = _make_fb_ctx_batched(client_row=(1, "ACME"), seq_rows=[])  # SEQ sumiu
    mock_fb.return_value.connect.return_value = ctx
    mock_depara.lookup.return_value = {
        ("codigo", "REF-X"): {"fire_produto_id": "999", "fire_codigo": "999"},
    }
    order = _order([{"product_code": "REF-X", "unit_price": 1.0}])
    report = product_check.check_order(order)
    assert report["items"][0]["match"] is False
```

Nota: `_make_fb_ctx_batched` precisa aceitar `seq_rows` e responder ao SQL `SEQ IN` (já previsto no Step 1 da Task 1).

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_product_check.py::test_check_order_terceiro_degrau_depara -v`
Expected: FAIL (o degrau de-para ainda não existe).

- [ ] **Step 3: Importar o repo e resolver o de-para no bloco batelado**

Em `app/erp/product_check.py`, adicionar no topo:

```python
from app.persistence import db, produto_depara_repo
```

No bloco batelado (Task 1), **depois** de montar `ean_map`/`code_map` e **antes** do `for it in order.items:`, resolver o de-para dos itens que não casaram por EAN/código:

```python
            # 3º degrau: de-para por cliente (SQLite local) → resolve SEQ no Fire.
            depara_map: dict[tuple[str, str], dict] = {}
            seq_map: dict[Any, tuple] = {}
            unmatched_codes = [
                it.product_code for it in order.items
                if it.product_code and it.product_code not in code_map
                and not (it.ean and it.ean in ean_map)
            ]
            unmatched_eans = [
                it.ean for it in order.items
                if it.ean and it.ean not in ean_map
            ]
            if unmatched_codes or unmatched_eans:
                try:
                    with db.connect() as sconn:
                        depara_map = produto_depara_repo.lookup(
                            sconn, order.header.customer_cnpj or "",
                            codigos=unmatched_codes, eans=unmatched_eans,
                        )
                except Exception as exc:  # noqa: BLE001 — sem ambiente ativo / db off: pula degrau
                    from app.utils.logger import logger
                    logger.debug(f"de-para lookup pulado: {type(exc).__name__}: {exc}")
                seqs = list({int(v["fire_produto_id"]) for v in depara_map.values()
                             if str(v["fire_produto_id"]).isdigit()})
                seq_map = _fetch_map_by_key(cur, queries.find_products_by_seqs_sql, seqs) if seqs else {}
```

Nota: `_fetch_map_by_key` monta `{key: (seq, desc, preco)}` a partir de `(SEQ, DESCRICAO, PRECO_VENDA)` — para `seq_map` a key É o próprio SEQ, então guarda `{seq: (seq, desc, preco)}`. OK.

No loop por item, **após** o `elif it.product_code and it.product_code in code_map:` e antes do `if hit is not None:`, adicionar o 3º degrau:

```python
                if hit is None:
                    dk = None
                    if it.product_code:
                        dk = ("codigo", produto_depara_repo._norm_key("codigo", it.product_code))
                    if (dk is None or dk not in depara_map) and it.ean:
                        dk = ("ean", produto_depara_repo._norm_key("ean", it.ean))
                    dv = depara_map.get(dk) if dk else None
                    if dv is not None:
                        seq = int(dv["fire_produto_id"]) if str(dv["fire_produto_id"]).isdigit() else None
                        resolved = seq_map.get(seq) if seq is not None else None
                        if resolved is not None:
                            hit, source = resolved, "depara"
```

O resto (`if hit is not None:` classificando preço) fica igual.

- [ ] **Step 4: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_product_check.py -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check app/erp/ tests/test_product_check.py && ruff format app/erp/ tests/test_product_check.py
git add app/erp/product_check.py tests/test_product_check.py
git commit -m "feat(erp): 3o degrau de match via de-para (resolve SEQ no Fire)"
```

---

### Task 6: Rotas — busca, vínculo, undo

**Files:**
- Modify: `app/web/server.py` (perto de `override_cliente`, ~2245; e `search_clientes`, ~2194)
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `require_user`, `_request_environment`, `db.connect()`, `catalogo_fire_repo.list_all` (para search), `produto_depara_repo`, `repo.get_import`, `repo.append_audit`, `check_order`.
- Produces: `GET /api/produtos/search`, `POST /api/imported/{id}/vincular-produto`, `DELETE /api/produtos/depara/{id}`.

- [ ] **Step 1: Escrever os testes das 3 rotas em `tests/test_web_server.py`**

Seguir o padrão dos testes de `override-cliente`/`search_clientes` já no arquivo (usar o client de teste + env ativo). Esboço:

```python
def test_produtos_search_local(client_com_env, seed_catalogo_fire):
    seed_catalogo_fire([{"fire_produto_id": "10", "codigo": "10", "nome": "TENIS AZUL", "ean": "789"}])
    r = client_com_env.get("/api/produtos/search?q=azul")
    assert r.status_code == 200
    assert any(p["fire_codigo"] == "10" for p in r.json()["results"])


def test_vincular_produto_grava_e_rechecka(client_com_env, parsed_import_sem_match):
    imp = parsed_import_sem_match  # item com product_code que não casa
    r = client_com_env.post(
        f"/api/imported/{imp}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": "10"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["check"]["items"][0]["match_source"] == "depara"


def test_delete_depara_desfaz(client_com_env, depara_existente):
    dep_id = depara_existente
    r = client_com_env.delete(f"/api/produtos/depara/{dep_id}")
    assert r.status_code == 200
```

Nota ao implementador: reusar fixtures de env/login já presentes em `tests/test_web_server.py`. Se não houver `seed_catalogo_fire`/`parsed_import_sem_match`, criá-las localmente com `router.env_connect` + `catalogo_fire_repo.replace_all` e `repo.record_import`. Verificar os nomes reais das fixtures antes: `grep -n "def client\|fixture\|record_import\|def _parsed" tests/test_web_server.py`.

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_web_server.py -k "produtos_search or vincular or depara" -v`
Expected: FAIL (rotas não existem).

- [ ] **Step 3: `GET /api/produtos/search` (catálogo local)**

Adicionar em `app/web/server.py`, perto de `search_clientes`:

```python
@app.get("/api/produtos/search")
def search_produtos(
    q: str,
    request: Request,
    limit: int = 20,
    _user: User = Depends(require_user),
) -> JSONResponse:
    """Busca produtos na cópia local do catálogo do Fire (catalogo_fire).
    Instantâneo, sem Firebird — funciona com a Fire offline."""
    from app.persistence import catalogo_fire_repo, db

    needle = (q or "").strip()
    if len(needle) < 2:
        raise HTTPException(status_code=400, detail="Informe ao menos 2 caracteres")
    limit = max(1, min(int(limit), 50))
    up = needle.upper()
    digits = re.sub(r"\D", "", needle)

    with db.connect() as conn:
        rows = catalogo_fire_repo.list_all(conn)

    def _hit(r: dict) -> bool:
        if up in (r.get("nome") or "").upper():
            return True
        if digits and (digits in (r.get("codigo") or "") or digits in (r.get("ean") or "")):
            return True
        return False

    results = [
        {
            "fire_produto_id": r["fire_produto_id"],
            "fire_codigo": r["codigo"],
            "fire_ean": r.get("ean"),
            "fire_nome": r["nome"],
        }
        for r in rows if _hit(r)
    ][:limit]
    return JSONResponse({"results": results, "total_returned": len(results)})
```

- [ ] **Step 4: `POST /api/imported/{id}/vincular-produto`**

```python
class VincularProdutoRequest(BaseModel):
    item_index: int
    fire_produto_id: str


@app.post("/api/imported/{import_id}/vincular-produto")
def vincular_produto(
    import_id: str,
    body: VincularProdutoRequest,
    request: Request,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Cria o vínculo de-para do item (code e/ou ean → produto do Fire),
    audita e re-roda o check. Só em pedidos em revisão."""
    from app.erp.product_check import check_order
    from app.models.order import Order
    from app.persistence import catalogo_fire_repo, db, produto_depara_repo, repo

    entry = repo.get_import(import_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if entry.get("portal_status") != "parsed":
        raise HTTPException(status_code=409, detail="Vínculo só é permitido em pedidos em revisão")
    snapshot = entry.get("snapshot")
    if not snapshot:
        raise HTTPException(status_code=422, detail="Snapshot do pedido indisponível")
    order = Order.model_validate(snapshot)
    if not (0 <= body.item_index < len(order.items)):
        raise HTTPException(status_code=422, detail="item_index fora do intervalo")
    item = order.items[body.item_index]

    with db.connect() as conn:
        prod = next(
            (p for p in catalogo_fire_repo.list_all(conn)
             if p["fire_produto_id"] == body.fire_produto_id), None
        )
        if prod is None:
            raise HTTPException(status_code=422, detail="Produto não encontrado no catálogo local")

        cnpj = order.header.customer_cnpj or ""
        now = datetime.now().isoformat(timespec="seconds")
        gravou = []
        for tipo, valor in (("ean", item.ean), ("codigo", item.product_code)):
            if not valor:
                continue
            produto_depara_repo.upsert(
                conn, cliente_cnpj=cnpj, chave_tipo=tipo, chave_valor=valor,
                fire_produto_id=prod["fire_produto_id"], fire_codigo=prod["codigo"],
                fire_ean=prod.get("ean"), fire_nome=prod["nome"],
                criado_em=now, criado_por=user.email,
            )
            gravou.append({"chave_tipo": tipo, "chave_valor": valor})

    if not gravou:
        raise HTTPException(status_code=422, detail="Item sem código nem EAN para vincular")

    with with_trace_id(entry.get("trace_id")):
        repo.append_audit(import_id, "produto_vinculo_criado", {
            "item_index": body.item_index, "fire_produto_id": prod["fire_produto_id"],
            "fire_nome": prod["nome"], "chaves": gravou,
            "user_id": user.id, "user_email": user.email,
        })

    check = check_order(order, env=_request_environment(request))
    return JSONResponse({"entry_id": import_id, "vinculos": gravou, "check": check})
```

- [ ] **Step 5: `DELETE /api/produtos/depara/{id}`**

```python
@app.delete("/api/produtos/depara/{depara_id}")
def delete_depara(
    depara_id: int,
    request: Request,
    user: User = Depends(require_user),
) -> JSONResponse:
    """Desfaz um vínculo de-para."""
    from app.persistence import db, produto_depara_repo, repo

    with db.connect() as conn:
        produto_depara_repo.delete(conn, depara_id)
    repo.append_audit(None, "produto_vinculo_removido",
                      {"depara_id": depara_id, "user_email": user.email})
    return JSONResponse({"deleted": depara_id})
```

Nota: se `repo.append_audit` exigir `import_id` não-nulo, trocar por log direto (`logger.info`) — verificar a assinatura com `grep -n "def append_audit" app/persistence/repo.py`.

- [ ] **Step 6: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_web_server.py -k "produtos_search or vincular or depara" -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
ruff check app/web/server.py tests/test_web_server.py && ruff format app/web/server.py tests/test_web_server.py
git add app/web/server.py tests/test_web_server.py
git commit -m "feat(web): rotas de busca/vinculo/undo de produto (de-para)"
```

---

# FASE C — Assistência (ranking + XLS + UI)

### Task 7: Ranking de candidatos

**Files:**
- Create: `app/erp/product_ranking.py`
- Test: `tests/test_product_ranking.py`

**Interfaces:**
- Consumes: linhas do `catalogo_fire` (dicts `{fire_produto_id, codigo, nome, ean}`).
- Produces: `product_ranking.rank_candidates(*, description, product_code, ean, catalog: list[dict], limit=5) -> list[dict]` — cada candidato com `score` (float) além dos campos do produto, ordenado desc.

- [ ] **Step 1: Escrever `tests/test_product_ranking.py`**

```python
from __future__ import annotations

from app.erp import product_ranking as pr

CATALOG = [
    {"fire_produto_id": "1", "codigo": "1", "nome": "TENIS CORRIDA AZUL", "ean": "7890001"},
    {"fire_produto_id": "2", "codigo": "2", "nome": "SANDALIA COURO PRETA", "ean": "7890002"},
    {"fire_produto_id": "3", "codigo": "3", "nome": "TENIS CAMINHADA CINZA", "ean": "7890003"},
]


def test_ranqueia_por_ean_parcial_primeiro():
    out = pr.rank_candidates(description="qualquer", product_code=None,
                             ean="0001", catalog=CATALOG, limit=3)
    assert out[0]["fire_produto_id"] == "1"


def test_ranqueia_por_tokens_da_descricao():
    out = pr.rank_candidates(description="tenis azul corrida", product_code=None,
                             ean=None, catalog=CATALOG, limit=3)
    assert out[0]["fire_produto_id"] == "1"
    assert all("score" in c for c in out)


def test_limita_resultados():
    out = pr.rank_candidates(description="tenis", product_code=None,
                             ean=None, catalog=CATALOG, limit=1)
    assert len(out) == 1


def test_sem_sinal_retorna_vazio_ou_score_zero():
    out = pr.rank_candidates(description="", product_code=None, ean=None,
                             catalog=CATALOG, limit=3)
    assert out == [] or all(c["score"] == 0 for c in out)
```

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_product_ranking.py -v`
Expected: FAIL (módulo não existe).

- [ ] **Step 3: Criar `app/erp/product_ranking.py`**

```python
"""Ranking assistido de candidatos do catálogo local para o picker de de-para.

Sugere; nunca aplica. Heurística barata sobre alguns milhares de linhas:
EAN parcial (peso alto) > sobreposição de tokens descrição×nome > código contido.
"""
from __future__ import annotations

import re


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", (s or "").upper()) if len(t) >= 2}


def _score(description: str, product_code: str | None, ean: str | None, prod: dict) -> float:
    score = 0.0
    ean_d = re.sub(r"\D", "", ean or "")
    prod_ean = re.sub(r"\D", "", prod.get("ean") or "")
    if ean_d and prod_ean and (ean_d in prod_ean or prod_ean in ean_d):
        score += 5.0
    dt, nt = _tokens(description), _tokens(prod.get("nome", ""))
    if dt and nt:
        inter = len(dt & nt)
        union = len(dt | nt)
        if union:
            score += 3.0 * (inter / union)
    code = (product_code or "").strip().upper()
    if code and (code in (prod.get("codigo") or "").upper() or code in (prod.get("nome") or "").upper()):
        score += 1.0
    return round(score, 4)


def rank_candidates(
    *,
    description: str,
    product_code: str | None,
    ean: str | None,
    catalog: list[dict],
    limit: int = 5,
) -> list[dict]:
    scored = []
    for prod in catalog:
        s = _score(description, product_code, ean, prod)
        if s > 0:
            scored.append({**prod, "score": s})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]
```

- [ ] **Step 4: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_product_ranking.py -v`
Expected: PASS.

- [ ] **Step 5: Expor sugestões na rota de busca (opt-in por item)**

Estender `GET /api/produtos/search` para aceitar params opcionais `desc`, `code`, `ean_item`: quando presentes, além do filtro por `q`, anexar `suggestions` (top 5 do ranking sobre o catálogo completo). Manter `q` >= 2 obrigatório **ou** permitir busca só por sugestão quando `desc`/`code`/`ean_item` vierem. Ajuste mínimo:

```python
    # após montar `rows` (catálogo completo), antes do filtro por needle:
    from app.erp import product_ranking
    suggestions = product_ranking.rank_candidates(
        description=request.query_params.get("desc", ""),
        product_code=request.query_params.get("code"),
        ean=request.query_params.get("ean_item"),
        catalog=rows, limit=5,
    )
    # ... results como antes ...
    return JSONResponse({"results": results, "suggestions": suggestions,
                         "total_returned": len(results)})
```

Adicionar 1 teste em `tests/test_web_server.py`: `?q=..&desc=tenis%20azul` retorna `suggestions` não vazio e ranqueado.

- [ ] **Step 6: Lint + commit**

```bash
ruff check app/erp/ tests/test_product_ranking.py tests/test_web_server.py && ruff format app/erp/ tests/test_product_ranking.py tests/test_web_server.py
git add app/erp/product_ranking.py tests/test_product_ranking.py app/web/server.py tests/test_web_server.py
git commit -m "feat(erp): ranking de candidatos para o picker de de-para (sugere, nao aplica)"
```

---

### Task 8: Enriquecer o XLS com a identidade do Fire

**Files:**
- Create: `app/erp/depara_apply.py`
- Modify: `app/web/server.py` (`_export_one_xlsx`, ~1857 — antes do `ERPExporter().export`)
- Test: `tests/test_depara_apply.py`

**Interfaces:**
- Consumes: `Order`, `produto_depara_repo.lookup`, `db.connect()`.
- Produces: `depara_apply.apply(order, *, conn) -> list[dict]` — muta os itens resolvidos (`product_code=fire_codigo`, `ean=fire_ean` se houver, anexa ref original ao `obs`) e retorna o resumo do que mudou.

- [ ] **Step 1: Escrever `tests/test_depara_apply.py`**

```python
from __future__ import annotations

import pytest

from app.erp import depara_apply
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import produto_depara_repo, router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _order():
    return Order(
        header=OrderHeader(order_number="T1", customer_cnpj="11111111000100", customer_name="X"),
        items=[
            OrderItem(description="A", product_code="REF-X", quantity=1),
            OrderItem(description="B", product_code="SEM-VINCULO", quantity=1),
        ],
    )


def test_apply_reescreve_item_vinculado(env_conn):
    produto_depara_repo.upsert(
        env_conn, cliente_cnpj="11111111000100", chave_tipo="codigo", chave_valor="REF-X",
        fire_produto_id="77", fire_codigo="77", fire_ean="7890", fire_nome="TENIS",
        criado_em="t", criado_por="grazi",
    )
    order = _order()
    changed = depara_apply.apply(order, conn=env_conn)
    assert order.items[0].product_code == "77"
    assert order.items[0].ean == "7890"
    assert "REF-X" in (order.items[0].obs or "")
    assert order.items[1].product_code == "SEM-VINCULO"  # intacto
    assert len(changed) == 1
```

- [ ] **Step 2: Rodar — deve FALHAR**

Run: `.venv/bin/pytest tests/test_depara_apply.py -v`
Expected: FAIL (módulo não existe).

- [ ] **Step 3: Criar `app/erp/depara_apply.py`**

```python
"""Aplica o de-para no Order antes de gerar o XLS.

Item resolvido por vínculo sai com a identidade do Fire (codigo/ean), com a
referência original do varejista anexada ao OBS (rastreabilidade). Mutação
transiente: o Order vem de Order.model_validate(snapshot), descartado após o
export. Não toca o snapshot persistido.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.persistence import produto_depara_repo

if TYPE_CHECKING:
    import sqlite3

    from app.models.order import Order


def apply(order: Order, *, conn: sqlite3.Connection) -> list[dict]:
    cnpj = order.header.customer_cnpj or ""
    codigos = [it.product_code for it in order.items if it.product_code]
    eans = [it.ean for it in order.items if it.ean]
    dm = produto_depara_repo.lookup(conn, cnpj, codigos=codigos, eans=eans)
    if not dm:
        return []

    changed: list[dict] = []
    for idx, it in enumerate(order.items):
        dv = None
        if it.ean:
            dv = dm.get(("ean", produto_depara_repo._norm_key("ean", it.ean)))
        if dv is None and it.product_code:
            dv = dm.get(("codigo", produto_depara_repo._norm_key("codigo", it.product_code)))
        if dv is None:
            continue
        orig = it.product_code or it.ean or ""
        it.product_code = dv["fire_codigo"]
        if dv.get("fire_ean"):
            it.ean = dv["fire_ean"]
        ref_note = f"ref cliente: {orig}"
        it.obs = f"{it.obs} | {ref_note}" if it.obs else ref_note
        changed.append({"item_index": idx, "fire_codigo": dv["fire_codigo"], "orig": orig})
    return changed
```

- [ ] **Step 4: Rodar — deve PASSAR**

Run: `.venv/bin/pytest tests/test_depara_apply.py -v`
Expected: PASS.

- [ ] **Step 5: Wire no `_export_one_xlsx`**

Em `app/web/server.py`, dentro do `with with_trace_id(...)` de `_export_one_xlsx` (~1857), **antes** de `paths = ERPExporter().export(order, str(output_path))`:

```python
        # Enriquecer com a identidade do Fire onde há vínculo de-para (SQLite local).
        from app.erp import depara_apply
        from app.persistence import db

        try:
            with db.connect() as sconn:
                dep_changed = depara_apply.apply(order, conn=sconn)
            if dep_changed:
                repo.append_audit(import_id, "depara_aplicado_no_xls", {"itens": dep_changed})
        except Exception as exc:  # noqa: BLE001 — enriquecimento é best-effort; XLS não pode falhar por isso
            from app.utils.logger import logger
            logger.warning(f"de-para apply pulado no export: {type(exc).__name__}: {exc}")
```

- [ ] **Step 6: Rodar suíte do web + exporter**

Run: `.venv/bin/pytest tests/test_web_server.py tests/test_exporter_split.py tests/test_depara_apply.py -v`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
ruff check app/erp/ app/web/server.py tests/test_depara_apply.py && ruff format app/erp/ app/web/server.py tests/test_depara_apply.py
git add app/erp/depara_apply.py app/web/server.py tests/test_depara_apply.py
git commit -m "feat(exporter): XLS usa identidade do Fire onde ha vinculo de-para"
```

---

### Task 9: UI — selo "vínculo", picker de produto, undo

**Files:**
- Modify: `app/web/static/index.html`

**Interfaces:**
- Consumes: `GET /api/produtos/search?q=&desc=&code=&ean_item=`, `POST /api/imported/{id}/vincular-produto`, `DELETE /api/produtos/depara/{id}`, `window.appShell.showError/showSuccess`.
- Produces: UI-only.

- [ ] **Step 1: Selo de origem no match**

No render da coluna de match (~1820), tratar `m.match_source === 'depara'`: mostrar `✓ vínculo` com tooltip "casado por vínculo que você criou". Distinguir visualmente do ✓ normal (ex.: cor `--accent` em vez de `--success`).

- [ ] **Step 2: Botão "Vincular" no item ✗**

Na linha do item sem match (`!m || !m.match`), além do `✗`, renderizar um botão pequeno "Vincular" que abre o picker para aquele `item_index`.

- [ ] **Step 3: Picker (busca + sugestões)**

Modal/inline: ao abrir, chamar `GET /api/produtos/search?desc=<item.description>&code=<item.product_code>&ean_item=<item.ean>` e listar `suggestions` no topo (pré-selecionar o 1º, **sem** aplicar) + campo de busca livre que refaz `?q=`. Cada candidato mostra `fire_codigo · fire_nome · fire_ean`.

- [ ] **Step 4: Confirmar o vínculo**

No clique em "Confirmar", `POST /api/imported/{id}/vincular-produto {item_index, fire_produto_id}`. Com a resposta (`body.check`), re-renderizar a tabela de itens (o item agora mostra `✓ vínculo`). `showSuccess('Vínculo criado — vai casar sozinho da próxima vez')`.

- [ ] **Step 5: Undo**

Onde o vínculo aparece (tooltip/detalhe), oferecer "desfazer" → `DELETE /api/produtos/depara/{id}`. (O `id` do vínculo pode ser obtido via uma listagem simples; se a rota de listagem por cliente não estiver exposta, o undo pode viver numa tela futura — nesta task, o mínimo é o DELETE funcionando a partir do id retornado no audit/confirm. Se o id não estiver disponível no fluxo, registrar como follow-up e não bloquear.)

- [ ] **Step 6: Verificação manual (GIF opcional)**

Run: `python ui.py` → subir um sample cujo item não casa → "Vincular" → escolher no picker → confirmar → ver `✓ vínculo` → **re-subir o mesmo arquivo** → confirmar que casa sozinho. Este re-upload é o teste de aceitação da feature inteira.

- [ ] **Step 7: Commit**

```bash
git add app/web/static/index.html
git commit -m "feat(web): picker de vinculo de produto + selo no preview"
```

---

### Task 10: Docs + suíte completa

**Files:**
- Modify: `docs/ai/modules/erp.md`, `docs/ai/modules/web.md`, `docs/ai/modules/exporters.md`

- [ ] **Step 1: Atualizar `docs/ai/modules/erp.md`**

Seção nova "De-para de produto (memória por cliente)": tabela `produto_depara`, `_norm_key`, 3º degrau no `check_order` (EAN→CODPROD_ALTERN→de-para→FIND_PRODUCT_BY_SEQ), batelamento (2N+1→~4). Caveat: `TRIM(CODPROD_ALTERN) IN (...)` não usa índice; de-para vai ao Firebird resolver SEQ (não casa com Fire offline).

- [ ] **Step 2: Atualizar `docs/ai/modules/web.md`**

Rotas novas: `GET /api/produtos/search`, `POST /api/imported/{id}/vincular-produto`, `DELETE /api/produtos/depara/{id}`. E: push FlowPCP agora enfileira no outbox (não faz HTTP no request path).

- [ ] **Step 3: Atualizar `docs/ai/modules/exporters.md`**

Seção: item resolvido por de-para sai com `CODIGO_PRODUTO=fire_codigo`/`EAN=fire_ean`, ref original no OBS, via `depara_apply.apply` antes do `ERPExporter.export`.

- [ ] **Step 4: Suíte completa + lint**

Run:
```bash
.venv/bin/pytest tests/ -v
ruff check app/ tests/ && ruff format app/ tests/
```
Expected: tudo verde, ruff limpo.

- [ ] **Step 5: Commit**

```bash
git add docs/ai/modules/
git commit -m "docs(ai): de-para de produto, batelamento do check e push via outbox"
```

---

## Self-Review

**1. Spec coverage:**
- §1.1 tabela+repo → Task 4 ✓
- §1.2 3º degrau → Task 5 ✓
- §1.3 selo no preview → Task 9 (Step 1) ✓
- §1.4 rotas → Task 6 ✓
- §1.5 ranking → Task 7 ✓
- §1.6 XLS identidade Fire → Task 8 ✓
- §2.1 push via outbox → Task 2 ✓
- §2.2 batelamento → Task 1 ✓
- §2.3 progresso no botão → Task 3 ✓
- Testes (§Testes) → distribuídos + Task 10 suíte completa ✓

**2. Placeholder scan:** sem TBD/TODO no código dos steps. Os pontos "verificar assinatura antes" (append_audit, fixtures de test_web_server) são instruções de verificação explícitas, não placeholders de implementação — o código a escrever está completo.

**3. Type consistency:**
- `check_order(order, *, env=None) -> dict` — inalterado em todos os callsites.
- `produto_depara_repo.lookup(conn, cnpj, *, codigos, eans) -> dict[(tipo,valor)->dict]` — usado igual em Task 5, 6, 8.
- `_norm_key(tipo, valor)` / `_norm_cnpj(cnpj)` — mesma assinatura em repo, check e apply.
- `FlowPCPExporter(*, tenant_id).enqueue(order, *, import_id) -> bool` — consistente em exporter, hook e testes.
- `rank_candidates(*, description, product_code, ean, catalog, limit)` — igual em Task 7 e na rota (Task 7 Step 5).
- `depara_apply.apply(order, *, conn) -> list[dict]` — igual em módulo e wire.

Consistente. Sem gaps.
