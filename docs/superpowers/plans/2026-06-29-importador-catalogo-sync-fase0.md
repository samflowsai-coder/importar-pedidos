# Importador — Sync de Catálogo Fire→FlowPCP (Fase 0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Importador extrai o catálogo de produtos do Fire (Firebird) e empurra pro FlowPCP em modo dry-run, recebendo o relatório de reconciliação — o "primeiro passo" da spec (§12), que decide a chave e o desenho do sync.

**Architecture:** Reusa a ponte Fatia G (mesmo `FlowPCPClient`/`OutboundClient`, `X-Service-Token`, config per-ambiente). Novo extrator lê `PRODUTOS` do Fire via `to_fb_config`; um mapper monta o payload `catalogo.produtos.v1`; um novo método no client faz `POST /api/portal-pedidos/catalogo`; um orquestrador junta tudo por slug; um CLI roda a Fase 0 em dry-run e imprime o relatório.

**Tech Stack:** Python 3.11+, pydantic v2, firebird-driver (embedded FB 5.0), httpx (via OutboundClient), pytest.

## Escopo e fronteira

- **Esta fatia = lado Importador (este repo).** Produz o cliente Python + extrator + orquestrador + CLeI, testáveis aqui contra mocks.
- **Lado Flow (pcp-app) é plano SEPARADO** e pré-requisito do *run real* (não da implementação/teste): endpoint `POST /api/portal-pedidos/catalogo`, schema Zod `catalogo.produtos.v1`, tabela `produtos_fire_staging`, job de reconciliação, surface de erro. **Migration exige aprovação explícita do Samuel** (regra da casa). O Importador implementa contra o contrato canônico definido lá.
- **Fase 0 só (dry-run).** Não promove nada. Fase 1 (promote) e Fase 2 (incremental/changelog) ficam fora.

## Kits (par × kit) — realidade MM

Validado no Fire (2026-06-29): o cliente final da MM **compra KIT**, mas a produção é por **par de meia** (o componente). O Flow controla produção no nível do par. Estrutura:
- `PRODUTOS` tem **tanto os kits** (ex. `SEQ 3381` "KIT C/5 ...") **quanto os pares** (ex. `SEQ 3170` "BRANCO COM BORDO") como linhas → o sync flat da Fase 0 **já traz os dois pro Flow** (o extract lê `PRODUTOS` inteiro). Garante "os produtos estão no Flow".
- `PRODUTOS_KIT` (`CODPRODUTO_PAI`=kit, `CODPRODUTO`=componente, `QTD`): **435 linhas, 215 kits**, sem kit-dentro-de-kit. Ex.: KIT C/5 (`SEQ 3381`) → 5 pares (`3170/3172/3173/3174/3175`), QTD 1 cada.

**Fase 0 (este plano):** sincroniza a **identidade flat** (kits + pares como produtos), com `tipo` (`kit`/`simples`) derivado de `PRODUTOS_KIT` marcando quem é kit.
**Fase futura (NÃO neste plano):** sincronizar a **composição do kit** (`PRODUTOS_KIT` → BOM no Flow) e o **processo de montagem** (explodir pedido de KIT → produção dos pares).
> ⚠️ **Tensão com a spec a resolver na fase de kits:** a spec §3 trata BOM (`produto_componentes`) como **enriquecimento Flow-owned — sync nunca escreve**. Mas na MM a composição do kit **vem do Fire** (`PRODUTOS_KIT`). Decidir nessa fase: BOM do kit é Fire-owned (sync escreve) ou Flow-curado? A spec assumiu Flow; o dado real diz Fire.

## Global Constraints

