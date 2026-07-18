# Carga de clientes ativos Fire → Flow — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Levar pro Flow, sob demanda e gated por ambiente, o cadastro curado dos clientes com pedido no Fire nos últimos 12 meses — 1 CNPJ = 1 cliente, deduplicado e normalizado.

**Architecture:** Espelho arquivo-por-arquivo do `catalogo_sync` existente (extrai do Fire → grava cópia local sempre → empurra ao Flow só com gate ON). A única lógica nova está na extração de clientes (regra CPF×CNPJ, dedup, contadores) e num normalizador canônico de CNPJ compartilhado entre a carga e o envio de pedido em runtime (correção B1 do review).

**Tech Stack:** Python 3.11, pydantic v2, firebird-driver (Firebird embedded/TCP), sqlite3 (cópia local por ambiente), FastAPI (rota admin), pytest.

## Global Constraints

- **Python ≥ 3.11** — sintaxe `X | None` e `match` liberadas.
- **Espelhar padrões existentes** — não introduzir dependência nem estilo novo; seguir `catalogo_sync`, `catalog_extract`, `catalogo_fire_repo`, `catalogo_schema`, `catalogo_mapper`, `config.py`, `client.py`.
- **Identidade: 1 CNPJ = 1 cliente.** Marca (`CODGRUPO`) só como campo `grupoCodigo`, nunca como identidade.
- **Gate default OFF** — `flowpcp_clientes_push` default `0`; sync roda local-only sem o endpoint do Flow existir.
- **`fullSync=False`** no envio (decisão I7 — carteira aditiva até a inativação existir).
- **CNPJ sempre dígitos-only** nos dois alimentadores do Flow (carga + runtime).
- **Testes direcionados primeiro** (`.venv/bin/pytest tests/<arquivo>.py -v`), suíte completa antes do commit final.
- **Lint:** `ruff check app/ tests/` + `ruff format app/ tests/` limpos antes de cada commit.
- Spec de referência: `docs/superpowers/specs/2026-07-17-carga-clientes-ativos-flow-design.md`.

---

### Task 0: Gates de verificação na Fire viva (pré-código)

Bloqueia **apenas** a Task 3 (query) e o enriquecimento de grupo. As Tasks 1–2 (normalização) e 4–9 não dependem disto e podem andar em paralelo. Sem acesso à Fire viva (VPN), execute assim mesmo e registre os resultados; use os fallbacks indicados.

**Files:**
- Referência: `tools/explore_firebird.py`, `app/erp/queries.py`

- [ ] **Passo 1: Confirmar `CADASTRO.CODGRUPO` (gate I1)**

Rodar contra uma CÓPIA do banco (nunca produção):
```bash
.venv/bin/python tools/explore_firebird.py --database empresa_COPIA.fdb > /tmp/schema.txt
grep -iE "CADASTRO|CODGRUPO|RELAC_CLIENTE|RAZAO_SOCIAL|CPF_CNPJ" /tmp/schema.txt
```
Registrar: `CODGRUPO` existe em `CADASTRO`? (S/N). **Fallback se N:** remover `C.CODGRUPO` do `SELECT` da Task 3 e passar `grupo_codigo=None` sempre — a carga não quebra, só perde o enriquecimento de marca.

- [ ] **Passo 2: Confirmar flag de bloqueio de cliente (gate I3)**

No mesmo `/tmp/schema.txt`, procurar em `CADASTRO` uma coluna análoga a `BLOQUEADO`/`INATIVO`/`SITUACAO`. Registrar o nome exato (ou "não existe"). **Se existir:** adicionar `AND C.<coluna> <> 'Sim'` (ou equivalente) ao `WHERE` da Task 3.

- [ ] **Passo 3: Confirmar índice `CAB_VENDAS(CLIENTE)` (M3)**

No relatório de índices do `explore_firebird.py`, confirmar índice em `CAB_VENDAS(CLIENTE)`. Registrar. Se ausente, anotar como risco de performance (não bloqueia; ~617 clientes é tolerável mesmo com scan).

- [ ] **Passo 4: Confirmar coluna de data da janela (I4/ajuste)**

Confirmar que `CAB_VENDAS.DATA_PEDIDO` é a data de emissão do pedido (existe — usada em `INSERT_CAB_VENDAS`). Se o negócio preferir outra (`DTHORA_PEDIDO`), ajustar o nome na query da Task 3.

- [ ] **Passo 5: Confirmar normalização no Flow (gate B1, repo pcp-app)**

Confirmar com o time do Flow que `resolver-cliente.ts` casa por CNPJ **dígitos-only**. Registrar a resposta. Independente dela, as Tasks 1–2 garantem dígitos do nosso lado.

Sem commit — este task só produz decisões registradas que alimentam a Task 3.

**RESULTADO (executado 2026-07-17, read-only via firebirdsql contra a Fire viva MM
`192.168.15.4:3050` / `C:\FireAdmMM\MM_CONFECCAO.FDB`):**
- **I1 — CODGRUPO existe:** SIM. SELECT de 4 colunas válido, sem fallback. **Porém a coluna
  está NULL em 100% do CADASTRO (0/29509)** → `grupo_codigo` vem None na prática; `grupoCodigo`
  omitido do payload. Coluna mantida p/ quando a MM popular a marca no Fire. O rollup por marca
  precisará de outra fonte (Studio Z/Centauro=SBF — ver memória).
- **I3 — flag de bloqueio:** `CADASTRO.BLOQUEADO` ∈ {'Sim','Nao'} (null-free). Aplicado
  `AND C.BLOQUEADO <> 'Sim'` na query (commit `c8ffeff`). `CAD_INATIVO` existe mas é
  NULL/'Nao'/0×'Sim' (não filtrado — BLOQUEADO cobre). Hoje 0 ativos bloqueados.
- **M3 — índice:** `CAB_VENDAS_COD_CLIENTE (CLIENTE)` existe → EXISTS indexado.
- **G4 — data:** `DATA_PEDIDO` é a coluna certa (existe; `DTHORA_PEDIDO` também). Mantido.
- **E2E:** extractor rodou contra dados reais → **68 clientes ativos, 0 CPF, 0 inválido,
  0 colisão de dedup**. Higiene de CNPJ da MM impecável.
