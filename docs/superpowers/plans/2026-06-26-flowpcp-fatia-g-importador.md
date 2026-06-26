# FlowPCP Fatia G (lado Importador) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Construir o lado Python da ponte Importador↔FlowPCP (Modelo B / OVERLAY): push de pedido novo pro Flow + poll de decisões + reconciliação da data de entrega no FIRE.

**Architecture:** Modelo B (OVERLAY): o pedido já vai pro FIRE pelo fluxo XLS de hoje; o FlowPCP recebe o pedido em paralelo e, quando o operador renegocia a data (`prazo_pactuado`), um job de poll (30s) detecta a decisão e executa `UPDATE CAB_VENDAS SET DT_ENTREGA` no FIRE, confirmando de volta. Tudo construído contra o contrato (HTTP mockado, TDD); a integração viva espera os endpoints `/decisoes` e `/confirmar-reconciliacao` serem implementados no lado FlowPCP (frente separada).

**Tech Stack:** Python 3.11+, pydantic v2, httpx (via `app.http.OutboundClient`), APScheduler, SQLite (per-env), firebird-driver, loguru, pytest, ruff.

## Global Constraints

- Python 3.11+ (`X | Y` unions, `from __future__ import annotations` no topo de cada módulo).
- pydantic v2 para todos os modelos de wire. `N815` (camelCase) é permitido nos modelos do contrato F.5.
- **Auth = header `X-Service-Token: <service_token>` + `X-Tenant-Id: <uuid>`** em TODAS as chamadas. NÃO usar `Authorization: Bearer` (o corpo da spec erra; ver Addendum da spec).
- HTTP sempre via `app.http.OutboundClient` (retry + trace_id + logs). Client injetável via param `outbound=`/`transport=` pra testes (espelhar `app/integrations/gestor/client.py`).
- FIRE: schema REAL é `CAB_VENDAS.DT_ENTREGA` (data de entrega do pedido), chave `TRIM(PEDIDO_CLIENTE)` + `CLIENTE` (FK = `CADASTRO.CODIGO`, resolvido do CNPJ via `FIND_CLIENT_BY_CNPJ`). NÃO existe `DATA_ENTREGA`/`NUM_PEDIDO`/`CNPJ_CLIENTE`.
- Contrato canônico dos 2 endpoints novos: §5.1 e §5.2 de `pcp-app/docs/superpowers/specs/2026-06-22-fatia-g-importador-ponte-flowpcp-design.md`.
- `dry_run=true`: poll + log do UPDATE, mas **não escreve no FIRE**; confirma de volta com `acao="data_atualizada"` + observação `DRY_RUN` (decisão da spec, §regra dry_run).
- Token vem do config per-ambiente; só o ambiente **MM** habilita FlowPCP (Nasmar só vende, nunca usa).
- Testes: `pytest tests/test_flowpcp_*.py -v`. Suíte completa (`.venv/bin/pytest tests/ -q`) verde antes de cada commit.
- Lint: `ruff check app/ tests/` limpo antes de cada commit.

---

### Task 1: Schemas pydantic do contrato FlowPCP

**Files:**
- Create: `app/integrations/flowpcp/schema.py`
- Test: `tests/test_flowpcp_schema.py`

**Interfaces:**
- Produces: `DecisaoFlowPCP`, `DecisoesResponse`, `AcaoReconciliacao` (str Enum), `ConfirmarReconciliacaoRequest`, `RecebimentoRequest` (+ `ClienteRecebimento`, `ItemRecebimento`, `OrigemRecebimento`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_schema.py
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
    DecisoesResponse,
)


def test_parse_decisoes_response_from_contract():
    raw = {
        "decisoes": [
            {
                "id": "11111111-1111-4111-8111-111111111111",
                "pedido_erp": "AW097",
                "cliente_cnpj": "12.345.678/0001-90",
                "nome_cliente": "MM Americanense",
                "prazo_entrega_original": "2026-07-10T03:00:00.000Z",
                "prazo_pactuado": "2026-07-17T03:00:00.000Z",
                "status": "em_pool",
                "motivo_decisao": "Negociado +7 dias",
                "atualizado_em": "2026-06-22T14:32:15.123Z",
            }
        ],
        "proximo_cursor": "2026-06-22T14:32:15.123Z",
    }
    resp = DecisoesResponse.model_validate(raw)
    assert len(resp.decisoes) == 1
    assert resp.decisoes[0].pedido_erp == "AW097"
    assert resp.decisoes[0].prazo_pactuado == "2026-07-17T03:00:00.000Z"
    assert resp.proximo_cursor == "2026-06-22T14:32:15.123Z"


def test_confirmar_request_rejects_unknown_acao():
    with pytest.raises(ValidationError):
        ConfirmarReconciliacaoRequest(acao="acao_inexistente")