- **Python `>=3.11`** — `X | None` e `match` liberados (pyproject exige). Venv local é 3.11+.
- **pydantic v2** — todos os modelos de dados.
- **Auth = header `X-Service-Token`** (NÃO Bearer) + `X-Tenant-Id`, exatamente como `/recebimento` (padrão Fatia G). Reusar o `FlowPCPClient` existente.
- **Schema do payload = string literal `"catalogo.produtos.v1"`** (campo `schema`, com alias — pydantic não deixa `schema` como nome de campo).
- **dryRun default `True`** na Fase 0 — nunca promove.
- **Wire em camelCase** (`fireProdutoId`, `dryRun`, `fullSync`, `importadorVersao`, `extraidoEm`), igual `RecebimentoRequest`/`ItemRecebimento` (usar `Field(alias=...)` + `populate_by_name`).
- **Achados da validação do Fire (MM, 2026-06-29) — verdade de campo, copiar no código/comentário:**
  - PK durável = **`PRODUTOS.SEQ`** (integer NOT NULL, único, imutável). **O `codigo` que o cliente usa É O SEQUENCIAL `SEQ`** (confirmado Samuel 2026-06-29) → `fireProdutoId = codigo = str(SEQ)` (coincidem → caso ideal, sem código instável). `CODPROD_ALTERN` é referência secundária do fornecedor (grade-code não-único, 121 dups) → **NÃO usar**.
  - `nome` = `DESCRICAO`; `unidade` = `UNIDADE` (100% preenchido).
  - `ean` = `CODIGO_EAN13` (47% preenchido, 85 dups → atributo/enriquecimento, não chave). `GTIN` é 0% → ignorar.
  - `ativo` = `BLOQUEADO` ≠ `'Sim'` (valores reais: `'Nao'` 3250, `'Sim'` 171).
  - `tipo` = **derivado de `PRODUTOS_KIT`**: `'kit'` se o `SEQ` aparece como `CODPRODUTO_PAI` (215 kits), senão `'simples'`. (`CODTIPOPROD` do Fire é inútil — 3407/3421 NULL — não usar.)
  - Volume = 3421 (3250 ativos, 215 kits) → **lote único `fullSync=true`**, sem paginação (YAGNI; paginar fica pra quando crescer).

## File Structure

- `app/integrations/flowpcp/catalogo_schema.py` (criar) — modelos pydantic do contrato: `CatalogoProdutoItem`, `CatalogoOrigem`, `CatalogoRequest`, `CatalogoReconciliacaoResponse`.
- `app/erp/queries.py` (modificar) — adicionar `LIST_PRODUTOS_CATALOGO`.
- `app/erp/catalog_extract.py` (criar) — `ProdutoFireDTO` (dataclass) + `extract_produtos(fire_conn) -> list[ProdutoFireDTO]`.
- `app/integrations/flowpcp/catalogo_mapper.py` (criar) — `build_catalogo_request(dtos, *, dry_run, full_sync, importador_versao, extraido_em) -> CatalogoRequest`.
- `app/integrations/flowpcp/client.py` (modificar) — método `send_catalogo(self, request) -> CatalogoReconciliacaoResponse` + constante `_CATALOGO_PATH`.
- `app/integrations/flowpcp/catalogo_sync.py` (criar) — `run_catalogo_sync(slug, *, dry_run=True, full_sync=True) -> CatalogoReconciliacaoResponse | None`.
- `tools/sync_catalogo_fire.py` (criar) — CLI da Fase 0 (dry-run), imprime o relatório.
- Testes: `tests/test_flowpcp_catalogo_schema.py`, `tests/test_catalog_extract.py`, `tests/test_flowpcp_catalogo_mapper.py`, `tests/test_flowpcp_catalogo_client.py`, `tests/test_catalogo_sync.py`.

---

### Task 1: Contrato pydantic (`catalogo.produtos.v1`)

**Files:**
- Create: `app/integrations/flowpcp/catalogo_schema.py`
- Test: `tests/test_flowpcp_catalogo_schema.py`

**Interfaces:**
- Produces:
  - `CatalogoProdutoItem(fireProdutoId:str, codigo:str|None, nome:str, unidade:str|None, ean:str|None, ativo:bool, tipo:str|None=None)` — `populate_by_name`, serializa camelCase.
  - `CatalogoOrigem(importadorVersao:str, extraidoEm:str)`.
  - `CatalogoRequest(schema_:str="catalogo.produtos.v1" alias "schema", dryRun:bool, fullSync:bool, itens:list[CatalogoProdutoItem], origem:CatalogoOrigem)`.
  - `CatalogoReconciliacaoResponse` — campos do relatório (todos default 0/None; `model_config = extra="allow"` porque o contrato é dono do Flow): `match_limpo:int=0, ambiguo:int=0, flow_only:int=0, fire_only:int=0, criados:int=0, atualizados:int=0, inalterados:int=0, desativados:int=0, erros:int=0, fire_pk_presente:bool|None=None`.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_flowpcp_catalogo_schema.py
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoReconciliacaoResponse,
    CatalogoRequest,
)