- **PENDENTE (Flow-side, não verificável na Fire):** `resolver-cliente.ts` casar por CNPJ
  dígitos-only + endpoint `POST /api/portal-pedidos/clientes` existir no pcp-app.

Tooling: `firebirdsql` + `passlib` instalados no venv (Python puro — `fdb`+fbclient FB5 não
conecta no Mac). Não declarados em pyproject (dev/ops; app usa `fdb` no Windows).

---

### Task 1: Normalizador canônico de CNPJ (B1)

**Files:**
- Create: `app/erp/cnpj.py`
- Test: `tests/test_cnpj.py`

**Interfaces:**
- Produces: `cnpj_digits(value: str | None) -> str` — remove tudo que não é dígito; `None`/vazio → `""`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cnpj.py
from app.erp.cnpj import cnpj_digits


def test_cnpj_digits_strips_formatting():
    assert cnpj_digits("06.347.409/0296-51") == "06347409029651"


def test_cnpj_digits_already_clean_is_stable():
    assert cnpj_digits("06347409029651") == "06347409029651"


def test_cnpj_digits_none_and_empty():
    assert cnpj_digits(None) == ""
    assert cnpj_digits("   ") == ""


def test_cnpj_digits_drops_letters_and_spaces():
    assert cnpj_digits(" 06 347 409/0296-51 abc") == "06347409029651"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cnpj.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.erp.cnpj'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/erp/cnpj.py
from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"\D")


def cnpj_digits(value: str | None) -> str:
    """Normalizador canônico de CNPJ/CPF: só os dígitos.

    Forma única e inequívoca usada por TODO alimentador do Flow (carga de
    clientes E envio de pedido em runtime) para o casamento por CNPJ bater.
    `None`/vazio → "".
    """
    if not value:
        return ""
    return _NON_DIGIT.sub("", value)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_cnpj.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/erp/cnpj.py tests/test_cnpj.py && ruff format app/erp/cnpj.py tests/test_cnpj.py
git add app/erp/cnpj.py tests/test_cnpj.py
git commit -m "feat(cnpj): normalizador canônico de CNPJ (dígitos-only)"
```

---

### Task 2: Normalizar CNPJ no envio de pedido em runtime (B1)

Corrige o alimentador de runtime: hoje `mapper.py:45` manda `customer_cnpj` sem normalizar (formatado), o que fragmentaria o cliente contra a carga.

**Files:**
- Modify: `app/integrations/flowpcp/mapper.py`
- Test: `tests/test_flowpcp_mapper_cnpj.py`

**Interfaces:**
- Consumes: `cnpj_digits` (Task 1).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_mapper_cnpj.py
from app.integrations.flowpcp.mapper import build_recebimento_payload
from app.models.order import Order, OrderHeader, OrderItem


def _order(cnpj: str | None) -> Order:
    return Order(
        header=OrderHeader(customer_name="LOJA X", customer_cnpj=cnpj, order_number="123"),
        items=[OrderItem(description="TENIS", quantity=1)],
        source_file="x.pdf",
    )


def test_payload_normalizes_formatted_cnpj():
    req = build_recebimento_payload(import_id="imp1", order=_order("06.347.409/0296-51"), tenant_id="t1")
    assert req.cliente.cnpj == "06347409029651"


def test_payload_keeps_none_when_no_cnpj():
    req = build_recebimento_payload(import_id="imp1", order=_order(None), tenant_id="t1")
    assert req.cliente.cnpj is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_mapper_cnpj.py -v`
Expected: FAIL — `test_payload_normalizes_formatted_cnpj` retorna `"06.347.409/0296-51"`, não `"06347409029651"`.

- [ ] **Step 3: Write minimal implementation**

Em `app/integrations/flowpcp/mapper.py`, adicionar o import no topo (junto aos outros imports):
```python
from app.erp.cnpj import cnpj_digits
```
E trocar a linha que monta o `cliente` (atual `mapper.py:45`):
```python
        cliente=ClienteRecebimento(
            nome=h.customer_name or "(sem cliente)",
            cnpj=(cnpj_digits(h.customer_cnpj) or None),
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flowpcp_mapper_cnpj.py -v`
Expected: PASS (2 passed)

Run também a suíte do mapper existente para garantir zero regressão:
Run: `.venv/bin/pytest tests/ -k "flowpcp and mapper" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
ruff check app/integrations/flowpcp/mapper.py tests/test_flowpcp_mapper_cnpj.py && ruff format app/integrations/flowpcp/mapper.py tests/test_flowpcp_mapper_cnpj.py
git add app/integrations/flowpcp/mapper.py tests/test_flowpcp_mapper_cnpj.py
git commit -m "fix(flowpcp): normaliza CNPJ do pedido em runtime (B1)"
```

---

### Task 3: Query + extractor de clientes ativos

O coração da lógica nova: janela de 12m, regra CPF×CNPJ (I2), dedup por CNPJ (maior CODIGO), contadores (I6).

**Files:**
- Modify: `app/erp/queries.py` (adicionar `LIST_CLIENTES_ATIVOS` ao fim)
- Create: `app/erp/cliente_extract.py`
- Test: `tests/test_cliente_extract.py`

**Interfaces:**
- Consumes: `cnpj_digits` (Task 1).
- Produces:
  - `ClienteFireDTO` (frozen): `fire_cliente_id: str`, `cnpj: str`, `nome: str`, `grupo_codigo: str | None`, `ativo: bool`.
  - `ExtracaoClientesResult` (frozen): `clientes: list[ClienteFireDTO]`, `descartados_cpf: int`, `descartados_invalidos: int`, `colisoes_dedup: int`.
  - `extract_clientes_ativos(fire_conn, *, desde_data: date) -> ExtracaoClientesResult`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cliente_extract.py
from datetime import date

from app.erp.cliente_extract import ExtracaoClientesResult, extract_clientes_ativos


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self):
        return self._cur