def test_confirmar_request_serializes_optional_fields():
    req = ConfirmarReconciliacaoRequest(
        acao=AcaoReconciliacao.DATA_ATUALIZADA,
        fire_id_externo="AW097",
        observacoes="UPDATE OK",
    )
    body = req.model_dump(exclude_none=True)
    assert body == {
        "acao": "data_atualizada",
        "fire_id_externo": "AW097",
        "observacoes": "UPDATE OK",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.integrations.flowpcp.schema'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/schema.py
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class DecisaoFlowPCP(BaseModel):
    id: str
    pedido_erp: str
    cliente_cnpj: str | None = None
    nome_cliente: str | None = None
    prazo_entrega_original: str
    prazo_pactuado: str | None = None
    status: str  # "em_pool" | "rejeitado"
    motivo_decisao: str | None = None
    atualizado_em: str


class DecisoesResponse(BaseModel):
    decisoes: list[DecisaoFlowPCP]
    proximo_cursor: str | None = None


class AcaoReconciliacao(str, Enum):
    DATA_ATUALIZADA = "data_atualizada"
    CANCELAMENTO_PENDENTE_MANUAL = "cancelamento_pendente_manual"
    SEM_ACAO_NECESSARIA = "sem_acao_necessaria"
    PEDIDO_NAO_ENCONTRADO_NO_FIRE = "pedido_nao_encontrado_no_fire"


class ConfirmarReconciliacaoRequest(BaseModel):
    acao: AcaoReconciliacao
    fire_id_externo: str | None = None
    observacoes: str | None = Field(default=None, max_length=1000)


# ── Push de pedido novo (contrato F.5 /recebimento) ───────────────────────────
class ClienteRecebimento(BaseModel):
    nome: str
    cnpj: str | None = None


class ItemRecebimento(BaseModel):
    produtoCodigo: str | None = None  # noqa: N815 — wire é camelCase
    produtoEan: str | None = None  # noqa: N815
    descricao: str
    quantidade: float
    precoUnitario: float | None = None  # noqa: N815


class OrigemRecebimento(BaseModel):
    importadorVersao: str  # noqa: N815
    arquivoOriginal: str  # noqa: N815
    parserUsado: str  # noqa: N815
    confiancaParser: str  # noqa: N815 — "alta" | "media" | "baixa"


class RecebimentoRequest(BaseModel):
    schema_: str = Field(default="pedido.recebimento.v1", alias="schema")
    externalId: str  # noqa: N815
    fornecedor: str
    pedidoNumero: str  # noqa: N815
    emitidoEm: str  # noqa: N815
    prazoSolicitado: str | None = None  # noqa: N815
    cliente: ClienteRecebimento
    itens: list[ItemRecebimento]
    origem: OrigemRecebimento

    model_config = {"populate_by_name": True}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_schema.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/schema.py tests/test_flowpcp_schema.py
git commit -m "feat(flowpcp): schemas pydantic do contrato (decisoes + confirmar + recebimento)"
```

---

### Task 2: FlowPCPClient (HTTP)

**Files:**
- Create: `app/integrations/flowpcp/client.py`
- Modify: `app/integrations/flowpcp/__init__.py` (export `FlowPCPClient`, `FlowPCPClientError`)
- Test: `tests/test_flowpcp_client.py`

**Interfaces:**
- Consumes: `RecebimentoRequest`, `DecisoesResponse`, `ConfirmarReconciliacaoRequest` (Task 1); `app.http.OutboundClient`.
- Produces: `FlowPCPClient(base_url, service_token, tenant_id, *, outbound=None)` com métodos `send_order(req, *, idempotency_key) -> dict`, `list_decisoes(cursor=None, limit=50) -> DecisoesResponse`, `confirmar_reconciliacao(decisao_id, req) -> dict` (dict tem `{"conflict": True, ...}` no 409); `FlowPCPClientError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_client.py
from __future__ import annotations

import json

import httpx
import pytest

from app.http.client import OutboundClient
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
)

TENANT = "1798c3c5-0fb6-4edb-a523-e13fb5bf52a0"
TOKEN = "test-service-token"


def _client(handler) -> FlowPCPClient:
    outbound = OutboundClient(
        base_url="https://flow.test",
        default_headers={
            "X-Service-Token": TOKEN,
            "X-Tenant-Id": TENANT,
            "Content-Type": "application/json",
        },
        transport=httpx.MockTransport(handler),
    )
    return FlowPCPClient(
        base_url="https://flow.test",
        service_token=TOKEN,
        tenant_id=TENANT,
        outbound=outbound,
    )


def test_list_decisoes_sends_auth_and_parses():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["token"] = request.headers.get("X-Service-Token")
        seen["tenant"] = request.headers.get("X-Tenant-Id")
        seen["cursor"] = request.url.params.get("cursor")
        return httpx.Response(200, json={"decisoes": [], "proximo_cursor": None})

    resp = _client(handler).list_decisoes(cursor="2026-06-22T14:30:00.000Z")
    assert seen["method"] == "GET"
    assert seen["path"] == "/api/portal-pedidos/decisoes"
    assert seen["token"] == TOKEN
    assert seen["tenant"] == TENANT
    assert seen["cursor"] == "2026-06-22T14:30:00.000Z"
    assert resp.decisoes == []


def test_confirmar_posts_to_id_path_and_handles_409():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/portal-pedidos/decisoes/dec-1/confirmar-reconciliacao"
        body = json.loads(request.content)
        assert body["acao"] == "data_atualizada"
        return httpx.Response(409, json={"error": "ja_reconciliado"})

    out = _client(handler).confirmar_reconciliacao(
        "dec-1",
        ConfirmarReconciliacaoRequest(acao=AcaoReconciliacao.DATA_ATUALIZADA),
    )
    assert out["conflict"] is True