def test_item_serializa_camelcase_e_aceita_snake_na_entrada():
    item = CatalogoProdutoItem(
        fireProdutoId="3566", codigo="5035G", nome="KALLAN 39/44 SP LISA MESCLA",
        unidade="PC", ean=None, ativo=True,
    )
    dumped = item.model_dump(by_alias=True)
    assert dumped["fireProdutoId"] == "3566"
    assert dumped["tipo"] is None  # Fire não tem tipo
    assert dumped["ativo"] is True


def test_request_usa_schema_alias_e_default_v1():
    req = CatalogoRequest(
        dryRun=True, fullSync=True,
        itens=[CatalogoProdutoItem(fireProdutoId="1", codigo=None, nome="X",
                                   unidade=None, ean=None, ativo=True)],
        origem=CatalogoOrigem(importadorVersao="1.0.0", extraidoEm="2026-06-29T00:00:00Z"),
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "catalogo.produtos.v1"
    assert body["dryRun"] is True and body["fullSync"] is True
    assert body["itens"][0]["nome"] == "X"


def test_response_tolera_campos_extra_do_flow():
    resp = CatalogoReconciliacaoResponse.model_validate(
        {"match_limpo": 10, "fire_only": 3400, "campo_novo_do_flow": "ok"}
    )
    assert resp.match_limpo == 10
    assert resp.fire_only == 3400
    assert resp.criados == 0  # default
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_schema.py -v`
Expected: FAIL com `ModuleNotFoundError: app.integrations.flowpcp.catalogo_schema`

- [ ] **Step 3: Implementar o módulo**

```python
# app/integrations/flowpcp/catalogo_schema.py
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CatalogoProdutoItem(BaseModel):
    """Item de identidade do produto (Fire é dono). camelCase no wire."""
    model_config = ConfigDict(populate_by_name=True)

    fireProdutoId: str = Field(alias="fireProdutoId")  # noqa: N815 — PK imutável do Fire (SEQ)
    codigo: str | None = None  # CODPROD_ALTERN — atributo, NÃO chave (grade-code, dups)
    nome: str
    unidade: str | None = None
    ean: str | None = None
    ativo: bool
    tipo: str | None = None  # Fire não fornece (CODTIPOPROD inútil) → sempre None


class CatalogoOrigem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    importadorVersao: str  # noqa: N815
    extraidoEm: str  # noqa: N815 — ISO8601


class CatalogoRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="catalogo.produtos.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[CatalogoProdutoItem]
    origem: CatalogoOrigem


class CatalogoReconciliacaoResponse(BaseModel):
    """Relatório devolvido pelo Flow (dry-run ou apply). O contrato é dono do
    Flow → tolera campos extras (amostras/buckets) sem quebrar o parse."""
    model_config = ConfigDict(extra="allow")

    match_limpo: int = 0
    ambiguo: int = 0
    flow_only: int = 0
    fire_only: int = 0
    criados: int = 0
    atualizados: int = 0
    inalterados: int = 0
    desativados: int = 0
    erros: int = 0
    fire_pk_presente: bool | None = None
```

- [ ] **Step 4: Rodar o teste e ver passar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_schema.py -v`
Expected: PASS (3 testes)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_schema.py tests/test_flowpcp_catalogo_schema.py
git commit -m "feat(flowpcp): contrato pydantic catalogo.produtos.v1"
```

---

### Task 2: Extrator do catálogo do Fire

**Files:**
- Modify: `app/erp/queries.py` (adicionar `LIST_PRODUTOS_CATALOGO` ao fim da seção de business queries)
- Create: `app/erp/catalog_extract.py`
- Test: `tests/test_catalog_extract.py`

**Interfaces:**
- Consumes: uma conexão Firebird (`fire_conn`) com `.cursor()` (DB-API). Em produção vem de `FirebirdConnection().connect_with_config(to_fb_config(env))`.
- Produces:
  - `@dataclass(frozen=True) ProdutoFireDTO(fire_produto_id:str, codigo:str|None, nome:str, unidade:str|None, ean:str|None, ativo:bool)`
  - `extract_produtos(fire_conn) -> list[ProdutoFireDTO]` — roda `LIST_PRODUTOS_CATALOGO`, mapeia posicional `(SEQ, CODPROD_ALTERN, DESCRICAO, UNIDADE, CODIGO_EAN13, BLOQUEADO)`.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_catalog_extract.py
from app.erp.catalog_extract import ProdutoFireDTO, extract_produtos


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql):
        self.executed = sql

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self):
        return self._cur


def test_extract_codigo_seq_e_tipo_kit():
    rows = [
        # (SEQ, DESCRICAO, UNIDADE, CODIGO_EAN13, BLOQUEADO, IS_KIT)
        (3381, "KIT C/5 BRANCO", "PC", None, "Nao", 1),
        (3170, "BRANCO COM BORDO", "PC ", None, "Nao", 0),
        (171, "PROD BLOQUEADO", "PC", "7891234567890", "Sim", 0),
        (10, "SEM EAN NEM UNIDADE", None, "  ", "Nao", 0),
    ]
    out = extract_produtos(_FakeConn(rows))
    # codigo == fire_produto_id == str(SEQ); kit detectado via IS_KIT
    assert out[0] == ProdutoFireDTO(
        fire_produto_id="3381", codigo="3381", nome="KIT C/5 BRANCO",
        unidade="PC", ean=None, ativo=True, tipo="kit",
    )
    assert out[1].tipo == "simples"
    assert out[2].ativo is False  # BLOQUEADO='Sim'
    # strings em branco viram None
    assert out[3].ean is None and out[3].unidade is None
    assert out[3].codigo == "10"


def test_extract_vazio():
    assert extract_produtos(_FakeConn([])) == []
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `.venv/bin/pytest tests/test_catalog_extract.py -v`
Expected: FAIL com `ModuleNotFoundError: app.erp.catalog_extract`

- [ ] **Step 3: Adicionar a query**

Adicionar ao fim de `app/erp/queries.py`:

```python
# ── Catálogo de produtos (sync Fire→Flow, Fase 0) ─────────────────────────────
# Subconjunto de IDENTIDADE de PRODUTOS. SEQ é a PK durável (imutável) E o código
# que o cliente usa (codigo = str(SEQ)). BLOQUEADO ∈ {'Sim','Nao'} → ativo =
# (BLOQUEADO <> 'Sim'). IS_KIT = 1 se o SEQ é pai em PRODUTOS_KIT (215 kits) →
# tipo 'kit'/'simples'. CODPROD_ALTERN ignorado (grade-code não-único).
LIST_PRODUTOS_CATALOGO = """
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
           CASE WHEN EXISTS (
               SELECT 1 FROM PRODUTOS_KIT K WHERE K.CODPRODUTO_PAI = P.SEQ
           ) THEN 1 ELSE 0 END AS IS_KIT
    FROM PRODUTOS P
    ORDER BY P.SEQ
"""
```

- [ ] **Step 4: Implementar o extrator**

```python
# app/erp/catalog_extract.py
from __future__ import annotations

from dataclasses import dataclass

from app.erp.queries import LIST_PRODUTOS_CATALOGO


@dataclass(frozen=True)
class ProdutoFireDTO:
    fire_produto_id: str          # str(SEQ) — PK durável imutável
    codigo: str                   # str(SEQ) — o código usado é o sequencial
    nome: str                     # DESCRICAO
    unidade: str | None           # UNIDADE
    ean: str | None               # CODIGO_EAN13
    ativo: bool                   # BLOQUEADO <> 'Sim'
    tipo: str                     # 'kit' | 'simples' (derivado de PRODUTOS_KIT)


def _clean(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def extract_produtos(fire_conn) -> list[ProdutoFireDTO]:
    """Lê o subconjunto de identidade de PRODUTOS do Fire. Read-only.
    codigo = fire_produto_id = str(SEQ) (o cliente usa o sequencial).
    tipo: 'kit' se o SEQ é pai em PRODUTOS_KIT (IS_KIT=1), senão 'simples'."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_CATALOGO)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: list[ProdutoFireDTO] = []
    for seq, desc, uni, ean, bloqueado, is_kit in rows:
        seq_s = str(seq)
        out.append(
            ProdutoFireDTO(
                fire_produto_id=seq_s,
                codigo=seq_s,
                nome=_clean(desc) or "",
                unidade=_clean(uni),
                ean=_clean(ean),
                ativo=(str(bloqueado or "").strip().lower() != "sim"),
                tipo=("kit" if is_kit else "simples"),
            )
        )
    return out
```

- [ ] **Step 5: Rodar o teste e ver passar**

Run: `.venv/bin/pytest tests/test_catalog_extract.py -v`
Expected: PASS (2 testes)

- [ ] **Step 6: Commit**

```bash
git add app/erp/queries.py app/erp/catalog_extract.py tests/test_catalog_extract.py
git commit -m "feat(erp): extrator de catálogo PRODUTOS do Fire (identidade)"
```

---

### Task 3: Mapper DTO → payload do contrato

**Files:**
- Create: `app/integrations/flowpcp/catalogo_mapper.py`
- Test: `tests/test_flowpcp_catalogo_mapper.py`

**Interfaces:**
- Consumes: `list[ProdutoFireDTO]` (Task 2), `CatalogoRequest`/`CatalogoProdutoItem`/`CatalogoOrigem` (Task 1).
- Produces: `build_catalogo_request(dtos:list[ProdutoFireDTO], *, dry_run:bool, full_sync:bool, importador_versao:str, extraido_em:str) -> CatalogoRequest`.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_flowpcp_catalogo_mapper.py
from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request


def test_build_request_mapeia_itens_e_origem():
    dtos = [
        ProdutoFireDTO("3381", "3381", "KIT C/5", "PC", None, True, "kit"),
        ProdutoFireDTO("3170", "3170", "BRANCO COM BORDO", "PC", "789", False, "simples"),
    ]
    req = build_catalogo_request(
        dtos, dry_run=True, full_sync=True,
        importador_versao="1.0.0", extraido_em="2026-06-29T12:00:00Z",
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "catalogo.produtos.v1"
    assert body["dryRun"] is True and body["fullSync"] is True
    assert len(body["itens"]) == 2
    assert body["itens"][0]["fireProdutoId"] == "3381"
    assert body["itens"][0]["codigo"] == "3381"
    assert body["itens"][0]["tipo"] == "kit"
    assert body["itens"][1]["ativo"] is False
    assert body["origem"]["importadorVersao"] == "1.0.0"
    assert body["origem"]["extraidoEm"] == "2026-06-29T12:00:00Z"
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_mapper.py -v`
Expected: FAIL com `ModuleNotFoundError: app.integrations.flowpcp.catalogo_mapper`

- [ ] **Step 3: Implementar o mapper**

```python
# app/integrations/flowpcp/catalogo_mapper.py
from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def build_catalogo_request(
    dtos: list[ProdutoFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
) -> CatalogoRequest:
    itens = [
        CatalogoProdutoItem(
            fireProdutoId=d.fire_produto_id,
            codigo=d.codigo,
            nome=d.nome,
            unidade=d.unidade,
            ean=d.ean,
            ativo=d.ativo,
            tipo=d.tipo,
        )
        for d in dtos
    ]
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=itens,
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
    )
```

- [ ] **Step 4: Rodar o teste e ver passar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_mapper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_mapper.py tests/test_flowpcp_catalogo_mapper.py
git commit -m "feat(flowpcp): mapper DTO->payload catalogo.produtos.v1"
```

---

### Task 4: `FlowPCPClient.send_catalogo`

**Files:**
- Modify: `app/integrations/flowpcp/client.py` (constante `_CATALOGO_PATH` perto de `_DECISOES_PATH`; método `send_catalogo`)
- Test: `tests/test_flowpcp_catalogo_client.py`

**Interfaces:**
- Consumes: `CatalogoRequest` (Task 1). O `OutboundClient` injetável já existe no construtor do `FlowPCPClient`.
- Produces: `FlowPCPClient.send_catalogo(self, request: CatalogoRequest) -> CatalogoReconciliacaoResponse` — `POST /api/portal-pedidos/catalogo`, body `model_dump(by_alias=True)`, idempotency-key derivada (`catalogo-<dryRun>-<n_itens>`). Erros HTTP → `FlowPCPClientError`.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_flowpcp_catalogo_client.py
import pytest

from app.integrations.flowpcp.catalogo_schema import (
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError


class _Resp:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


class _FakeOutbound:
    def __init__(self, resp):
        self._resp = resp
        self.last_path = None
        self.last_json = None
        self.last_key = None

    def post_json(self, path, *, json, idempotency_key):
        self.last_path = path
        self.last_json = json
        self.last_key = idempotency_key
        return self._resp

    def close(self):
        pass


def _req():
    return CatalogoRequest(
        dryRun=True, fullSync=True,
        itens=[CatalogoProdutoItem(fireProdutoId="1", codigo="A", nome="X",
                                   unidade="PC", ean=None, ativo=True)],
        origem=CatalogoOrigem(importadorVersao="1.0.0", extraidoEm="2026-06-29T00:00:00Z"),
    )


def test_send_catalogo_posta_no_path_certo_e_parseia_relatorio():
    out = _FakeOutbound(_Resp(200, {"match_limpo": 1, "fire_only": 3420, "fire_pk_presente": True}))
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    rep = client.send_catalogo(_req())
    assert out.last_path == "/api/portal-pedidos/catalogo"
    assert out.last_json["schema"] == "catalogo.produtos.v1"
    assert out.last_json["dryRun"] is True
    assert rep.match_limpo == 1 and rep.fire_only == 3420 and rep.fire_pk_presente is True


def test_send_catalogo_erro_http_vira_FlowPCPClientError():
    out = _FakeOutbound(_Resp(500, text="boom"))
    client = FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)
    with pytest.raises(FlowPCPClientError):
        client.send_catalogo(_req())
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_client.py -v`
Expected: FAIL com `AttributeError: 'FlowPCPClient' object has no attribute 'send_catalogo'`

- [ ] **Step 3: Implementar no client**

Adicionar perto de `_DECISOES_PATH` em `app/integrations/flowpcp/client.py`:

```python
_CATALOGO_PATH = "/api/portal-pedidos/catalogo"
```

Adicionar o import (topo, junto aos outros de schema):

```python
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoReconciliacaoResponse,
    CatalogoRequest,
)
```

Adicionar o método dentro de `FlowPCPClient` (ex.: depois de `confirmar_reconciliacao`):

```python
    def send_catalogo(
        self, request: CatalogoRequest
    ) -> CatalogoReconciliacaoResponse:
        body = request.model_dump(by_alias=True)
        idem = f"catalogo-{int(request.dryRun)}-{len(request.itens)}"
        try:
            resp = self._client.post_json(_CATALOGO_PATH, json=body, idempotency_key=idem)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_catalogo falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"catalogo status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return CatalogoReconciliacaoResponse.model_validate(resp.json())
```

- [ ] **Step 4: Rodar o teste e ver passar**

Run: `.venv/bin/pytest tests/test_flowpcp_catalogo_client.py -v`
Expected: PASS (2 testes)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/client.py tests/test_flowpcp_catalogo_client.py
git commit -m "feat(flowpcp): client.send_catalogo (POST /catalogo)"
```

---

### Task 5: Orquestrador `run_catalogo_sync`

**Files:**
- Create: `app/integrations/flowpcp/catalogo_sync.py`
- Test: `tests/test_catalogo_sync.py`

**Interfaces:**
- Consumes: `flowpcp_config_for_slug` (`app/integrations/flowpcp/config.py`), `environments_repo.{get_by_slug,to_fb_config}`, `FirebirdConnection`, `extract_produtos` (Task 2), `build_catalogo_request` (Task 3), `FlowPCPClient.send_catalogo` (Task 4).
- Produces: `run_catalogo_sync(slug:str, *, dry_run:bool=True, full_sync:bool=True, now_iso:str|None=None, _client=None, _fire_conn=None) -> CatalogoReconciliacaoResponse | None`. Retorna `None` se o ambiente não tem FlowPCP habilitado. Os params `_client`/`_fire_conn` são injeção de teste (default = constrói os reais).

**Notas de design:** espelha `app/worker/jobs/poll_flowpcp.py` (gating por `flowpcp_config_for_slug`, conexão Fire via `to_fb_config`). `now_iso` injetável porque `Date.now()` não é determinístico em teste — caller passa o timestamp.

- [ ] **Step 1: Escrever o teste que falha**

```python
# tests/test_catalogo_sync.py
from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp import catalogo_sync


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_catalogo(self, request):
        self.sent = request
        return CatalogoReconciliacaoResponse(match_limpo=0, fire_only=len(request.itens),
                                             fire_pk_presente=True)

    def close(self):
        pass


def test_run_sync_extrai_empurra_e_devolve_relatorio(monkeypatch):
    dtos = [ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples"),
            ProdutoFireDTO("2", "2", "Y", "PC", "789", False, "kit")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn: dtos)

    class _Cfg:
        enabled = True
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    fake_client = _FakeClient()
    rep = catalogo_sync.run_catalogo_sync(
        "mm", dry_run=True, full_sync=True, now_iso="2026-06-29T00:00:00Z",
        _client=fake_client, _fire_conn=object(),
    )
    assert rep.fire_only == 2 and rep.fire_pk_presente is True
    assert fake_client.sent.dryRun is True
    assert fake_client.sent.fullSync is True
    assert len(fake_client.sent.itens) == 2


def test_run_sync_none_quando_flowpcp_desabilitado(monkeypatch):
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: None)
    assert catalogo_sync.run_catalogo_sync("mm", _client=object(), _fire_conn=object()) is None
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `.venv/bin/pytest tests/test_catalogo_sync.py -v`
Expected: FAIL com `ModuleNotFoundError: app.integrations.flowpcp.catalogo_sync`

- [ ] **Step 3: Implementar o orquestrador**

```python
# app/integrations/flowpcp/catalogo_sync.py
from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime

from app.erp.catalog_extract import extract_produtos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import environments_repo
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"


def _build_client(cfg) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_catalogo_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = True,
    now_iso: str | None = None,
    _client=None,
    _fire_conn=None,
) -> CatalogoReconciliacaoResponse | None:
    """Extrai o catálogo do Fire do ambiente `slug` e empurra ao FlowPCP.
    Fase 0: dry_run=True (não promove). Retorna o relatório, ou None se o
    ambiente não tem FlowPCP habilitado. `_client`/`_fire_conn` são injeção
    de teste (default constrói os reais)."""
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"catalogo sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    client = _client or _build_client(cfg)

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(
            environments_repo.to_fb_config(env)
        )

    try:
        with fire_ctx as fire_conn:
            dtos = extract_produtos(fire_conn)
        request = build_catalogo_request(
            dtos,
            dry_run=dry_run,
            full_sync=full_sync,
            importador_versao=_IMPORTADOR_VERSAO,
            extraido_em=extraido_em,
        )
        logger.info(
            f"catalogo sync env={slug} itens={len(dtos)} dry_run={dry_run} "
            f"full_sync={full_sync}"
        )
        return client.send_catalogo(request)
    finally:
        if _client is None:
            client.close()
```

- [ ] **Step 4: Rodar o teste e ver passar**

Run: `.venv/bin/pytest tests/test_catalogo_sync.py -v`
Expected: PASS (2 testes)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_sync.py tests/test_catalogo_sync.py
git commit -m "feat(flowpcp): orquestrador run_catalogo_sync (Fase 0 dry-run)"
```

---

### Task 6: CLI da Fase 0 (`tools/sync_catalogo_fire.py`)

**Files:**
- Create: `tools/sync_catalogo_fire.py`

**Interfaces:**
- Consumes: `run_catalogo_sync` (Task 5). Lê `.env` (`FB_CLIENT_LIBRARY`, `APP_DATA_DIR`) como `tools/close_flowpcp_e2e.py`.
- Produces: binário de linha de comando. `--slug mm` (default), `--apply` (default é dry-run; `--apply` manda `dryRun=false` — NÃO usar na Fase 0). Imprime o relatório de reconciliação.

**Nota:** não tem teste unitário (é thin CLI; a lógica testável está em `run_catalogo_sync`). Verificação = rodar `--help` e um dry-run real quando o endpoint do Flow existir.

- [ ] **Step 1: Implementar o CLI**

```python
# tools/sync_catalogo_fire.py
#!/usr/bin/env python3
"""Fase 0 do sync de catálogo Fire→FlowPCP: extrai PRODUTOS do Fire e empurra
em DRY-RUN, imprimindo o relatório de reconciliação que o Flow devolve.

Pré-requisitos: engine Firebird configurada (FB_CLIENT_LIBRARY no .env) + o
endpoint POST /api/portal-pedidos/catalogo no pcp-app no ar.

Rodar do root do projeto:
  .venv/bin/python tools/sync_catalogo_fire.py            # dry-run (Fase 0)
  .venv/bin/python tools/sync_catalogo_fire.py --slug mm
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync catálogo Fire->FlowPCP (Fase 0 dry-run)")
    parser.add_argument("--slug", default="mm")
    parser.add_argument(
        "--apply", action="store_true",
        help="Manda dryRun=false (PROMOVE no Flow). NÃO usar na Fase 0.",
    )
    args = parser.parse_args()

    base = Path(__file__).resolve().parent.parent
    load_dotenv(base / ".env")
    os.environ.setdefault("APP_DATA_DIR", str(base / "data"))

    from app.integrations.flowpcp.catalogo_sync import run_catalogo_sync

    dry_run = not args.apply
    print(f"== Catálogo Fire->FlowPCP | slug={args.slug} | dry_run={dry_run} ==")
    rep = run_catalogo_sync(args.slug, dry_run=dry_run, full_sync=True)
    if rep is None:
        print("✗ ambiente sem FlowPCP habilitado.")
        return 2

    print("\n== RELATÓRIO DE RECONCILIAÇÃO ==")
    for campo in (
        "match_limpo", "ambiguo", "flow_only", "fire_only", "criados",
        "atualizados", "inalterados", "desativados", "erros", "fire_pk_presente",
    ):
        print(f"  {campo:<18} {getattr(rep, campo, None)}")
    extras = rep.model_dump(exclude={
        "match_limpo", "ambiguo", "flow_only", "fire_only", "criados",
        "atualizados", "inalterados", "desativados", "erros", "fire_pk_presente",
    })
    if extras:
        print("\n  extras do Flow:", extras)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Verificar que importa e o --help funciona**

Run: `.venv/bin/python tools/sync_catalogo_fire.py --help`
Expected: imprime o usage sem erro de import.

- [ ] **Step 3: Lint + suíte completa**

Run: `.venv/bin/ruff check app/ tools/ tests/ && .venv/bin/pytest tests/ -q`
Expected: ruff "All checks passed!"; pytest tudo verde (testes existentes + os novos das Tasks 1–5).

- [ ] **Step 4: Commit**

```bash
git add tools/sync_catalogo_fire.py
git commit -m "feat(tools): CLI Fase 0 sync de catálogo Fire->FlowPCP (dry-run)"
```

---

## Dependência do lado Flow (pcp-app — plano separado)

O *run real* da Fase 0 precisa, no pcp-app, do contrato canônico desta spec:
- `POST /api/portal-pedidos/catalogo` com auth `X-Service-Token` (constant-time), gating por feature-flag do tenant.
- Schema Zod `catalogo.produtos.v1` (espelho do pydantic da Task 1).
- Migration `produtos_fire_staging` (aterrissagem read-only) — **requer aprovação explícita do Samuel**.
- Job de reconciliação (diff vs `produtos`) que devolve `{ match_limpo, ambiguo, flow_only, fire_only, criados, atualizados, inalterados, desativados, erros, fire_pk_presente }` + amostras.

Até isso existir, as Tasks 1–6 ficam verdes nos testes (mocks), mas o CLI da Task 6 só roda de verdade contra o endpoint vivo.

## Self-Review

- **Cobertura da spec (lado Importador):** extrai catálogo do Fire (Task 2 ✓), monta `catalogo.produtos.v1` (Task 1+3 ✓), push via Fatia G/`X-Service-Token` (Task 4 ✓), dry-run sem promover (Task 5 default ✓), CLI pra rodar a Fase 0 e ver o relatório (Task 6 ✓). Lado Flow declarado como dependência separada (não é desta fatia).
- **Achados da validação aplicados:** `fireProdutoId = codigo = str(SEQ)` (o cliente usa o sequencial), `tipo` derivado de `PRODUTOS_KIT` (`kit`/`simples`), `ativo=BLOQUEADO≠'Sim'`, `CODPROD_ALTERN` descartado, lote único `fullSync`. Kits + pares ambos sincronizados (linhas de `PRODUTOS`); composição/montagem do kit = fase futura. ✓
- **Type consistency:** `ProdutoFireDTO` (Task 2) consumido idêntico em Task 3 e Task 5; `CatalogoRequest`/`CatalogoReconciliacaoResponse` (Task 1) usados em 3/4/5; `send_catalogo` (Task 4) chamado em Task 5 com a mesma assinatura. ✓
- **Sem placeholders:** todo step tem código/comando completo. ✓