def test_extract_keeps_cnpj_discards_cpf_and_invalid():
    # (CODIGO, RAZAO_SOCIAL, CPF_CNPJ, CODGRUPO)
    rows = [
        (498, "SBF S.A", "06.347.409/0296-51", 12),   # CNPJ 14 díg → mantém
        (10, "JOAO PESSOA FISICA", "123.456.789-09", None),  # CPF 11 díg → descarta
        (11, "LIXO", "abc", None),                     # inválido → descarta
    ]
    res = extract_clientes_ativos(_FakeConn(rows), desde_data=date(2025, 7, 17))
    assert isinstance(res, ExtracaoClientesResult)
    assert [c.cnpj for c in res.clientes] == ["06347409029651"]
    assert res.clientes[0].fire_cliente_id == "498"
    assert res.clientes[0].nome == "SBF S.A"
    assert res.clientes[0].grupo_codigo == "12"
    assert res.clientes[0].ativo is True
    assert res.descartados_cpf == 1
    assert res.descartados_invalidos == 1
    assert res.colisoes_dedup == 0


def test_extract_dedups_by_cnpj_keeping_max_codigo():
    rows = [
        (100, "CADASTRO ANTIGO", "06347409029651", 12),
        (200, "CADASTRO NOVO", "06.347.409/0296-51", 12),  # mesmo CNPJ, CODIGO maior
    ]
    res = extract_clientes_ativos(_FakeConn(rows), desde_data=date(2025, 7, 17))
    assert len(res.clientes) == 1
    assert res.clientes[0].fire_cliente_id == "200"
    assert res.clientes[0].nome == "CADASTRO NOVO"
    assert res.colisoes_dedup == 1


def test_extract_passes_desde_data_as_bind():
    conn = _FakeConn([])
    extract_clientes_ativos(conn, desde_data=date(2025, 7, 17))
    _sql, params = conn._cur.executed
    assert params == (date(2025, 7, 17),)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_cliente_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.erp.cliente_extract'`

- [ ] **Step 3a: Adicionar a query em `app/erp/queries.py`**

Ao final do arquivo:
```python
# ── Clientes ativos (carga Fire→Flow) ─────────────────────────────────────────
# Clientes (RELAC_CLIENTE='Sim') com pelo menos um pedido em CAB_VENDAS dentro da
# janela (bind = data de corte, calculada no Python). CODGRUPO = a marca (Task 0
# gate I1: se a coluna não existir, remover C.CODGRUPO e o extractor manda grupo=None).
LIST_CLIENTES_ATIVOS = """
    SELECT C.CODIGO, C.RAZAO_SOCIAL, C.CPF_CNPJ, C.CODGRUPO
    FROM CADASTRO C
    WHERE C.RELAC_CLIENTE = 'Sim'
      AND EXISTS (
          SELECT 1 FROM CAB_VENDAS V
          WHERE V.CLIENTE = C.CODIGO
            AND V.DATA_PEDIDO >= ?
      )
    ORDER BY C.CODIGO
"""
```

> **Fallback do gate I1 (Task 0 Passo 1):** se `CODGRUPO` não existir, remover `, C.CODGRUPO`
> do `SELECT` **e** ajustar o unpacking do extractor (Step 3b) para 3 colunas
> (`for codigo, razao, cpf_cnpj in rows:`) com `codgrupo = None`. O teste
> `test_extract_keeps_cnpj_discards_cpf_and_invalid` passaria a esperar `grupo_codigo is None`.

- [ ] **Step 3b: Escrever o extractor**

```python
# app/erp/cliente_extract.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.erp.cnpj import cnpj_digits
from app.erp.queries import LIST_CLIENTES_ATIVOS


@dataclass(frozen=True)
class ClienteFireDTO:
    fire_cliente_id: str      # str(CADASTRO.CODIGO) — PK durável
    cnpj: str                 # dígitos-only, 14 — chave de match no Flow
    nome: str                 # RAZAO_SOCIAL
    grupo_codigo: str | None  # str(CODGRUPO) — a marca; None se a coluna não existir
    ativo: bool               # sempre True nesta fase (janela ativa)


@dataclass(frozen=True)
class ExtracaoClientesResult:
    clientes: list[ClienteFireDTO]
    descartados_cpf: int
    descartados_invalidos: int
    colisoes_dedup: int


def _clean(v) -> str:
    return str(v).strip() if v is not None else ""


def extract_clientes_ativos(fire_conn, *, desde_data: date) -> ExtracaoClientesResult:
    """Lê os clientes ativos (pedido na janela) do Fire. Read-only.

    Regras (spec I2/I6): normaliza CPF_CNPJ para dígitos; 14 = CNPJ (mantém),
    11 = CPF (descarta), resto = inválido (descarta). Dedup por CNPJ mantendo o
    maior CODIGO (a query vem ORDER BY CODIGO asc → o último visto é o maior).
    """
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_CLIENTES_ATIVOS, (desde_data,))
        rows = cur.fetchall()
    finally:
        cur.close()

    by_cnpj: dict[str, tuple[int, str, object]] = {}
    descartados_cpf = 0
    descartados_invalidos = 0
    colisoes_dedup = 0

    for codigo, razao, cpf_cnpj, codgrupo in rows:
        digits = cnpj_digits(cpf_cnpj)
        if len(digits) == 14:
            if digits in by_cnpj:
                colisoes_dedup += 1
                if codigo > by_cnpj[digits][0]:
                    by_cnpj[digits] = (codigo, _clean(razao), codgrupo)
            else:
                by_cnpj[digits] = (codigo, _clean(razao), codgrupo)
        elif len(digits) == 11:
            descartados_cpf += 1
        else:
            descartados_invalidos += 1

    clientes = [
        ClienteFireDTO(
            fire_cliente_id=str(codigo),
            cnpj=digits,
            nome=razao,
            grupo_codigo=(str(codgrupo) if codgrupo is not None else None),
            ativo=True,
        )
        for digits, (codigo, razao, codgrupo) in by_cnpj.items()
    ]
    return ExtracaoClientesResult(
        clientes=clientes,
        descartados_cpf=descartados_cpf,
        descartados_invalidos=descartados_invalidos,
        colisoes_dedup=colisoes_dedup,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cliente_extract.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/erp/queries.py app/erp/cliente_extract.py tests/test_cliente_extract.py && ruff format app/erp/queries.py app/erp/cliente_extract.py tests/test_cliente_extract.py
git add app/erp/queries.py app/erp/cliente_extract.py tests/test_cliente_extract.py
git commit -m "feat(erp): extractor de clientes ativos (janela 12m, CPF×CNPJ, dedup)"
```