def test_send_order_raises_on_500():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    from app.integrations.flowpcp.schema import (
        ClienteRecebimento,
        ItemRecebimento,
        OrigemRecebimento,
        RecebimentoRequest,
    )

    req = RecebimentoRequest(
        externalId="imp-1",
        fornecedor="Centauro",
        pedidoNumero="AW097",
        emitidoEm="2026-06-15T10:00:00.000Z",
        cliente=ClienteRecebimento(nome="MM", cnpj="12345678000190"),
        itens=[ItemRecebimento(descricao="meia", quantidade=10)],
        origem=OrigemRecebimento(
            importadorVersao="1.0.0",
            arquivoOriginal="p.pdf",
            parserUsado="Test",
            confiancaParser="alta",
        ),
    )
    with pytest.raises(FlowPCPClientError):
        _client(handler).send_order(req, idempotency_key="imp-1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_client.py -v`
Expected: FAIL — `ModuleNotFoundError: app.integrations.flowpcp.client`

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/client.py
from __future__ import annotations

from typing import Any

from app.http.client import HttpError, OutboundClient
from app.http.policies import idempotent_post_policy
from app.integrations.flowpcp.schema import (
    ConfirmarReconciliacaoRequest,
    DecisoesResponse,
    RecebimentoRequest,
)
from app.utils.logger import logger

_RECEBIMENTO_PATH = "/api/portal-pedidos/recebimento"
_DECISOES_PATH = "/api/portal-pedidos/decisoes"
DEFAULT_TIMEOUT_SECONDS = 30.0


class FlowPCPClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class FlowPCPClient:
    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        tenant_id: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        outbound: OutboundClient | None = None,
    ) -> None:
        self._service_token = service_token
        self._tenant_id = tenant_id
        if outbound is None:
            outbound = OutboundClient(
                base_url=base_url,
                timeout=timeout,
                retry_policy=idempotent_post_policy(),
                default_headers={
                    "X-Service-Token": service_token,
                    "X-Tenant-Id": tenant_id,
                    "Content-Type": "application/json",
                },
            )
        self._client = outbound

    def close(self) -> None:
        self._client.close()

    def send_order(self, request: RecebimentoRequest, *, idempotency_key: str) -> dict[str, Any]:
        body = request.model_dump(by_alias=True, exclude_none=False)
        resp = self._post(_RECEBIMENTO_PATH, body, idempotency_key=idempotency_key)
        return resp.json()

    def list_decisoes(self, cursor: str | None = None, limit: int = 50) -> DecisoesResponse:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        try:
            resp = self._client.get(_DECISOES_PATH, params=params)
        except HttpError as exc:
            raise FlowPCPClientError(f"list_decisoes falhou: {exc}", status_code=exc.status_code, body=exc.body) from exc
        if not resp.is_success:
            raise FlowPCPClientError(f"decisoes status {resp.status_code}", status_code=resp.status_code, body=(resp.text or "")[:500])
        return DecisoesResponse.model_validate(resp.json())

    def confirmar_reconciliacao(self, decisao_id: str, request: ConfirmarReconciliacaoRequest) -> dict[str, Any]:
        path = f"{_DECISOES_PATH}/{decisao_id}/confirmar-reconciliacao"
        body = request.model_dump(mode="json", exclude_none=True)
        try:
            resp = self._client.post_json(
                path, json=body, idempotency_key=f"reconciliar-{decisao_id}-{body['acao']}"
            )
        except HttpError as exc:
            raise FlowPCPClientError(f"confirmar falhou: {exc}", status_code=exc.status_code, body=exc.body) from exc
        if resp.status_code == 409:
            logger.warning(f"flowpcp confirmar 409 (ja_reconciliado) decisao={decisao_id}")
            return {"conflict": True, "details": resp.json()}
        if not resp.is_success:
            raise FlowPCPClientError(f"confirmar status {resp.status_code}", status_code=resp.status_code, body=(resp.text or "")[:500])
        return resp.json()

    def _post(self, path: str, body: dict[str, Any], *, idempotency_key: str):
        try:
            resp = self._client.post_json(path, json=body, idempotency_key=idempotency_key)
        except HttpError as exc:
            raise FlowPCPClientError(f"POST {path} falhou: {exc}", status_code=exc.status_code, body=exc.body) from exc
        if not resp.is_success:
            raise FlowPCPClientError(f"POST {path} status {resp.status_code}", status_code=resp.status_code, body=(resp.text or "")[:500])
        return resp
```

Update `app/integrations/flowpcp/__init__.py`:

```python
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError

__all__ = ["FlowPCPClient", "FlowPCPClientError"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_client.py -v`
Expected: PASS (3 passed). If `OutboundClient` não aceitar `transport=` kwarg, conferir a assinatura em `app/http/client.py` e ajustar o helper `_client` do teste para o mecanismo de injeção real (ex.: `transport=` no `httpx.Client` interno).

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/client.py app/integrations/flowpcp/__init__.py tests/test_flowpcp_client.py
git commit -m "feat(flowpcp): FlowPCPClient (send_order/list_decisoes/confirmar) com X-Service-Token"
```

---

### Task 3: Config per-ambiente

**Files:**
- Create: `app/integrations/flowpcp/config.py`
- Test: `tests/test_flowpcp_config.py`

**Interfaces:**
- Produces: `FlowPCPConfig` (frozen dataclass: `enabled, base_url, service_token, tenant_id, timezone, dry_run, poll_interval_s, request_timeout_s`); `load_flowpcp_config(env: dict) -> FlowPCPConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_config.py
from __future__ import annotations

from app.integrations.flowpcp.config import FlowPCPConfig, load_flowpcp_config


def test_default_is_disabled():
    cfg = load_flowpcp_config({})
    assert cfg.enabled is False
    assert cfg.timezone == "America/Sao_Paulo"
    assert cfg.poll_interval_s == 30


def test_loads_enabled_mm_config():
    cfg = load_flowpcp_config(
        {
            "flowpcp": {
                "enabled": True,
                "base_url": "https://flow.test",
                "service_token": "tok",
                "tenant_id": "uuid-mm",
                "dry_run": True,
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.base_url == "https://flow.test"
    assert cfg.dry_run is True
    assert isinstance(cfg, FlowPCPConfig)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_config.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/config.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlowPCPConfig:
    enabled: bool = False
    base_url: str = ""
    service_token: str = ""
    tenant_id: str = ""
    timezone: str = "America/Sao_Paulo"
    dry_run: bool = False
    poll_interval_s: int = 30
    request_timeout_s: float = 30.0


def load_flowpcp_config(env: dict) -> FlowPCPConfig:
    """Lê a sub-seção `flowpcp` do config do ambiente. Desligado por padrão.
    Só o ambiente MM preenche; Nasmar fica disabled."""
    raw = (env or {}).get("flowpcp") or {}
    return FlowPCPConfig(
        enabled=bool(raw.get("enabled", False)),
        base_url=str(raw.get("base_url", "")),
        service_token=str(raw.get("service_token", "")),
        tenant_id=str(raw.get("tenant_id", "")),
        timezone=str(raw.get("timezone", "America/Sao_Paulo")),
        dry_run=bool(raw.get("dry_run", False)),
        poll_interval_s=int(raw.get("poll_interval_s", 30)),
        request_timeout_s=float(raw.get("request_timeout_s", 30.0)),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/config.py tests/test_flowpcp_config.py
git commit -m "feat(flowpcp): FlowPCPConfig per-ambiente (disabled default; só MM liga)"
```

> Nota: a leitura do `service_token` via `secret_store` (criptografado) + UI de config é Task 8/follow-up. Por ora o token entra via dict de config (env var/JSON), suficiente pra dev e testes.

---

### Task 4: Persistência local (cursor + tentativas)

**Files:**
- Modify: `app/persistence/schema_env.py` (acrescentar 2 tabelas ao `TABLES_SQL`)
- Create: `app/persistence/flowpcp_repo.py`
- Test: `tests/test_flowpcp_repo.py`

**Interfaces:**
- Consumes: padrão de conexão SQLite per-env (fixture `tmp_env_db` do conftest; funções recebem `conn: sqlite3.Connection`).
- Produces: `get_last_cursor(conn) -> str | None`, `save_last_cursor(conn, cursor)`, `register_attempt(conn, decisao_id) -> int`, `get_attempts_count(conn, decisao_id) -> int`, `mark_reconciliada(conn, decisao_id, acao)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_repo.py
from __future__ import annotations

from app.persistence import flowpcp_repo as repo
from app.persistence.schema_env import TABLES_SQL


def _init(conn):
    conn.executescript(TABLES_SQL)
    return conn


def test_cursor_roundtrip(tmp_env_db):
    conn = _init(tmp_env_db)
    assert repo.get_last_cursor(conn) is None
    repo.save_last_cursor(conn, "2026-06-22T14:00:00.000Z")
    assert repo.get_last_cursor(conn) == "2026-06-22T14:00:00.000Z"
    repo.save_last_cursor(conn, "2026-06-22T15:00:00.000Z")
    assert repo.get_last_cursor(conn) == "2026-06-22T15:00:00.000Z"


def test_attempts_increment(tmp_env_db):
    conn = _init(tmp_env_db)
    assert repo.get_attempts_count(conn, "dec-1") == 0
    assert repo.register_attempt(conn, "dec-1") == 1
    assert repo.register_attempt(conn, "dec-1") == 2
    assert repo.get_attempts_count(conn, "dec-1") == 2


def test_mark_reconciliada(tmp_env_db):
    conn = _init(tmp_env_db)
    repo.register_attempt(conn, "dec-1")
    repo.mark_reconciliada(conn, "dec-1", "data_atualizada")
    row = conn.execute(
        "SELECT acao_executada, reconciliado_em FROM flowpcp_decisoes_mapping WHERE decisao_id=?",
        ("dec-1",),
    ).fetchone()
    assert row["acao_executada"] == "data_atualizada"
    assert row["reconciliado_em"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_repo.py -v`
Expected: FAIL — `app.persistence.flowpcp_repo` missing (e/ou tabela inexistente).

- [ ] **Step 3: Write minimal implementation**

Acrescentar ao final do `TABLES_SQL` em `app/persistence/schema_env.py` (antes do fechamento `"""`):

```sql
CREATE TABLE IF NOT EXISTS flowpcp_decisoes_mapping (
    decisao_id      TEXT PRIMARY KEY,
    pedido_erp      TEXT,
    cliente_cnpj    TEXT,
    acao_executada  TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    reconciliado_em TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS flowpcp_cursor_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_cursor   TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
);
```

```python
# app/persistence/flowpcp_repo.py
from __future__ import annotations

import sqlite3


def get_last_cursor(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT last_cursor FROM flowpcp_cursor_state WHERE id = 1").fetchone()
    return row["last_cursor"] if row else None


def save_last_cursor(conn: sqlite3.Connection, cursor: str) -> None:
    conn.execute(
        """
        INSERT INTO flowpcp_cursor_state (id, last_cursor, atualizado_em)
        VALUES (1, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET last_cursor = excluded.last_cursor,
                                      atualizado_em = datetime('now')
        """,
        (cursor,),
    )
    conn.commit()


def get_attempts_count(conn: sqlite3.Connection, decisao_id: str) -> int:
    row = conn.execute(
        "SELECT attempts FROM flowpcp_decisoes_mapping WHERE decisao_id = ?", (decisao_id,)
    ).fetchone()
    return int(row["attempts"]) if row else 0


def register_attempt(conn: sqlite3.Connection, decisao_id: str) -> int:
    conn.execute(
        """
        INSERT INTO flowpcp_decisoes_mapping (decisao_id, attempts)
        VALUES (?, 1)
        ON CONFLICT(decisao_id) DO UPDATE SET attempts = attempts + 1,
                                              atualizado_em = datetime('now')
        """,
        (decisao_id,),
    )
    conn.commit()
    return get_attempts_count(conn, decisao_id)


def mark_reconciliada(conn: sqlite3.Connection, decisao_id: str, acao: str) -> None:
    conn.execute(
        """
        INSERT INTO flowpcp_decisoes_mapping (decisao_id, acao_executada, reconciliado_em, atualizado_em)
        VALUES (?, ?, datetime('now'), datetime('now'))
        ON CONFLICT(decisao_id) DO UPDATE SET acao_executada = excluded.acao_executada,
                                              reconciliado_em = datetime('now'),
                                              atualizado_em = datetime('now')
        """,
        (decisao_id, acao),
    )
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_repo.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/persistence/schema_env.py app/persistence/flowpcp_repo.py tests/test_flowpcp_repo.py
git commit -m "feat(flowpcp): persistência local (cursor + contador de tentativas)"
```

---

### Task 5: UPDATE da data de entrega no FIRE

**Files:**
- Modify: `app/erp/queries.py` (acrescentar `UPDATE_DT_ENTREGA`)
- Create: `app/erp/fire_update.py`
- Test: `tests/test_flowpcp_fire_update.py`

**Interfaces:**
- Consumes: `app/erp/queries.py` (`FIND_CLIENT_BY_CNPJ`, novo `UPDATE_DT_ENTREGA`); conexão Firebird (driver com `.cursor()`, `.commit()`, `.rollback()`).
- Produces: `update_dt_entrega(conn, *, pedido_cliente, cliente_cnpj, new_date_iso, timezone="America/Sao_Paulo") -> int` (rows afetadas; 0 se cliente não achado ou pedido não localizado).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_fire_update.py
from __future__ import annotations

from unittest.mock import MagicMock

from app.erp.fire_update import update_dt_entrega


def _conn(client_row, update_rowcount):
    cur = MagicMock()
    cur.fetchone.return_value = client_row  # FIND_CLIENT_BY_CNPJ result
    cur.rowcount = update_rowcount
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_update_resolves_cnpj_then_updates_dt_entrega():
    conn, cur = _conn(client_row=(42, "MM AMERICANENSE"), update_rowcount=1)
    rows = update_dt_entrega(
        conn,
        pedido_cliente="AW097",
        cliente_cnpj="12.345.678/0001-90",
        new_date_iso="2026-07-17T03:00:00.000Z",
    )
    assert rows == 1
    conn.commit.assert_called_once()
    # segunda execução (o UPDATE) recebeu o CLIENTE codigo resolvido (42) e o pedido
    update_args = cur.execute.call_args_list[-1].args[1]
    assert 42 in update_args
    assert "AW097" in update_args


def test_returns_zero_when_client_not_found():
    conn, cur = _conn(client_row=None, update_rowcount=0)
    rows = update_dt_entrega(
        conn, pedido_cliente="AW097", cliente_cnpj="00000000000000",
        new_date_iso="2026-07-17T03:00:00.000Z",
    )
    assert rows == 0
    conn.commit.assert_not_called()


def test_rollback_on_error():
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (42, "MM")
    cur.execute.side_effect = [None, RuntimeError("lock")]  # SELECT ok, UPDATE falha
    conn.cursor.return_value = cur
    import pytest
    with pytest.raises(RuntimeError):
        update_dt_entrega(conn, pedido_cliente="AW097", cliente_cnpj="123",
                          new_date_iso="2026-07-17T03:00:00.000Z")
    conn.rollback.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_fire_update.py -v`
Expected: FAIL — `app.erp.fire_update` missing.

- [ ] **Step 3: Write minimal implementation**

Acrescentar em `app/erp/queries.py`:

```python
# Reconciliação FlowPCP → Fire: atualiza data de entrega do pedido.
# Chave = PEDIDO_CLIENTE (ref do varejista) + CLIENTE (FK CADASTRO.CODIGO).
UPDATE_DT_ENTREGA = """
    UPDATE CAB_VENDAS
       SET DT_ENTREGA = ?
     WHERE TRIM(PEDIDO_CLIENTE) = ?
       AND CLIENTE = ?
"""
```

```python
# app/erp/fire_update.py
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.erp.queries import FIND_CLIENT_BY_CNPJ, UPDATE_DT_ENTREGA
from app.utils.logger import logger


def _to_fire_date(new_date_iso: str, timezone: str) -> date:
    dt = datetime.fromisoformat(new_date_iso.replace("Z", "+00:00"))
    return dt.astimezone(ZoneInfo(timezone)).date()


def update_dt_entrega(
    conn,
    *,
    pedido_cliente: str,
    cliente_cnpj: str | None,
    new_date_iso: str,
    timezone: str = "America/Sao_Paulo",
) -> int:
    """Resolve o CNPJ → CADASTRO.CODIGO e atualiza CAB_VENDAS.DT_ENTREGA.
    Devolve rows afetadas (0 = cliente não achado ou pedido não localizado)."""
    fire_date = _to_fire_date(new_date_iso, timezone)
    cnpj_clean = re.sub(r"\D", "", cliente_cnpj or "")
    cur = conn.cursor()
    try:
        cur.execute(FIND_CLIENT_BY_CNPJ, (cnpj_clean,))
        client = cur.fetchone()
        if not client:
            logger.warning(f"fire_update: cliente CNPJ={cnpj_clean} não achado no CADASTRO")
            return 0
        cliente_codigo = client[0]
        cur.execute(UPDATE_DT_ENTREGA, (fire_date, pedido_cliente, cliente_codigo))
        rows = cur.rowcount
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_fire_update.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/erp/queries.py app/erp/fire_update.py tests/test_flowpcp_fire_update.py
git commit -m "feat(flowpcp): update_dt_entrega no CAB_VENDAS (schema real PEDIDO_CLIENTE+CLIENTE)"
```

> **Antes do cutover (não bloqueia o plano):** validar `CAB_VENDAS.DT_ENTREGA` e o tipo da coluna contra um dump fresco da MM via `tools/explore_firebird.py`. O teste usa mock; a verdade do schema confirma-se com dump real.

---

### Task 6: Orquestração — processar_decisao + poll_decisoes_once

**Files:**
- Create: `app/integrations/flowpcp/poll_decisoes.py`
- Test: `tests/test_flowpcp_poll.py`

**Interfaces:**
- Consumes: `FlowPCPClient` (Task 2), `DecisaoFlowPCP`/`ConfirmarReconciliacaoRequest`/`AcaoReconciliacao` (Task 1), `flowpcp_repo` (Task 4), `update_dt_entrega` (Task 5), `FlowPCPConfig` (Task 3).
- Produces: `processar_decisao(decisao, *, client, fire_conn, conn, config) -> None`; `poll_decisoes_once(*, client, fire_conn, conn, config) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_poll.py
from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.poll_decisoes import processar_decisao
from app.integrations.flowpcp.schema import DecisaoFlowPCP
from app.persistence import flowpcp_repo
from app.persistence.schema_env import TABLES_SQL

CFG = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")


def _decisao(**over):
    base = dict(
        id="dec-1", pedido_erp="AW097", cliente_cnpj="123",
        nome_cliente="MM", prazo_entrega_original="2026-07-10T03:00:00.000Z",
        prazo_pactuado="2026-07-17T03:00:00.000Z", status="em_pool",
        motivo_decisao="negociado", atualizado_em="2026-06-22T14:00:00.000Z",
    )
    base.update(over)
    return DecisaoFlowPCP(**base)


def test_rejeitado_confirma_cancelamento_sem_tocar_fire(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    fire = MagicMock()
    called = monkeypatch.setattr  # no-op alias to keep import tidy
    processar_decisao(_decisao(status="rejeitado", prazo_pactuado=None),
                      client=client, fire_conn=fire, conn=tmp_env_db, config=CFG)
    acao = client.confirmar_reconciliacao.call_args.args[1].acao.value
    assert acao == "cancelamento_pendente_manual"
    fire.cursor.assert_not_called()


def test_sem_mudanca_confirma_sem_acao(tmp_env_db):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    processar_decisao(_decisao(prazo_pactuado="2026-07-10T03:00:00.000Z"),
                      client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "sem_acao_necessaria"


def test_data_nova_atualiza_fire_e_confirma(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: 1,
    )
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "data_atualizada"


def test_dry_run_nao_chama_update(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    calls = {"n": 0}
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or 1,
    )
    dry = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm", dry_run=True)
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=dry)
    assert calls["n"] == 0
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "data_atualizada"


def test_nao_encontrado_incrementa_e_confirma_apos_5(tmp_env_db, monkeypatch):
    tmp_env_db.executescript(TABLES_SQL)
    client = MagicMock()
    monkeypatch.setattr(
        "app.integrations.flowpcp.poll_decisoes.update_dt_entrega",
        lambda *a, **k: 0,
    )
    for _ in range(4):
        processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert client.confirmar_reconciliacao.call_count == 0  # ainda tentando
    processar_decisao(_decisao(), client=client, fire_conn=MagicMock(), conn=tmp_env_db, config=CFG)
    assert client.confirmar_reconciliacao.call_args.args[1].acao.value == "pedido_nao_encontrado_no_fire"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_poll.py -v`
Expected: FAIL — `app.integrations.flowpcp.poll_decisoes` missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/poll_decisoes.py
from __future__ import annotations

import sqlite3

from app.erp.fire_update import update_dt_entrega
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import FlowPCPConfig
from app.integrations.flowpcp.schema import (
    AcaoReconciliacao,
    ConfirmarReconciliacaoRequest,
    DecisaoFlowPCP,
)
from app.persistence import flowpcp_repo
from app.utils.logger import logger

_MAX_NAO_ENCONTRADO = 5


def _confirmar(client: FlowPCPClient, conn, decisao_id: str, acao: AcaoReconciliacao,
               *, fire_id_externo: str | None = None, observacoes: str | None = None) -> None:
    client.confirmar_reconciliacao(
        decisao_id,
        ConfirmarReconciliacaoRequest(acao=acao, fire_id_externo=fire_id_externo, observacoes=observacoes),
    )
    flowpcp_repo.mark_reconciliada(conn, decisao_id, acao.value)


def processar_decisao(decisao: DecisaoFlowPCP, *, client: FlowPCPClient, fire_conn,
                      conn: sqlite3.Connection, config: FlowPCPConfig) -> None:
    # 1. Rejeitado → cancelamento manual no Fire; Importador só alerta.
    if decisao.status == "rejeitado":
        logger.warning(f"flowpcp decisão {decisao.id} rejeitada — cancelamento manual no Fire (pedido={decisao.pedido_erp})")
        _confirmar(client, conn, decisao.id, AcaoReconciliacao.CANCELAMENTO_PENDENTE_MANUAL,
                   observacoes=decisao.motivo_decisao)
        return

    # 2. Aprovado, mas data não mudou.
    if decisao.prazo_pactuado is None or decisao.prazo_pactuado == decisao.prazo_entrega_original:
        _confirmar(client, conn, decisao.id, AcaoReconciliacao.SEM_ACAO_NECESSARIA)
        return

    # 3. Aprovado com data nova → UPDATE no Fire (ou dry_run).
    if config.dry_run:
        logger.info(f"[DRY_RUN] UPDATE DT_ENTREGA={decisao.prazo_pactuado} pedido={decisao.pedido_erp}")
        _confirmar(client, conn, decisao.id, AcaoReconciliacao.DATA_ATUALIZADA,
                   fire_id_externo=decisao.pedido_erp, observacoes="DRY_RUN (sem escrita real no Fire)")
        return

    try:
        rows = update_dt_entrega(
            fire_conn, pedido_cliente=decisao.pedido_erp, cliente_cnpj=decisao.cliente_cnpj,
            new_date_iso=decisao.prazo_pactuado, timezone=config.timezone,
        )
    except Exception as exc:  # timeout/lock — não confirma; re-tenta no próximo poll
        logger.error(f"flowpcp UPDATE Fire falhou decisao={decisao.id}: {exc}")
        return

    if rows == 0:
        attempts = flowpcp_repo.register_attempt(conn, decisao.id)
        if attempts >= _MAX_NAO_ENCONTRADO:
            logger.critical(f"flowpcp pedido {decisao.pedido_erp} não localizado no Fire após {attempts} tentativas")
            _confirmar(client, conn, decisao.id, AcaoReconciliacao.PEDIDO_NAO_ENCONTRADO_NO_FIRE,
                       observacoes=f"{attempts} tentativas")
        return

    _confirmar(client, conn, decisao.id, AcaoReconciliacao.DATA_ATUALIZADA,
               fire_id_externo=decisao.pedido_erp, observacoes=f"UPDATE OK (rows={rows})")


def poll_decisoes_once(*, client: FlowPCPClient, fire_conn, conn: sqlite3.Connection,
                       config: FlowPCPConfig) -> int:
    if not config.enabled:
        return 0
    cursor = flowpcp_repo.get_last_cursor(conn)
    resp = client.list_decisoes(cursor=cursor, limit=50)
    for decisao in resp.decisoes:
        try:
            processar_decisao(decisao, client=client, fire_conn=fire_conn, conn=conn, config=config)
        except Exception as exc:  # noqa: BLE001 — uma decisão ruim não derruba o lote
            logger.error(f"flowpcp erro processando decisão {decisao.id}: {exc}")
    if resp.proximo_cursor:
        flowpcp_repo.save_last_cursor(conn, resp.proximo_cursor)
    return len(resp.decisoes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_poll.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/poll_decisoes.py tests/test_flowpcp_poll.py
git commit -m "feat(flowpcp): orquestração de decisões (4 ramos + dry_run + contador not-found)"
```

---

### Task 7: FlowPCPExporter — push de pedido novo

**Files:**
- Create: `app/integrations/flowpcp/mapper.py`
- Create: `app/integrations/flowpcp/exporter.py`
- Test: `tests/test_flowpcp_exporter.py`

**Interfaces:**
- Consumes: `Order`/`OrderHeader`/`OrderItem` (`app.models.order`); `FlowPCPClient` (Task 2); `RecebimentoRequest`+filhos (Task 1); `outbox_repo.enqueue` (existente).
- Produces: `build_recebimento_payload(*, import_id, order, tenant_id) -> RecebimentoRequest`; `FlowPCPExporter(client, *, tenant_id).export(order, *, import_id) -> bool` (True=enviado; em falha, enfileira outbox e devolve False).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_exporter.py
from __future__ import annotations

from unittest.mock import MagicMock

from app.integrations.flowpcp.exporter import FlowPCPExporter
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.integrations.flowpcp.schema import RecebimentoRequest
from app.models.order import Order, OrderHeader, OrderItem

TENANT = "uuid-mm"


def _order():
    return Order(
        header=OrderHeader(order_number="AW097", issue_date="15/06/2026",
                           customer_name="MM", customer_cnpj="12345678000190"),
        items=[OrderItem(description="meia preta", product_code="ABC", ean="789",
                         quantity=10, unit_price=12.5, delivery_date="22/06/2026")],
    )


def test_mapper_shape():
    req = build_recebimento_payload(import_id="imp-1", order=_order(), tenant_id=TENANT)
    assert isinstance(req, RecebimentoRequest)
    assert req.externalId == "imp-1"
    assert req.pedidoNumero == "AW097"
    assert req.cliente.cnpj == "12345678000190"
    assert len(req.itens) == 1
    assert req.itens[0].descricao == "meia preta"
    assert req.itens[0].quantidade == 10


def test_export_sends_when_ok():
    client = MagicMock()
    sent = FlowPCPExporter(client, tenant_id=TENANT).export(_order(), import_id="imp-1")
    assert sent is True
    client.send_order.assert_called_once()


def test_export_enqueues_on_failure(monkeypatch):
    client = MagicMock()
    client.send_order.side_effect = RuntimeError("rede caiu")
    enq = MagicMock()
    monkeypatch.setattr("app.integrations.flowpcp.exporter.outbox_repo.enqueue", enq)
    sent = FlowPCPExporter(client, tenant_id=TENANT).export(_order(), import_id="imp-1")
    assert sent is False
    enq.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_exporter.py -v`
Expected: FAIL — módulos `mapper`/`exporter` missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/mapper.py
from __future__ import annotations

from datetime import datetime

from app.integrations.flowpcp.schema import (
    ClienteRecebimento,
    ItemRecebimento,
    OrigemRecebimento,
    RecebimentoRequest,
)
from app.models.order import Order

_IMPORTADOR_VERSAO = "1.0.0"


def _to_iso(br_date: str | None) -> str | None:
    if not br_date:
        return None
    try:
        return datetime.strptime(br_date, "%d/%m/%Y").strftime("%Y-%m-%dT00:00:00.000Z")
    except ValueError:
        return None


def build_recebimento_payload(*, import_id: str, order: Order, tenant_id: str) -> RecebimentoRequest:
    h = order.header
    itens = [
        ItemRecebimento(
            produtoCodigo=it.product_code or None,
            produtoEan=it.ean or None,
            descricao=it.description,
            quantidade=float(it.quantity),
            precoUnitario=float(it.unit_price) if it.unit_price is not None else None,
        )
        for it in order.items
    ]
    primeiro_prazo = _to_iso(order.items[0].delivery_date) if order.items else None
    return RecebimentoRequest(
        externalId=import_id,
        fornecedor=h.customer_name or "(sem fornecedor)",
        pedidoNumero=h.order_number or import_id,
        emitidoEm=_to_iso(h.issue_date) or datetime.utcnow().strftime("%Y-%m-%dT00:00:00.000Z"),
        prazoSolicitado=primeiro_prazo,
        cliente=ClienteRecebimento(nome=h.customer_name or "(sem cliente)", cnpj=h.customer_cnpj or None),
        itens=itens,
        origem=OrigemRecebimento(
            importadorVersao=_IMPORTADOR_VERSAO,
            arquivoOriginal=order.source_file or "",
            parserUsado="importador",
            confiancaParser="alta",
        ),
    )
```

```python
# app/integrations/flowpcp/exporter.py
from __future__ import annotations

import json

from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order
from app.persistence import outbox_repo
from app.utils.logger import logger

FLOWPCP_SEND_ORDER = "flowpcp_send_order"


class FlowPCPExporter:
    def __init__(self, client: FlowPCPClient, *, tenant_id: str) -> None:
        self._client = client
        self._tenant_id = tenant_id

    def export(self, order: Order, *, import_id: str) -> bool:
        req = build_recebimento_payload(import_id=import_id, order=order, tenant_id=self._tenant_id)
        try:
            self._client.send_order(req, idempotency_key=f"send-{import_id}")
            return True
        except Exception as exc:  # noqa: BLE001 — falha vira retry via outbox
            logger.warning(f"flowpcp send_order falhou (import={import_id}): {exc} — enfileirando outbox")
            outbox_repo.enqueue(
                target="flowpcp",
                kind=FLOWPCP_SEND_ORDER,
                idempotency_key=f"send-{import_id}",
                payload=json.dumps(req.model_dump(by_alias=True)),
                import_id=import_id,
            )
            return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_exporter.py -v`
Expected: PASS (3 passed). Se `outbox_repo.enqueue` tiver assinatura diferente (conferir `app/persistence/outbox_repo.py:78`), ajustar a chamada e o teste pros nomes reais de parâmetro (`target`, `kind`, `idempotency_key`, `payload`, `import_id`).

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/mapper.py app/integrations/flowpcp/exporter.py tests/test_flowpcp_exporter.py
git commit -m "feat(flowpcp): exporter de pedido novo (Order→recebimento) + outbox em falha"
```

---

### Task 8: Wiring no worker (scheduler + drain_outbox)

**Files:**
- Modify: `app/worker/scheduler.py` (registrar job de poll quando FlowPCP habilitado por ambiente)
- Modify: `app/worker/jobs/drain_outbox.py` (despachar `kind="flowpcp_send_order"`)
- Create: `app/worker/jobs/poll_flowpcp.py`
- Test: `tests/test_flowpcp_worker_wiring.py`

**Interfaces:**
- Consumes: `poll_decisoes_once` (Task 6), `load_flowpcp_config` (Task 3), `FlowPCPClient` (Task 2); padrão de iteração de ambientes do `poll_fire.py`/`scan_environments.py`.
- Produces: `run_poll_flowpcp()` (entry do job, itera ambientes FlowPCP-on); dispatch de outbox `flowpcp_send_order`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_worker_wiring.py
from __future__ import annotations

from unittest.mock import MagicMock

import app.worker.jobs.poll_flowpcp as job


def test_run_poll_skips_disabled_envs(monkeypatch):
    monkeypatch.setattr(job, "_list_flowpcp_envs", lambda: [])  # nenhum ambiente ligado
    called = MagicMock()
    monkeypatch.setattr(job, "poll_decisoes_once", called)
    job.run_poll_flowpcp()
    called.assert_not_called()


def test_run_poll_invokes_once_per_enabled_env(monkeypatch):
    from app.integrations.flowpcp.config import FlowPCPConfig
    cfg = FlowPCPConfig(enabled=True, base_url="x", service_token="t", tenant_id="mm")
    monkeypatch.setattr(job, "_list_flowpcp_envs", lambda: [("mm", cfg)])
    monkeypatch.setattr(job, "_open_env_conn", lambda slug: MagicMock())
    monkeypatch.setattr(job, "_open_fire_conn", lambda slug: MagicMock())
    monkeypatch.setattr(job, "_build_client", lambda cfg: MagicMock())
    called = MagicMock(return_value=0)
    monkeypatch.setattr(job, "poll_decisoes_once", called)
    job.run_poll_flowpcp()
    assert called.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_worker_wiring.py -v`
Expected: FAIL — `app.worker.jobs.poll_flowpcp` missing.

- [ ] **Step 3: Write minimal implementation**

```python
# app/worker/jobs/poll_flowpcp.py
from __future__ import annotations

from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import FlowPCPConfig, load_flowpcp_config
from app.integrations.flowpcp.poll_decisoes import poll_decisoes_once
from app.utils.logger import logger

# NOTE: as 4 funções `_*` abaixo encapsulam o acesso multi-ambiente do worker.
# Implementar conforme o padrão de `app/worker/jobs/poll_fire.py` /
# `scan_environments.py` (environments_repo + router + db per-env + firebird_config).


def _list_flowpcp_envs() -> list[tuple[str, FlowPCPConfig]]:
    """Devolve (slug, config) só dos ambientes com flowpcp.enabled=true."""
    from app.persistence import environments_repo

    out: list[tuple[str, FlowPCPConfig]] = []
    for env in environments_repo.list_active():
        cfg = load_flowpcp_config(env.get("config") or {})
        if cfg.enabled:
            out.append((env["slug"], cfg))
    return out


def _open_env_conn(slug: str):
    from app.persistence import db, router

    return db.connect_env(router.env_db_path(slug))


def _open_fire_conn(slug: str):
    from app.erp.connection import FirebirdConnection
    from app.firebird_config import load as load_fb

    return FirebirdConnection().connect_with_config(load_fb(slug))


def _build_client(cfg: FlowPCPConfig) -> FlowPCPClient:
    return FlowPCPClient(base_url=cfg.base_url, service_token=cfg.service_token,
                         tenant_id=cfg.tenant_id, timeout=cfg.request_timeout_s)


def run_poll_flowpcp() -> None:
    for slug, cfg in _list_flowpcp_envs():
        try:
            conn = _open_env_conn(slug)
            fire_ctx = _open_fire_conn(slug)
            with fire_ctx as fire_conn:
                n = poll_decisoes_once(client=_build_client(cfg), fire_conn=fire_conn,
                                       conn=conn, config=cfg)
            logger.info(f"flowpcp poll env={slug} decisoes={n}")
        except Exception as exc:  # noqa: BLE001 — um ambiente ruim não derruba os outros
            logger.error(f"flowpcp poll env={slug} falhou: {exc}")
```

Registrar no scheduler — em `app/worker/scheduler.py`, ao lado dos jobs existentes (`poll_fire`, `drain_outbox`):

```python
from app.worker.jobs.poll_flowpcp import run_poll_flowpcp

scheduler.add_job(
    run_poll_flowpcp, "interval", seconds=30,
    id="poll_flowpcp", max_instances=1, coalesce=True, replace_existing=True,
)
```

Em `app/worker/jobs/drain_outbox.py`, no dispatch por `kind`, acrescentar o ramo FlowPCP (espelhar o handler existente do gestor):

```python
elif row.kind == "flowpcp_send_order":
    from app.integrations.flowpcp.client import FlowPCPClient
    from app.integrations.flowpcp.config import load_flowpcp_config
    from app.integrations.flowpcp.schema import RecebimentoRequest

    cfg = load_flowpcp_config(_env_config_for(row))  # mesmo helper que o gestor usa
    client = FlowPCPClient(base_url=cfg.base_url, service_token=cfg.service_token, tenant_id=cfg.tenant_id)
    client.send_order(RecebimentoRequest.model_validate_json(row.payload), idempotency_key=row.idempotency_key)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_flowpcp_worker_wiring.py -v`
Expected: PASS (2 passed). Os helpers `_open_env_conn`/`_open_fire_conn`/`environments_repo.list_active` devem casar com os nomes reais do repo — conferir contra `poll_fire.py` e ajustar; os testes mockam esses helpers, então a suíte fica verde mesmo antes do fio real com ambientes.

- [ ] **Step 5: Commit**

```bash
git add app/worker/jobs/poll_flowpcp.py app/worker/scheduler.py app/worker/jobs/drain_outbox.py tests/test_flowpcp_worker_wiring.py
git commit -m "feat(flowpcp): wiring no worker (job poll 30s + drain_outbox flowpcp_send_order)"
```

---

## Fora deste plano (follow-ups explícitos)

- **Endpoints `/decisoes` e `/confirmar-reconciliacao` no FlowPCP** (lado Flow) — pré-requisito da integração viva. Frente separada (spec irmã em pcp-app). A "Migration 0083 — colunas de reconciliação" da spec irmã colide com `0083_clientes_unique_cnpj` já aplicada; renumerar pra 0084+.
- **UI de config (toggle Gateway FlowPCP + secret_store)** — §6 da spec. Não bloqueia a lógica; token entra via config dict por ora.
- **Validar `CAB_VENDAS.DT_ENTREGA` contra dump real da MM** via `tools/explore_firebird.py` antes do cutover.
- **Smoke E2E real** contra `flowpcp.fly.dev` com pedido descartável (depois que o lado Flow existir).
- **Wire do exporter no fim do pipeline de parse** (`main.py` / web commit) — chamar `FlowPCPExporter.export` em paralelo ao fluxo XLS→Fire, só no ambiente MM.

## Self-review

- **Cobertura da spec:** push (Task 7), poll (Task 6), reconciliação Fire (Task 5), client (Task 2), persistência/cursor/tentativas (Task 4), 4 ramos + dry_run + not-found-5x (Task 6), config per-env (Task 3), wiring 30s + outbox (Task 8). UI e endpoints-Flow explicitamente em follow-up.
- **Auth:** X-Service-Token em todo o client (Task 2) — consistente com F.5 implementado, não Bearer.
- **Schema FIRE:** DT_ENTREGA + PEDIDO_CLIENTE+CLIENTE (Task 5) — schema real do queries.py, não o chute da spec.
- **Tipos consistentes:** `AcaoReconciliacao`/`ConfirmarReconciliacaoRequest`/`DecisaoFlowPCP` (Task 1) usados igual em 2,6; `update_dt_entrega` assinatura idêntica em 5 e 6; `poll_decisoes_once`/`processar_decisao` idênticos em 6 e 8.