---

### Task 4: Cópia local `clientes_fire` (repo + DDL)

**Files:**
- Modify: `app/persistence/schema_env.py` (adicionar DDL ao `TABLES_SQL`)
- Create: `app/persistence/clientes_fire_repo.py`
- Test: `tests/test_clientes_fire_repo.py`

**Interfaces:**
- Consumes: `ClienteFireDTO` (Task 3).
- Produces: `clientes_fire_repo.replace_all(conn, dtos, *, extraido_em) -> int`, `list_all(conn) -> list[dict]`, `count(conn) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clientes_fire_repo.py
import sqlite3

from app.erp.cliente_extract import ClienteFireDTO
from app.persistence import clientes_fire_repo
from app.persistence.schema_env import TABLES_SQL


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(TABLES_SQL)
    return conn


def _dto(codigo: str, cnpj: str) -> ClienteFireDTO:
    return ClienteFireDTO(
        fire_cliente_id=codigo, cnpj=cnpj, nome=f"CLIENTE {codigo}",
        grupo_codigo="12", ativo=True,
    )


def test_replace_all_snapshot_and_count():
    conn = _conn()
    n = clientes_fire_repo.replace_all(
        conn, [_dto("1", "11111111111111"), _dto("2", "22222222222222")],
        extraido_em="2026-07-17T12:00:00Z",
    )
    assert n == 2
    assert clientes_fire_repo.count(conn) == 2
    # substituição: segunda carga menor apaga a anterior
    clientes_fire_repo.replace_all(conn, [_dto("3", "33333333333333")], extraido_em="2026-07-17T13:00:00Z")
    rows = clientes_fire_repo.list_all(conn)
    assert [r["fire_cliente_id"] for r in rows] == ["3"]
    assert rows[0]["cnpj"] == "33333333333333"
    assert rows[0]["ativo"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientes_fire_repo.py -v`
Expected: FAIL — `no such table: clientes_fire` (ou ImportError do repo)

- [ ] **Step 3a: Adicionar o DDL em `app/persistence/schema_env.py`**

Dentro da string `TABLES_SQL`, logo após o bloco `CREATE TABLE IF NOT EXISTS catalogo_fire (...);` (antes do `"""` que fecha a string):
```sql
-- Cópia local dos clientes ativos do Fire ("manter no importador"). Snapshot
-- substitutivo a cada sync; envio ao Flow é gated por flowpcp_clientes_push.
CREATE TABLE IF NOT EXISTS clientes_fire (
    fire_cliente_id TEXT PRIMARY KEY,
    cnpj            TEXT NOT NULL,
    nome            TEXT NOT NULL,
    grupo_codigo    TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1,
    extraido_em     TEXT NOT NULL
);
```

- [ ] **Step 3b: Escrever o repo**

```python
# app/persistence/clientes_fire_repo.py
"""Cópia local dos clientes ativos do Fire (`clientes_fire`, db do ambiente).

Snapshot substitutivo (delete + insert); o envio ao Flow é decisão separada
(flowpcp_clientes_push). Recebe a conexão aberta (mesmo padrão do catalogo_fire_repo).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

    from app.erp.cliente_extract import ClienteFireDTO

_COLS = ("fire_cliente_id", "cnpj", "nome", "grupo_codigo", "ativo", "extraido_em")


def replace_all(conn: sqlite3.Connection, dtos: list[ClienteFireDTO], *, extraido_em: str) -> int:
    """Substitui o snapshot inteiro pela extração atual. Retorna o total gravado."""
    conn.execute("DELETE FROM clientes_fire")
    conn.executemany(
        f"INSERT INTO clientes_fire ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
        [
            (d.fire_cliente_id, d.cnpj, d.nome, d.grupo_codigo, 1 if d.ativo else 0, extraido_em)
            for d in dtos
        ],
    )
    return len(dtos)


def list_all(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"SELECT {', '.join(_COLS)} FROM clientes_fire ORDER BY fire_cliente_id"
    ).fetchall()
    return [dict(zip(_COLS, r, strict=True)) for r in rows]


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientes_fire_repo.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/persistence/clientes_fire_repo.py tests/test_clientes_fire_repo.py && ruff format app/persistence/clientes_fire_repo.py tests/test_clientes_fire_repo.py
git add app/persistence/schema_env.py app/persistence/clientes_fire_repo.py tests/test_clientes_fire_repo.py
git commit -m "feat(persistence): tabela clientes_fire + repo (cópia local)"
```

---

### Task 5: Schema pydantic + mapper do request

**Files:**
- Create: `app/integrations/flowpcp/clientes_schema.py`
- Create: `app/integrations/flowpcp/clientes_mapper.py`
- Test: `tests/test_clientes_mapper.py`

**Interfaces:**
- Consumes: `ClienteFireDTO` (Task 3).
- Produces:
  - `ClienteItem`, `ClientesOrigem`, `ClientesRequest` (default `schema="cadastro.clientes.v1"`), `ClientesReconciliacaoResponse` (extra allow).
  - `build_clientes_request(dtos, *, dry_run, full_sync, importador_versao, extraido_em) -> ClientesRequest`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clientes_mapper.py
from app.erp.cliente_extract import ClienteFireDTO
from app.integrations.flowpcp.clientes_mapper import build_clientes_request


def _dto() -> ClienteFireDTO:
    return ClienteFireDTO(
        fire_cliente_id="498", cnpj="06347409029651",
        nome="SBF S.A", grupo_codigo="12", ativo=True,
    )


def test_build_request_maps_fields_and_aliases():
    req = build_clientes_request(
        [_dto()], dry_run=True, full_sync=False,
        importador_versao="1.0.0", extraido_em="2026-07-17T12:00:00Z",
    )
    body = req.model_dump(by_alias=True)
    assert body["schema"] == "cadastro.clientes.v1"
    assert body["dryRun"] is True
    assert body["fullSync"] is False
    item = body["itens"][0]
    assert item["fireClienteId"] == "498"
    assert item["cnpj"] == "06347409029651"
    assert item["nome"] == "SBF S.A"
    assert item["grupoCodigo"] == "12"
    assert item["ativo"] is True
    assert body["origem"]["importadorVersao"] == "1.0.0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientes_mapper.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.integrations.flowpcp.clientes_mapper'`

- [ ] **Step 3a: Escrever o schema**

```python
# app/integrations/flowpcp/clientes_schema.py
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ClienteItem(BaseModel):
    """Item de identidade do cliente (Fire é dono). camelCase no wire."""

    model_config = ConfigDict(populate_by_name=True)

    fireClienteId: str  # noqa: N815 — CADASTRO.CODIGO (PK durável)
    cnpj: str           # dígitos-only — chave de match
    nome: str           # RAZAO_SOCIAL
    grupoCodigo: str | None = None  # noqa: N815 — CODGRUPO (marca)
    ativo: bool = True


class ClientesOrigem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    importadorVersao: str  # noqa: N815
    extraidoEm: str  # noqa: N815 — ISO8601


class ClientesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="cadastro.clientes.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[ClienteItem]
    origem: ClientesOrigem


class ClientesReconciliacaoResponse(BaseModel):
    """Relatório devolvido pelo Flow. O Flow é dono do contrato de resposta;
    `extra="allow"` tolera campos novos (contagens/amostras aninhados, camelCase)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dry_run: bool | None = Field(default=None, alias="dryRun")
    full_sync: bool | None = Field(default=None, alias="fullSync")
```

- [ ] **Step 3b: Escrever o mapper**

```python
# app/integrations/flowpcp/clientes_mapper.py
from __future__ import annotations

from app.erp.cliente_extract import ClienteFireDTO
from app.integrations.flowpcp.clientes_schema import (
    ClienteItem,
    ClientesOrigem,
    ClientesRequest,
)


def build_clientes_request(
    dtos: list[ClienteFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
) -> ClientesRequest:
    itens = [
        ClienteItem(
            fireClienteId=d.fire_cliente_id,
            cnpj=d.cnpj,
            nome=d.nome,
            grupoCodigo=d.grupo_codigo,
            ativo=d.ativo,
        )
        for d in dtos
    ]
    return ClientesRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=itens,
        origem=ClientesOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientes_mapper.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/integrations/flowpcp/clientes_schema.py app/integrations/flowpcp/clientes_mapper.py tests/test_clientes_mapper.py && ruff format app/integrations/flowpcp/clientes_schema.py app/integrations/flowpcp/clientes_mapper.py tests/test_clientes_mapper.py
git add app/integrations/flowpcp/clientes_schema.py app/integrations/flowpcp/clientes_mapper.py tests/test_clientes_mapper.py
git commit -m "feat(flowpcp): schema + mapper do request de clientes (cadastro.clientes.v1)"
```

---

### Task 6: `client.send_clientes` com idempotency key por conteúdo (I5)

**Files:**
- Modify: `app/integrations/flowpcp/client.py`
- Test: `tests/test_clientes_client.py`

**Interfaces:**
- Consumes: `ClientesRequest`, `ClientesReconciliacaoResponse` (Task 5).
- Produces: `FlowPCPClient.send_clientes(request: ClientesRequest) -> ClientesReconciliacaoResponse`; constante `_CLIENTES_PATH`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clientes_client.py
import hashlib

from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.clientes_schema import ClienteItem, ClientesOrigem, ClientesRequest


class _FakeResp:
    is_success = True
    status_code = 200

    def json(self):
        return {"dryRun": True, "contagens": {"fireTotal": 1}}


class _FakeOutbound:
    def __init__(self):
        self.calls = []

    def post_json(self, path, *, json, idempotency_key):
        self.calls.append((path, json, idempotency_key))
        return _FakeResp()

    def close(self):
        pass


def _req(itens):
    return ClientesRequest(
        dryRun=True, fullSync=False, itens=itens,
        origem=ClientesOrigem(importadorVersao="1.0.0", extraidoEm="2026-07-17T12:00:00Z"),
    )


def _item(cnpj, nome, grupo="12"):
    return ClienteItem(fireClienteId="1", cnpj=cnpj, nome=nome, grupoCodigo=grupo)


def _client(outbound):
    return FlowPCPClient(base_url="http://x", service_token="t", tenant_id="t1", outbound=outbound)


def test_send_clientes_posts_to_path_and_parses():
    ob = _FakeOutbound()
    resp = _client(ob).send_clientes(_req([_item("06347409029651", "SBF")]))
    assert ob.calls[0][0] == "/api/portal-pedidos/clientes"
    assert resp.dry_run is True


def test_idempotency_key_changes_with_content_not_just_count():
    ob = _FakeOutbound()
    c = _client(ob)
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    c.send_clientes(_req([_item("06347409029651", "SBF CORRIGIDO")]))  # mesma contagem, nome diferente
    key1, key2 = ob.calls[0][2], ob.calls[1][2]
    assert key1 != key2


def test_idempotency_key_stable_for_same_content():
    ob = _FakeOutbound()
    c = _client(ob)
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    c.send_clientes(_req([_item("06347409029651", "SBF")]))
    assert ob.calls[0][2] == ob.calls[1][2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientes_client.py -v`
Expected: FAIL — `AttributeError: 'FlowPCPClient' object has no attribute 'send_clientes'`

- [ ] **Step 3: Write minimal implementation**

Em `app/integrations/flowpcp/client.py`:

1. No topo, adicionar imports:
```python
import hashlib
```
e nos imports do pacote (junto ao bloco `from app.integrations.flowpcp.catalogo_schema import ...`):
```python
from app.integrations.flowpcp.clientes_schema import (
    ClientesReconciliacaoResponse,
    ClientesRequest,
)
```

2. Junto às constantes de path (perto de `_CATALOGO_PATH`):
```python
_CLIENTES_PATH = "/api/portal-pedidos/clientes"
```

3. Adicionar o método na classe `FlowPCPClient` (espelha `send_catalogo`, mas key por conteúdo):
```python
    def send_clientes(self, request: ClientesRequest) -> ClientesReconciliacaoResponse:
        body = request.model_dump(by_alias=True)
        # I5: key inclui hash do conteúdo — estável em retry, único quando muda
        # (a contagem sozinha colidiria: 617 clientes seguem 617 com nomes corrigidos).
        payload_sig = "|".join(
            sorted(f"{i.cnpj}:{i.nome}:{i.grupoCodigo or ''}" for i in request.itens)
        )
        digest = hashlib.sha256(payload_sig.encode("utf-8")).hexdigest()[:16]
        idem = f"clientes-{int(request.dryRun)}-{digest}"
        try:
            resp = self._client.post_json(_CLIENTES_PATH, json=body, idempotency_key=idem)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_clientes falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if not resp.is_success:
            raise FlowPCPClientError(
                f"clientes status {resp.status_code}",
                status_code=resp.status_code,
                body=(resp.text or "")[:500],
            )
        return ClientesReconciliacaoResponse.model_validate(resp.json())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientes_client.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/integrations/flowpcp/client.py tests/test_clientes_client.py && ruff format app/integrations/flowpcp/client.py tests/test_clientes_client.py
git add app/integrations/flowpcp/client.py tests/test_clientes_client.py
git commit -m "feat(flowpcp): client.send_clientes com idempotency key por conteúdo (I5)"
```

---

### Task 7: Gate `clientes_push` (config + schema + repo + UI request)

**Files:**
- Modify: `app/integrations/flowpcp/config.py`
- Modify: `app/persistence/schema_shared.py` (CREATE + lista de ALTER)
- Modify: `app/persistence/environments_repo.py` (lista de colunas + `set_flowpcp_config`)
- Modify: `app/web/routes_environments.py` (`FlowPCPConfigRequest`)
- Test: `tests/test_flowpcp_clientes_config.py`

**Interfaces:**
- Produces: `FlowPCPConfig.clientes_push: bool`; coluna `environments.flowpcp_clientes_push`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_clientes_config.py
from app.integrations.flowpcp.config import flowpcp_config_from_env


def test_config_reads_clientes_push_on():
    cfg = flowpcp_config_from_env({"flowpcp_enabled": 1, "flowpcp_clientes_push": 1}, service_token="t")
    assert cfg.clientes_push is True


def test_config_clientes_push_defaults_off():
    cfg = flowpcp_config_from_env({"flowpcp_enabled": 1}, service_token="t")
    assert cfg.clientes_push is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_flowpcp_clientes_config.py -v`
Expected: FAIL — `AttributeError: 'FlowPCPConfig' object has no attribute 'clientes_push'`

- [ ] **Step 3a: `config.py`** — adicionar o campo e o mapeamento

No dataclass `FlowPCPConfig`, após `catalogo_push`:
```python
    # Gate do envio de clientes ao Flow: OFF = sync só atualiza a cópia local.
    clientes_push: bool = False
```
Em `flowpcp_config_from_env`, no `return FlowPCPConfig(...)`, após `catalogo_push=...`:
```python
        clientes_push=bool(env.get("flowpcp_clientes_push")),
```

- [ ] **Step 3b: `schema_shared.py`** — coluna nova

No `CREATE TABLE environments`, após a linha `flowpcp_catalogo_apenas_meias ...`:
```sql
    flowpcp_clientes_push     INTEGER NOT NULL DEFAULT 0,
```
Na lista de migração (após a tupla `("environments", "flowpcp_catalogo_apenas_meias", ...)`):
```python
    ("environments", "flowpcp_clientes_push",
     "ALTER TABLE environments ADD COLUMN flowpcp_clientes_push INTEGER NOT NULL DEFAULT 0"),
```

- [ ] **Step 3c: `environments_repo.py`** — lista de colunas + `set_flowpcp_config`

Na tupla/lista de colunas FlowPCP (perto de `"flowpcp_catalogo_apenas_meias"`), adicionar:
```python
    "flowpcp_clientes_push",
```
Na assinatura de `set_flowpcp_config`, após `catalogo_apenas_meias: bool = False,`:
```python
    clientes_push: bool = False,
```
No dict `fields`, após `"flowpcp_catalogo_apenas_meias": ...`:
```python
        "flowpcp_clientes_push": 1 if clientes_push else 0,
```

- [ ] **Step 3d: `routes_environments.py`** — `FlowPCPConfigRequest`

Após o campo `catalogo_apenas_meias: bool = False` (antes de `service_token`):
```python
    # Gate do envio de clientes ao Flow (OFF = sync só atualiza a cópia local)
    clientes_push: bool = False
```
(O handler `set_environment_flowpcp` já faz `**payload.model_dump()`, então o campo flui automaticamente.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_flowpcp_clientes_config.py -v`
Expected: PASS (2 passed)

Regressão do schema/repo:
Run: `.venv/bin/pytest tests/ -k "environments or schema or config" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
ruff check app/integrations/flowpcp/config.py app/persistence/schema_shared.py app/persistence/environments_repo.py app/web/routes_environments.py tests/test_flowpcp_clientes_config.py && ruff format app/integrations/flowpcp/config.py app/persistence/schema_shared.py app/persistence/environments_repo.py app/web/routes_environments.py tests/test_flowpcp_clientes_config.py
git add app/integrations/flowpcp/config.py app/persistence/schema_shared.py app/persistence/environments_repo.py app/web/routes_environments.py tests/test_flowpcp_clientes_config.py
git commit -m "feat(flowpcp): gate flowpcp_clientes_push (config + schema + repo + request)"
```

---

### Task 8: Orquestrador `run_clientes_sync` (trava de vazio I4 + contadores I6)

**Files:**
- Create: `app/integrations/flowpcp/clientes_sync.py`
- Test: `tests/test_clientes_sync.py`

**Interfaces:**
- Consumes: `extract_clientes_ativos`/`ExtracaoClientesResult` (T3), `clientes_fire_repo` (T4), `build_clientes_request` (T5), `FlowPCPClient.send_clientes` (T6), `flowpcp_config_for_slug` (T7).
- Produces:
  - `ClientesSyncResult` (frozen): `itens: int`, `extraido_em: str`, `descartados_cpf: int`, `descartados_invalidos: int`, `colisoes_dedup: int`, `skipped_empty: bool = False`, `reconciliacao: ClientesReconciliacaoResponse | None = None`. (`reconciliacao is None` ⇒ local-only.)
  - `run_clientes_sync(slug, *, dry_run=True, full_sync=False, now_iso=None, _hoje=None, permitir_vazio=False, _client=None, _fire_conn=None, _env_conn=None) -> ClientesSyncResult | None`.

> **Nota de design:** este orquestrador retorna UM `ClientesSyncResult` em vez da trinca `None|LocalResult|Response` do catálogo, para carregar os contadores de descarte/dedup em TODOS os caminhos (I6). Retorna `None` só quando o ambiente não tem FlowPCP.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_clientes_sync.py
import sqlite3
from datetime import date

import pytest

from app.erp.cliente_extract import ClienteFireDTO, ExtracaoClientesResult
from app.integrations.flowpcp import clientes_sync
from app.integrations.flowpcp.config import FlowPCPConfig
from app.persistence.schema_env import TABLES_SQL


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_clientes(self, request):
        self.sent = request

        class _R:
            dry_run = True
        return _R()

    def close(self):
        pass


def _dto(codigo, cnpj):
    return ClienteFireDTO(fire_cliente_id=codigo, cnpj=cnpj, nome=f"C{codigo}", grupo_codigo=None, ativo=True)


def _env_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(TABLES_SQL)
    return conn


@pytest.fixture
def _patch(monkeypatch):
    def _apply(cfg, extracao):
        monkeypatch.setattr(clientes_sync, "flowpcp_config_for_slug", lambda slug: cfg)
        monkeypatch.setattr(clientes_sync, "extract_clientes_ativos", lambda conn, *, desde_data: extracao)
    return _apply


def test_returns_none_when_no_flowpcp(_patch):
    _patch(None, None)
    assert clientes_sync.run_clientes_sync("mm", _fire_conn=object(), _env_conn=_env_conn()) is None


def test_empty_extraction_skips_write_and_push(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=True)
    _patch(cfg, ExtracaoClientesResult(clientes=[], descartados_cpf=2, descartados_invalidos=0, colisoes_dedup=0))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.skipped_empty is True
    assert res.itens == 0
    assert res.descartados_cpf == 2
    assert client.sent is None
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 0


def test_gate_off_writes_local_only(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=False)
    _patch(cfg, ExtracaoClientesResult(clientes=[_dto("1", "11111111111111")], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=1))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.reconciliacao is None
    assert res.itens == 1
    assert res.colisoes_dedup == 1
    assert client.sent is None
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1


def test_gate_on_pushes_and_returns_reconciliacao(_patch):
    cfg = FlowPCPConfig(enabled=True, clientes_push=True)
    _patch(cfg, ExtracaoClientesResult(clientes=[_dto("1", "11111111111111")], descartados_cpf=0, descartados_invalidos=0, colisoes_dedup=0))
    conn = _env_conn()
    client = _FakeClient()
    res = clientes_sync.run_clientes_sync("mm", dry_run=True, _client=client, _fire_conn=object(), _env_conn=conn)
    assert res.reconciliacao is not None
    assert client.sent is not None
    assert client.sent.fullSync is False  # I7
    assert conn.execute("SELECT COUNT(*) FROM clientes_fire").fetchone()[0] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_clientes_sync.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.integrations.flowpcp.clientes_sync'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/clientes_sync.py
from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from app.erp.cliente_extract import extract_clientes_ativos
from app.erp.connection import FirebirdConnection
from app.integrations.flowpcp.client import FlowPCPClient
from app.integrations.flowpcp.clientes_mapper import build_clientes_request
from app.integrations.flowpcp.clientes_schema import ClientesReconciliacaoResponse
from app.integrations.flowpcp.config import flowpcp_config_for_slug
from app.persistence import clientes_fire_repo, environments_repo, router
from app.utils.logger import logger

_IMPORTADOR_VERSAO = "1.0.0"
_JANELA_DIAS = 365  # ~12 meses (hardcoded — YAGNI)


@dataclass(frozen=True)
class ClientesSyncResult:
    itens: int
    extraido_em: str
    descartados_cpf: int
    descartados_invalidos: int
    colisoes_dedup: int
    skipped_empty: bool = False
    reconciliacao: ClientesReconciliacaoResponse | None = None


def _build_client(cfg) -> FlowPCPClient:
    return FlowPCPClient(
        base_url=cfg.base_url,
        service_token=cfg.service_token,
        tenant_id=cfg.tenant_id,
        timeout=cfg.request_timeout_s,
    )


def run_clientes_sync(
    slug: str,
    *,
    dry_run: bool = True,
    full_sync: bool = False,   # I7: aditivo até a inativação existir
    now_iso: str | None = None,
    _hoje: date | None = None,
    permitir_vazio: bool = False,
    _client=None,
    _fire_conn=None,
    _env_conn=None,
) -> ClientesSyncResult | None:
    """Extrai clientes ativos (12m) do Fire do ambiente `slug`, grava a cópia
    local e — só se `flowpcp_clientes_push` estiver ligado — empurra ao Flow.

    Retorna `ClientesSyncResult` (com contadores em todos os caminhos) ou `None`
    se o ambiente não tem FlowPCP habilitado. `reconciliacao is None` ⇒ local-only.
    Trava I4: extração vazia não zera o snapshot nem envia (salvo `permitir_vazio`).
    """
    cfg = flowpcp_config_for_slug(slug)
    if cfg is None or not getattr(cfg, "enabled", False):
        logger.info(f"clientes sync: ambiente {slug} sem FlowPCP habilitado — skip")
        return None

    extraido_em = now_iso or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    hoje = _hoje or datetime.now(ZoneInfo(cfg.timezone)).date()
    desde = hoje - timedelta(days=_JANELA_DIAS)

    if _fire_conn is not None:
        fire_ctx = nullcontext(_fire_conn)
    else:
        env = environments_repo.get_by_slug(slug)
        fire_ctx = FirebirdConnection().connect_with_config(environments_repo.to_fb_config(env))

    with fire_ctx as fire_conn:
        extr = extract_clientes_ativos(fire_conn, desde_data=desde)

    logger.info(
        f"clientes sync env={slug} ativos={len(extr.clientes)} "
        f"descartados_cpf={extr.descartados_cpf} descartados_invalidos={extr.descartados_invalidos} "
        f"colisoes_dedup={extr.colisoes_dedup} desde={desde}"
    )

    # I4 — trava de vazio: não zera o snapshot local nem manda 0 itens ao Flow.
    if not extr.clientes and not permitir_vazio:
        logger.warning(
            f"clientes sync env={slug}: extração VAZIA — snapshot preservado, nada enviado "
            f"(use permitir_vazio=True para zerar de propósito)"
        )
        return ClientesSyncResult(
            itens=0, extraido_em=extraido_em,
            descartados_cpf=extr.descartados_cpf,
            descartados_invalidos=extr.descartados_invalidos,
            colisoes_dedup=extr.colisoes_dedup,
            skipped_empty=True,
        )

    env_ctx = nullcontext(_env_conn) if _env_conn is not None else router.env_connect(slug)
    with env_ctx as env_conn:
        clientes_fire_repo.replace_all(env_conn, extr.clientes, extraido_em=extraido_em)

    base = dict(
        itens=len(extr.clientes), extraido_em=extraido_em,
        descartados_cpf=extr.descartados_cpf,
        descartados_invalidos=extr.descartados_invalidos,
        colisoes_dedup=extr.colisoes_dedup,
    )

    if not getattr(cfg, "clientes_push", False):
        logger.info(f"clientes sync env={slug}: envio ao Flow DESLIGADO (clientes_push=0)")
        return ClientesSyncResult(**base)

    client = _client or _build_client(cfg)
    try:
        request = build_clientes_request(
            extr.clientes, dry_run=dry_run, full_sync=full_sync,
            importador_versao=_IMPORTADOR_VERSAO, extraido_em=extraido_em,
        )
        rep = client.send_clientes(request)
        return ClientesSyncResult(**base, reconciliacao=rep)
    finally:
        if _client is None:
            client.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_clientes_sync.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
ruff check app/integrations/flowpcp/clientes_sync.py tests/test_clientes_sync.py && ruff format app/integrations/flowpcp/clientes_sync.py tests/test_clientes_sync.py
git add app/integrations/flowpcp/clientes_sync.py tests/test_clientes_sync.py
git commit -m "feat(flowpcp): run_clientes_sync (trava de vazio + contadores)"
```

---

### Task 9: Rota admin `sync-clientes`

**Files:**
- Modify: `app/web/routes_environments.py`
- Test: `tests/test_route_sync_clientes.py`

**Interfaces:**
- Consumes: `run_clientes_sync`/`ClientesSyncResult` (Task 8).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_route_sync_clientes.py
from app.integrations.flowpcp import clientes_sync
from app.integrations.flowpcp.clientes_sync import ClientesSyncResult
from app.web import routes_environments


def test_route_returns_counters_local_only(monkeypatch):
    monkeypatch.setattr(routes_environments.environments_repo, "get",
                        lambda env_id: {"id": env_id, "slug": "mm", "flowpcp_enabled": 1})
    monkeypatch.setattr(clientes_sync, "run_clientes_sync",
                        lambda slug, **kw: ClientesSyncResult(
                            itens=5, extraido_em="2026-07-17T12:00:00Z",
                            descartados_cpf=3, descartados_invalidos=1, colisoes_dedup=2))
    body = routes_environments.sync_clientes_flowpcp("env1", apply=False, _=None)
    assert body["local_only"] is True
    assert body["itens"] == 5
    assert body["descartados_cpf"] == 3
    assert body["colisoes_dedup"] == 2
    assert "reconciliacao" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_route_sync_clientes.py -v`
Expected: FAIL — `AttributeError: module 'app.web.routes_environments' has no attribute 'sync_clientes_flowpcp'`

- [ ] **Step 3: Write minimal implementation**

Em `app/web/routes_environments.py`, adicionar a rota (logo após `sync_catalogo_flowpcp`, antes do `@router.delete`):
```python
@router.post("/{env_id}/flowpcp/sync-clientes")
def sync_clientes_flowpcp(env_id: str, apply: bool = False, _=Depends(require_admin)):
    """Carga de clientes ativos (Fire → FlowPCP), direção IDA.

    Lê os clientes com pedido nos últimos 12 meses do Fire do ambiente.
    - `apply=false` (default): dry-run — reconcilia/relatório, não grava no Flow.
    - `apply=true`: promove (exige o `/clientes` do Flow no ar).
    A cópia local (`clientes_fire`) é sempre atualizada. Blocking → threadpool.
    """
    env = environments_repo.get(env_id)
    if not env:
        raise HTTPException(404, "Ambiente não encontrado")
    if not env.get("flowpcp_enabled"):
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    from app.integrations.flowpcp.clientes_sync import run_clientes_sync

    try:
        res = run_clientes_sync(env["slug"], dry_run=not apply, full_sync=False)
    except Exception as exc:  # noqa: BLE001 — vira erro HTTP legível pro operador
        raise HTTPException(502, f"Falha na carga de clientes: {exc}") from exc
    if res is None:
        raise HTTPException(409, "FlowPCP não está habilitado neste ambiente")

    body = {
        "local_only": res.reconciliacao is None,
        "skipped_empty": res.skipped_empty,
        "itens": res.itens,
        "extraido_em": res.extraido_em,
        "descartados_cpf": res.descartados_cpf,
        "descartados_invalidos": res.descartados_invalidos,
        "colisoes_dedup": res.colisoes_dedup,
    }
    if res.reconciliacao is not None:
        body["reconciliacao"] = res.reconciliacao.model_dump()
    return body
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_route_sync_clientes.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Suíte completa + commit**

```bash
.venv/bin/pytest tests/ -v
```
Expected: toda a suíte PASS (48 anteriores + os novos).

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/web/routes_environments.py tests/test_route_sync_clientes.py
git commit -m "feat(web): rota sync-clientes (dry-run/apply, contadores na resposta)"
```

---

## Notas de fechamento

- **Endpoint do Flow (`POST /api/portal-pedidos/clientes`)** — dependência externa (pcp-app). Até existir, tudo roda com `clientes_push=OFF` (local-only) e a rota `apply=true` devolve 502 legível. Fechar o contrato de wire (B1/I7/formato de resposta) com o time do Flow antes de eles codarem.
- **UI (botão "Sincronizar clientes")** — o backend está pronto (rota + gate). O botão em `static/` espelha o de catálogo; adicionar quando for expor ao operador (fora deste plano, é frontend puro).
- **Follow-ons (YAGNI)** — agendamento noturno, inativação de quem sai da janela, migração das cópias antigas de `_cnpj_digits` para o helper canônico, campos fantasia/cidade/uf.
