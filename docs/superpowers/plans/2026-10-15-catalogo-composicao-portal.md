# Composição de kits e código do Apontaê no catálogo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O catálogo que o Portal manda ao Flow passa a levar, atrás de um interruptor que nasce desligado, a composição de cada kit (`componentes`) e o código do par no Apontaê (`codigoMes`).

**Architecture:** O extract do Fire ganha o `CODPROD_ALTERN` e uma leitura separada de `PRODUTOS_KIT`. Um módulo puro (`catalogo_composicao.py`) aplica as regras de segurança da spec e decide, por SEQ, se a chave `componentes` vai com lista, com `[]` ou ausente. O mapper monta os campos, e o schema tira do JSON as chaves que não foram preenchidas. Com o interruptor desligado, o payload fica idêntico ao de hoje.

**Tech Stack:** Python 3.11, pydantic v2, firebirdsql/firebird-driver (Fire 2.5), pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-15-catalogo-composicao-portal-design.md` (par do Flow: `pcp-app` `docs/superpowers/specs/2026-10-15-catalogo-erp-fase2-composicao-vinculo-mes-design.md`).

## Global Constraints

- Contrato aditivo em `catalogo.produtos.v1`: `componentes: [{erpProdutoId: string, quantidade: number}]`, um nível só, no máximo 50 por item, `0 < quantidade ≤ 100000`.
- Chave `componentes` **ausente** = "não mexer"; `[]` = "o ERP afirma que não tem composição"; lista = "a composição é exatamente esta".
- `codigoMes`: só em `tipo == "simples"`, e só quando `CODPROD_ALTERN` sem espaços for só dígitos ASCII; senão `null`. Kit e pack: sempre `null`.
- Leitura de `PRODUTOS_KIT` falhou → chave `componentes` ausente em **todos** os itens do lote.
- Interruptor `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO`: só `"1"` liga. Desligado = payload idêntico ao de hoje (nem `componentes` nem `codigoMes` no JSON).
- Nenhuma migração de banco no Portal; a cópia local `catalogo_fire` não muda.
- Diff mínimo, sem reformatação fora do escopo (rodar `ruff format` só nos arquivos tocados e desfazer o que cair fora do escopo).
- Testes: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider <arquivo>` a partir do worktree `importar pedidos-sp1`; lint com `"../importar pedidos/.venv/bin/ruff" check app tests`.

## Review Focus

1. Leitura de `PRODUTOS_KIT` falhando no meio do sync (Fire caiu, timeout): nenhum item pode sair com `componentes: []`, porque o Flow apagaria a composição boa. → teste na Task 4.
2. Interruptor desligado: o JSON tem que ser byte a byte igual ao de antes desta entrega (nenhuma chave nova, nem `null`). → teste na Task 4.
3. Mesmo número de itens com conteúdo diferente (composição mudou e contagem igual): a chave de idempotência não pode repetir, ou o Flow devolve o relatório velho. → teste na Task 4.
4. Kit cujo par foi tirado com `excluir=`: o kit vai sem a chave `componentes`, e não com a composição apontando para um par ausente. → teste na Task 2.
5. Código alternativo numérico em kit (código de cliente, 116 casos na MM) e par com espaços em volta do número: kit → `null`; par → número sem espaços. → teste na Task 2.

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `app/integrations/flowpcp/catalogo_schema.py` | modelos do wire: `CatalogoComponente`, campos novos do item e omissão de chave não preenchida |
| `app/integrations/flowpcp/catalogo_composicao.py` (novo) | regras puras: `envio_ligado()`, `codigo_mes()`, `montar_composicao()` |
| `app/erp/queries.py` | `CODPROD_ALTERN` nas duas consultas do catálogo + `LIST_PRODUTOS_KIT_COMPOSICAO` |
| `app/erp/catalog_extract.py` | `ProdutoFireDTO.codigo_alternativo` + `extract_composicao()` |
| `app/integrations/flowpcp/catalogo_mapper.py` | monta `componentes`/`codigoMes` quando recebe a composição |
| `app/integrations/flowpcp/catalogo_sync.py` | lê a composição só com o interruptor ligado; falha vira "ausente em todos"; log do resumo |
| `app/integrations/flowpcp/client.py` | chave de idempotência do catálogo com hash do conteúdo |

---

### Task 1: Wire — campos novos e omissão de chave

**Files:**
- Modify: `app/integrations/flowpcp/catalogo_schema.py:1-21`
- Test: `tests/test_flowpcp_catalogo_mapper.py` (acrescentar no fim)

**Interfaces:**
- Produces: `CatalogoComponente(erpProdutoId: str, quantidade: float)`; `CatalogoProdutoItem.componentes: list[CatalogoComponente] | None` e `CatalogoProdutoItem.codigoMes: str | None`. Campo **não passado** no construtor não aparece em `model_dump(by_alias=True)`; campo passado (mesmo `None` ou `[]`) aparece.

- [ ] **Step 1: Write the failing test**

```python
# no fim de tests/test_flowpcp_catalogo_mapper.py
from app.integrations.flowpcp.catalogo_schema import CatalogoComponente, CatalogoProdutoItem


def _item(**extra):
    return CatalogoProdutoItem(
        fireProdutoId="2544", codigo="2544", nome="KIT 6", ativo=True, tipo="kit", **extra
    )


def test_wire_sem_campos_novos_e_igual_ao_de_hoje():
    corpo = _item().model_dump(by_alias=True)
    assert "componentes" not in corpo and "codigoMes" not in corpo


def test_wire_lista_vazia_e_null_aparecem():
    corpo = _item(componentes=[], codigoMes=None).model_dump(by_alias=True)
    assert corpo["componentes"] == [] and corpo["codigoMes"] is None


def test_wire_componentes_em_camel_case():
    corpo = _item(
        componentes=[CatalogoComponente(erpProdutoId="3306", quantidade=2.0)]
    ).model_dump(by_alias=True)
    assert corpo["componentes"] == [{"erpProdutoId": "3306", "quantidade": 2.0}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py`
Expected: FAIL com `ImportError: cannot import name 'CatalogoComponente'`.

- [ ] **Step 3: Write minimal implementation**

Em `app/integrations/flowpcp/catalogo_schema.py`, trocar o import e o bloco de `CatalogoProdutoItem` por:

```python
from pydantic import BaseModel, ConfigDict, Field, model_serializer


class CatalogoComponente(BaseModel):
    """Um filho da composição (PRODUTOS_KIT). Nome pelo papel (P-PROD-08 do Flow)."""

    model_config = ConfigDict(populate_by_name=True)

    erpProdutoId: str  # noqa: N815 — SEQ do filho
    quantidade: float


# Chaves que só vão no JSON quando o mapper as preencheu: ausente ≠ [] ≠ null
# (ausente = "não mexer" no Flow). Com o interruptor desligado o payload fica
# igual ao de antes da composição.
_SO_QUANDO_PREENCHIDO = ("componentes", "codigoMes")


class CatalogoProdutoItem(BaseModel):
    """Item de identidade do produto (Fire é dono). camelCase no wire."""

    model_config = ConfigDict(populate_by_name=True)

    fireProdutoId: str = Field(alias="fireProdutoId")  # noqa: N815 — PK imutável do Fire (SEQ)
    codigo: str | None = (
        None  # str(SEQ) — o sequencial que o cliente usa (== fireProdutoId). CODPROD_ALTERN NÃO é usado.
    )
    nome: str
    unidade: str | None = None
    ean: str | None = None
    ativo: bool
    tipo: str | None = (
        None  # 'pack' | 'kit' | 'simples' — catalog_extract._tipo (CODTIPOPROD do Fire não é usado).
    )
    componentes: list[CatalogoComponente] | None = None
    codigoMes: str | None = None  # noqa: N815 — CODPROD_ALTERN só dígitos, só em par

    @model_serializer(mode="wrap")
    def _omite_nao_preenchidos(self, handler):
        corpo = handler(self)
        for campo in _SO_QUANDO_PREENCHIDO:
            if campo not in self.model_fields_set:
                corpo.pop(campo, None)
        return corpo
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py`
Expected: PASS (os testes antigos do mapper e do sync continuam passando).

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_schema.py tests/test_flowpcp_catalogo_mapper.py
git commit -m "feat(catalogo): componentes e codigoMes no wire, omitidos quando não preenchidos"
```

---

### Task 2: Regras puras da composição

**Files:**
- Create: `app/integrations/flowpcp/catalogo_composicao.py`
- Test: `tests/test_flowpcp_catalogo_composicao.py`

**Interfaces:**
- Consumes: `ProdutoFireDTO` (com `codigo_alternativo`, criado na Task 3 com default `None`; neste task os testes passam `codigo_alternativo=` por keyword — por isso a Task 3 adiciona o campo; se executar a Task 2 antes, crie o campo no DTO aqui mesmo, ver Step 3b).
- Produces:
  - `envio_ligado() -> bool`
  - `codigo_mes(tipo: str, codigo_alternativo: str | None) -> str | None`
  - `ComposicaoDoEnvio(por_seq: dict[str, list[tuple[str, float]]], omitidos: dict[str, str], leitura_ok: bool)`
  - `montar_composicao(dtos: list[ProdutoFireDTO], lida: dict[str, list[tuple[str, float]]] | None) -> ComposicaoDoEnvio` — `dtos` já é a lista **enviada** (sem os `fora_do_envio`); `lida=None` = leitura falhou.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_catalogo_composicao.py
import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_composicao as cc


def _p(seq, tipo="simples", alt=None):
    return ProdutoFireDTO(seq, seq, f"P{seq}", "PARES", None, True, tipo, codigo_alternativo=alt)


@pytest.mark.parametrize("valor,ligado", [("1", True), ("0", False), ("", False), ("sim", False)])
def test_interruptor_so_liga_com_1(monkeypatch, valor, ligado):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", valor)
    assert cc.envio_ligado() is ligado


def test_interruptor_ausente_e_desligado(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    assert cc.envio_ligado() is False


@pytest.mark.parametrize(
    "tipo,alt,esperado",
    [
        ("simples", "8366", "8366"),
        ("simples", " 113647 ", "113647"),
        ("simples", "SAM´S-BP34", None),
        ("simples", "", None),
        ("simples", None, None),
        ("simples", "٣١", None),  # dígito não ASCII
        ("kit", "986388014917", None),  # código de cliente em kit
        ("pack", "8366", None),
    ],
)
def test_codigo_mes(tipo, alt, esperado):
    assert cc.codigo_mes(tipo, alt) == esperado


def test_composicao_valida_vazia_e_sem_linha():
    dtos = [_p("2544", "kit"), _p("3306"), _p("3364"), _p("2133", "kit")]
    lida = {"2544": [("3306", 2.0), ("3364", 4.0)]}
    r = cc.montar_composicao(dtos, lida)
    assert r.leitura_ok is True
    assert r.por_seq["2544"] == [("3306", 2.0), ("3364", 4.0)]
    assert r.por_seq["2133"] == []  # kit sem composição: o ERP afirma vazio
    assert r.por_seq["3306"] == []  # par: sem composição
    assert r.omitidos == {}


def test_leitura_falhou_nenhum_item_leva_a_chave():
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], None)
    assert r.leitura_ok is False and r.por_seq == {} and r.omitidos == {}


def test_filho_fora_do_envio_omite_a_chave_do_pai():
    # 3174 saiu com excluir=; o kit 3381 não pode afirmar composição com par ausente
    dtos = [_p("3381", "kit"), _p("3170")]
    r = cc.montar_composicao(dtos, {"3381": [("3174", 1.0), ("3170", 1.0)]})
    assert "3381" not in r.por_seq
    assert r.omitidos["3381"] == "filho fora do envio: 3174"


@pytest.mark.parametrize(
    "linhas,motivo",
    [
        ([("3306", 1.0), ("3306", 1.0)], "filho repetido: 3306"),
        ([("3306", 0.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("3306", -1.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("3306", 100001.0)], "quantidade fora de 0 < q <= 100000: 3306"),
        ([("2544", 1.0)], "kit dentro dele mesmo"),
    ],
)
def test_composicao_invalida_omite_com_motivo(linhas, motivo):
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], {"2544": linhas})
    assert "2544" not in r.por_seq and r.omitidos["2544"] == motivo


def test_mais_de_50_filhos_omite():
    filhos = [_p(str(4000 + i)) for i in range(51)]
    lida = {"2544": [(f.fire_produto_id, 1.0) for f in filhos]}
    r = cc.montar_composicao([_p("2544", "kit"), *filhos], lida)
    assert r.omitidos["2544"] == "mais de 50 filhos (51)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_composicao.py`
Expected: FAIL com `ModuleNotFoundError: ... catalogo_composicao` (ou `TypeError: ... codigo_alternativo` se a Task 3 ainda não rodou).

- [ ] **Step 3: Write minimal implementation**

```python
# app/integrations/flowpcp/catalogo_composicao.py
"""Composição de kits e código do Apontaê no catálogo enviado ao Flow.

Regra-mãe (spec 2026-10-15-catalogo-composicao-portal-design.md): o Portal só
afirma o que leu. Chave `componentes` ausente = "não mexer" no Flow; `[]` = o
Fire afirma que não tem composição (o Flow remove a dele); lista = a composição
é exatamente esta. Na dúvida, omite. Puro: sem Fire, sem rede.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from app.erp.catalog_extract import ProdutoFireDTO

MAX_FILHOS = 50
MAX_QUANTIDADE = 100_000
_SO_DIGITOS = re.compile(r"[0-9]+")


def envio_ligado() -> bool:
    """`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=1` liga. Chave de migração: só liga
    depois de o Flow aceitar os campos (Fatia A); antes disso o Flow recusa o
    lote inteiro com 400."""
    return os.environ.get("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", "").strip() == "1"


def codigo_mes(tipo: str, codigo_alternativo: str | None) -> str | None:
    """Código do par no Apontaê = CODPROD_ALTERN só dígitos, e só em par: na MM,
    116 kits têm código alternativo numérico que é código de cliente (0 batem com
    o Apontaê), contra 405 de 410 pares que batem (15/10/2026)."""
    if tipo != "simples" or not codigo_alternativo:
        return None
    valor = codigo_alternativo.strip()
    return valor if _SO_DIGITOS.fullmatch(valor) else None


@dataclass(frozen=True)
class ComposicaoDoEnvio:
    por_seq: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    omitidos: dict[str, str] = field(default_factory=dict)
    leitura_ok: bool = True


def _problema(seq: str, linhas: list[tuple[str, float]], enviados: set[str]) -> str | None:
    filhos = [f for f, _ in linhas]
    if seq in filhos:
        return "kit dentro dele mesmo"
    if len(linhas) > MAX_FILHOS:
        return f"mais de {MAX_FILHOS} filhos ({len(linhas)})"
    repetidos = sorted({f for f in filhos if filhos.count(f) > 1}, key=int)
    if repetidos:
        return f"filho repetido: {', '.join(repetidos)}"
    fora_faixa = [f for f, q in linhas if not 0 < q <= MAX_QUANTIDADE]
    if fora_faixa:
        return f"quantidade fora de 0 < q <= {MAX_QUANTIDADE}: {', '.join(fora_faixa)}"
    ausentes = [f for f in filhos if f not in enviados]
    if ausentes:
        return f"filho fora do envio: {', '.join(ausentes)}"
    return None


def montar_composicao(
    dtos: list[ProdutoFireDTO], lida: dict[str, list[tuple[str, float]]] | None
) -> ComposicaoDoEnvio:
    """`dtos` = o que vai ao Flow nesta rodada (já sem `fora_do_envio`).
    `lida=None` = a leitura de PRODUTOS_KIT falhou: nenhum item leva a chave."""
    if lida is None:
        return ComposicaoDoEnvio(leitura_ok=False)
    enviados = {d.fire_produto_id for d in dtos}
    por_seq: dict[str, list[tuple[str, float]]] = {}
    omitidos: dict[str, str] = {}
    for d in dtos:
        linhas = lida.get(d.fire_produto_id, [])
        motivo = _problema(d.fire_produto_id, linhas, enviados) if linhas else None
        if motivo:
            omitidos[d.fire_produto_id] = motivo
        else:
            por_seq[d.fire_produto_id] = linhas
    return ComposicaoDoEnvio(por_seq=por_seq, omitidos=omitidos)
```

- [ ] **Step 3b (só se a Task 3 ainda não rodou): campo no DTO**

Em `app/erp/catalog_extract.py`, no fim do `ProdutoFireDTO`:

```python
    codigo_alternativo: str | None = None  # CODPROD_ALTERN (código do par no Apontaê, se par)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_composicao.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_composicao.py tests/test_flowpcp_catalogo_composicao.py app/erp/catalog_extract.py
git commit -m "feat(catalogo): regras da composição e do codigoMes (puro, falha fechada)"
```

---

### Task 3: Extract — código alternativo e leitura de PRODUTOS_KIT

**Files:**
- Modify: `app/erp/queries.py` (`LIST_PRODUTOS_CATALOGO`, `LIST_PRODUTOS_CATALOGO_MEIAS`; nova `LIST_PRODUTOS_KIT_COMPOSICAO`)
- Modify: `app/erp/catalog_extract.py`
- Test: `tests/test_catalog_extract.py`

**Interfaces:**
- Produces: `ProdutoFireDTO.codigo_alternativo: str | None = None` (último campo, com default: as construções posicionais de 7 argumentos em `tests/` continuam válidas); `extract_composicao(fire_conn) -> dict[str, list[tuple[str, float]]]` (levanta a exceção do driver em falha — quem decide o que fazer é o sync).

- [ ] **Step 1: Write the failing test**

Em `tests/test_catalog_extract.py`, as linhas falsas passam a ter 7 colunas (a 7ª é `CODPROD_ALTERN`). Atualizar **todas** as tuplas `rows = [...]` existentes do arquivo acrescentando `, None` no fim de cada tupla, e acrescentar:

```python
from app.erp.catalog_extract import extract_composicao
from app.erp.queries import LIST_PRODUTOS_KIT_COMPOSICAO


def test_extract_traz_codigo_alternativo_limpo():
    rows = [
        (3759, "SAM'S 34/38 BCO C/ LISTRAS PTO C.A.", "PARES", None, "Nao", 0, " 113649 "),
        (3174, "BRANCO COM PRETO", "PAR", None, "Nao", 0, "  "),
    ]
    out = extract_produtos(_FakeConn(rows))
    assert out[0].codigo_alternativo == "113649"
    assert out[1].codigo_alternativo is None


class _FakeKitCursor(_FakeCursor):
    pass


def test_extract_composicao_agrupa_por_pai_na_ordem_lida():
    conn = _FakeConn([(2544, 3306, 2.0), (2544, 3364, 2.0), (2406, 3712, 3)])
    out = extract_composicao(conn)
    assert conn._cur.executed == LIST_PRODUTOS_KIT_COMPOSICAO
    assert out == {"2544": [("3306", 2.0), ("3364", 2.0)], "2406": [("3712", 3.0)]}


def test_consultas_do_catalogo_trazem_codprod_altern():
    for sql in (LIST_PRODUTOS_CATALOGO, LIST_PRODUTOS_CATALOGO_MEIAS):
        assert "P.CODPROD_ALTERN" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_catalog_extract.py`
Expected: FAIL (`ImportError: extract_composicao` / `ValueError: too many values to unpack`).

- [ ] **Step 3: Write minimal implementation**

`app/erp/queries.py` — nas duas consultas, trocar a primeira linha do SELECT final

```sql
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
```

e o fim da expressão `IS_KIT`

```sql
           ) THEN 1 ELSE 0 END AS IS_KIT
```

por

```sql
           ) THEN 1 ELSE 0 END AS IS_KIT,
           P.CODPROD_ALTERN
```

(o `SELECT` inicial fica igual; `CODPROD_ALTERN` vira a 7ª coluna). E acrescentar logo depois de `LIST_PRODUTOS_CATALOGO_MEIAS`:

```python
# Composição (PRODUTOS_KIT) para o catálogo enviado ao Flow: pai, filho e
# quantidade, um nível (o Flow explode recursivo). Sem filtro: são ~500 linhas na
# MM, e quem decide o que vai é catalogo_composicao. ORDER BY CODIGO mantém a
# ordem de cadastro, que vira a posição do filho no Flow.
LIST_PRODUTOS_KIT_COMPOSICAO = """
    SELECT K.CODPRODUTO_PAI, K.CODPRODUTO, K.QTD
    FROM PRODUTOS_KIT K
    ORDER BY K.CODPRODUTO_PAI, K.CODIGO
"""
```

`app/erp/catalog_extract.py`:

```python
from app.erp.queries import (
    LIST_PRODUTOS_CATALOGO,
    LIST_PRODUTOS_CATALOGO_MEIAS,
    LIST_PRODUTOS_KIT_COMPOSICAO,
)
```

no `ProdutoFireDTO`, último campo:

```python
    codigo_alternativo: str | None = None  # CODPROD_ALTERN (código do par no Apontaê, se par)
```

no laço de `extract_produtos`, trocar o desempacotamento e acrescentar o campo:

```python
    for seq, desc, uni, ean, bloqueado, is_kit, alternativo in rows:
        ...
                tipo=_tipo(is_kit, _clean(uni), nome),
                codigo_alternativo=_clean(alternativo),
```

e no fim do arquivo:

```python
def extract_composicao(fire_conn) -> dict[str, list[tuple[str, float]]]:
    """PRODUTOS_KIT inteiro, agrupado por pai: {SEQ do pai: [(SEQ do filho, QTD)]},
    na ordem de cadastro. Read-only. Erro do driver sobe: o sync trata a falha como
    "composição ausente em todos" (nunca como "sem composição")."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_KIT_COMPOSICAO)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: dict[str, list[tuple[str, float]]] = {}
    for pai, filho, qtd in rows:
        out.setdefault(str(pai), []).append((str(filho), float(qtd)))
    return out
```

Se a Task 2 já criou o campo `codigo_alternativo`, não duplicar.

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_catalog_extract.py tests/test_catalogo_fire_repo.py tests/test_web_server.py -k "catalog or extract or produto"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/queries.py app/erp/catalog_extract.py tests/test_catalog_extract.py
git commit -m "feat(catalogo): extract lê CODPROD_ALTERN e a composição (PRODUTOS_KIT)"
```

---

### Task 4: Mapper, sync e idempotência

**Files:**
- Modify: `app/integrations/flowpcp/catalogo_mapper.py`
- Modify: `app/integrations/flowpcp/catalogo_sync.py`
- Modify: `app/integrations/flowpcp/client.py:116-118`
- Test: `tests/test_catalogo_sync.py`, `tests/test_flowpcp_catalogo_mapper.py`

**Interfaces:**
- Consumes: `CatalogoComponente`, `CatalogoProdutoItem` (Task 1); `envio_ligado`, `codigo_mes`, `montar_composicao`, `ComposicaoDoEnvio` (Task 2); `extract_composicao`, `ProdutoFireDTO.codigo_alternativo` (Task 3).
- Produces: `build_catalogo_request(..., composicao: ComposicaoDoEnvio | None = None)`; `run_catalogo_sync` igual por fora (mesma assinatura e retorno).

- [ ] **Step 1: Write the failing tests**

Em `tests/test_flowpcp_catalogo_mapper.py`:

```python
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request


def _dtos():
    return [
        ProdutoFireDTO("2544", "2544", "KIT 6", "KIT", None, True, "kit", codigo_alternativo="986388"),
        ProdutoFireDTO("3306", "3306", "PAR PTO", "PARES", None, True, "simples", codigo_alternativo="8366"),
        ProdutoFireDTO("3381", "3381", "KIT C/5", "KIT", None, True, "kit"),
    ]


def _req(composicao):
    return build_catalogo_request(
        _dtos(), dry_run=True, full_sync=True, importador_versao="1", extraido_em="t",
        composicao=composicao,
    ).model_dump(by_alias=True)


def test_mapper_sem_composicao_nao_manda_campos_novos():
    for item in _req(None)["itens"]:
        assert "componentes" not in item and "codigoMes" not in item


def test_mapper_com_composicao():
    comp = ComposicaoDoEnvio(
        por_seq={"2544": [("3306", 6.0)], "3306": []}, omitidos={"3381": "filho fora do envio: 3174"}
    )
    kit, par, c5 = _req(comp)["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert kit["codigoMes"] is None  # kit com código de cliente numérico
    assert par["componentes"] == [] and par["codigoMes"] == "8366"
    assert "componentes" not in c5 and c5["codigoMes"] is None


def test_mapper_leitura_falhou_so_codigo_mes():
    itens = _req(ComposicaoDoEnvio(leitura_ok=False))["itens"]
    assert all("componentes" not in i for i in itens)
    assert itens[1]["codigoMes"] == "8366"
```

Em `tests/test_catalogo_sync.py`:

```python
import json


class _FireComKit:
    """Cursor fake: PRODUTOS_KIT devolve linhas ou falha."""

    def __init__(self, linhas=None, falha=False):
        self.linhas, self.falha = linhas or [], falha

    def cursor(self):
        dono = self

        class _C:
            def execute(self, sql):
                if dono.falha:
                    raise RuntimeError("Fire caiu")

            def fetchall(self):
                return dono.linhas

            def close(self):
                pass

        return _C()


def _sync_capturando(monkeypatch, *, ligado, fire, dtos):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", "1" if ligado else "0")
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    cliente = _FakeClient()
    catalogo_sync.run_catalogo_sync(
        "mm", now_iso="t", _client=cliente, _fire_conn=fire, _env_conn=_FakeEnvConn()
    )
    return cliente.sent.model_dump(by_alias=True)


_DTOS_KIT = [
    ProdutoFireDTO("2544", "2544", "KIT", "KIT", None, True, "kit"),
    ProdutoFireDTO("3306", "3306", "PAR", "PARES", None, True, "simples", codigo_alternativo="8366"),
]


def test_sync_desligado_payload_identico_ao_de_hoje(monkeypatch):
    corpo = _sync_capturando(monkeypatch, ligado=False, fire=_FireComKit([(2544, 3306, 6)]), dtos=_DTOS_KIT)
    esperado = {
        "fireProdutoId": "2544", "codigo": "2544", "nome": "KIT", "unidade": "KIT",
        "ean": None, "ativo": True, "tipo": "kit",
    }
    assert json.dumps(corpo["itens"][0], sort_keys=True) == json.dumps(esperado, sort_keys=True)


def test_sync_ligado_manda_composicao(monkeypatch):
    corpo = _sync_capturando(monkeypatch, ligado=True, fire=_FireComKit([(2544, 3306, 6)]), dtos=_DTOS_KIT)
    kit, par = corpo["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert par["componentes"] == [] and par["codigoMes"] == "8366"


def test_sync_leitura_da_composicao_falhou_nenhum_item_com_a_chave(monkeypatch):
    corpo = _sync_capturando(monkeypatch, ligado=True, fire=_FireComKit(falha=True), dtos=_DTOS_KIT)
    assert all("componentes" not in i for i in corpo["itens"])
    assert corpo["itens"][1]["codigoMes"] == "8366"
```

E um teste da chave de idempotência — em `tests/test_catalogo_sync.py`:

```python
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.client import _idem_catalogo


def test_idempotencia_muda_quando_o_conteudo_muda_com_mesma_contagem():
    a = build_catalogo_request(_DTOS_KIT, dry_run=True, full_sync=True, importador_versao="1", extraido_em="t")
    b_dtos = [_DTOS_KIT[0], ProdutoFireDTO("3306", "3306", "PAR NOVO", "PARES", None, True, "simples")]
    b = build_catalogo_request(b_dtos, dry_run=True, full_sync=True, importador_versao="1", extraido_em="t")
    assert _idem_catalogo(a) != _idem_catalogo(b)
    assert _idem_catalogo(a) == _idem_catalogo(a)  # estável em retry
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py`
Expected: FAIL (`unexpected keyword argument 'composicao'`, `cannot import name '_idem_catalogo'`).

- [ ] **Step 3: Write minimal implementation**

`app/integrations/flowpcp/catalogo_mapper.py`:

```python
from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio, codigo_mes
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def _item(d: ProdutoFireDTO, composicao: ComposicaoDoEnvio | None) -> CatalogoProdutoItem:
    campos = dict(
        fireProdutoId=d.fire_produto_id,
        codigo=d.codigo,
        nome=d.nome,
        unidade=d.unidade,
        ean=d.ean,
        ativo=d.ativo,
        tipo=d.tipo,
    )
    if composicao is not None:
        campos["codigoMes"] = codigo_mes(d.tipo, d.codigo_alternativo)
        if d.fire_produto_id in composicao.por_seq:
            campos["componentes"] = [
                CatalogoComponente(erpProdutoId=f, quantidade=q)
                for f, q in composicao.por_seq[d.fire_produto_id]
            ]
    return CatalogoProdutoItem(**campos)


def build_catalogo_request(
    dtos: list[ProdutoFireDTO],
    *,
    dry_run: bool,
    full_sync: bool,
    importador_versao: str,
    extraido_em: str,
    composicao: ComposicaoDoEnvio | None = None,
) -> CatalogoRequest:
    """`composicao=None` (interruptor desligado): payload igual ao de antes."""
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=[_item(d, composicao) for d in dtos],
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
    )
```

`app/integrations/flowpcp/catalogo_sync.py` — imports:

```python
from app.erp.catalog_extract import extract_composicao, extract_produtos
from app.integrations.flowpcp import catalogo_composicao
```

dentro do `with fire_ctx as fire_conn:`, depois do `extract_produtos`:

```python
        lida = None
        if getattr(cfg, "catalogo_push", False) and catalogo_composicao.envio_ligado():
            try:
                lida = extract_composicao(fire_conn)
            except Exception as exc:  # noqa: BLE001 — falha vira "ausente em todos", nunca "vazio"
                logger.error(f"catalogo sync env={slug}: leitura de PRODUTOS_KIT falhou: {exc!r}")
                lida = None
            leu_composicao = True
        else:
            leu_composicao = False
```

e, no lugar da montagem do request (depois de `enviados = [...]`):

```python
    composicao = None
    if leu_composicao:
        composicao = catalogo_composicao.montar_composicao(enviados, lida)
        com_lista = sum(1 for v in composicao.por_seq.values() if v)
        logger.info(
            f"catalogo sync env={slug} composicao: leitura_ok={composicao.leitura_ok} "
            f"com_lista={com_lista} vazia={len(composicao.por_seq) - com_lista} "
            f"omitidos={len(composicao.omitidos)}"
        )
        for seq, motivo in sorted(composicao.omitidos.items(), key=lambda kv: int(kv[0])):
            logger.warning(f"catalogo sync env={slug} composicao omitida SEQ {seq}: {motivo}")
```

e passar `composicao=composicao` no `build_catalogo_request(...)`.

`app/integrations/flowpcp/client.py` — acima da classe (perto dos outros helpers do módulo):

```python
def _idem_catalogo(request: CatalogoRequest) -> str:
    """Chave estável em retry e nova quando o conteúdo muda: só a contagem
    colidia (mesmos 1.026 itens com composição nova = relatório velho)."""
    corpo = json.dumps(request.model_dump(by_alias=True)["itens"], sort_keys=True, default=str)
    digest = hashlib.sha256(corpo.encode("utf-8")).hexdigest()[:16]
    return f"catalogo-{int(request.dryRun)}-{len(request.itens)}-{digest}"
```

e em `send_catalogo` trocar `idem = f"catalogo-{int(request.dryRun)}-{len(request.itens)}"` por `idem = _idem_catalogo(request)`. Acrescentar `import json` no topo (o `hashlib` já é importado e usado em `send_clientes`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py tests/test_flowpcp_catalogo_diario.py tests/test_admin_environments_routes.py tests/test_flowpcp_catalogo_client.py`
Expected: PASS. (Se `tests/test_flowpcp_catalogo_client.py` verificar a chave de idempotência antiga literalmente, atualizar a asserção para o formato novo `catalogo-<dry>-<n>-<hash16>`.)

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_mapper.py app/integrations/flowpcp/catalogo_sync.py app/integrations/flowpcp/client.py tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py tests/test_flowpcp_catalogo_client.py
git commit -m "feat(catalogo): manda composição e codigoMes atrás do interruptor; idempotência pelo conteúdo"
```

---

### Task 5: Docs, suíte, validação no Fire real e PR

**Files:**
- Modify: `docs/ai/modules/flowpcp.md` (§4, depois do parágrafo "Fora do envio")
- Modify: `CLAUDE.md` (bloco de variáveis de ambiente)
- Modify: `CHANGELOG.md` (seção `## Não publicado`)

- [ ] **Step 1: Doc do módulo**

Em `docs/ai/modules/flowpcp.md`, depois do parágrafo **Fora do envio**:

```markdown
**Composição e código do Apontaê** (`catalogo_composicao.py`, desde 15/10/2026, atrás de
`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=1`): cada item leva `componentes`
(`[{erpProdutoId, quantidade}]`, de `PRODUTOS_KIT`, um nível) e `codigoMes`
(`CODPROD_ALTERN` só dígitos, só em `simples`). Chave `componentes` ausente = "não mexer"
no Flow; `[]` = o Fire afirma que não tem; falha ao ler `PRODUTOS_KIT` = ausente em todos.
Composição com filho fora do envio, filho repetido, quantidade fora de 0 < q ≤ 100000,
mais de 50 filhos ou kit dentro dele mesmo: ausente naquele item, com log do motivo.
Só ligar depois de o Flow aceitar os campos (Fatia A); antes disso ele recusa o lote com 400.
Spec: `docs/superpowers/specs/2026-10-15-catalogo-composicao-portal-design.md`.
```

- [ ] **Step 2: CLAUDE.md**

No bloco de variáveis, logo depois das linhas de `PORTAL_FLOWPCP_CATALOGO_DIARIO`:

```env
PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=  # 1 manda composição dos kits e código do Apontaê no
                                     # catálogo. Só ligar com o Flow aceitando (Fatia A).
```

- [ ] **Step 3: CHANGELOG (seção `## Não publicado`, substituindo `_(nada ainda)_`)**

```markdown
1) **O catálogo pode levar ao Flow de que cada kit é feito e o código de cada par no Apontaê.** Fica desligado até o Flow estar pronto para receber; quando ligado, o Flow passa a saber quais pares vão em cada kit, sem cadastro manual, e qual par do Apontaê é cada par do Fire. Se o portal não conseguir ler a composição no Fire, ele não manda nada sobre composição, e o Flow mantém a que tem.
```

- [ ] **Step 4: Lint e suíte completa**

Run: `"../importar pedidos/.venv/bin/ruff" check app tests && "../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests`
Expected: `All checks passed!` e todos os testes passando.

- [ ] **Step 5: Validação real, só leitura, no Fire de produção da MM**

Script no scratchpad (nunca no repo), com `FB_PASSWORD` no ambiente, conexão `READ_COMMITED_RO`, assert de `SELECT` em todo SQL. Monta o request real com o código da branch e confere contra a medição de 15/10:

```python
import os, sys, collections, firebirdsql
sys.path.insert(0, sys.argv[1])
os.environ["PORTAL_FLOWPCP_CATALOGO_COMPOSICAO"] = "1"
from app.erp.catalog_extract import extract_produtos, extract_composicao
from app.integrations.flowpcp.catalogo_composicao import montar_composicao
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
c = firebirdsql.connect(host="192.168.15.7", port=3050, database=r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB",
                        user="SYSDBA", password=os.environ["FB_PASSWORD"], charset="ISO8859_1")
c.set_isolation_level(firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO)
dtos = extract_produtos(c, apenas_meias=True)
comp = montar_composicao(dtos, extract_composicao(c))
itens = build_catalogo_request(dtos, dry_run=True, full_sync=True, importador_versao="1",
                               extraido_em="t", composicao=comp).model_dump(by_alias=True)["itens"]
com_lista = sum(1 for i in itens if i.get("componentes"))
vazia = sum(1 for i in itens if i.get("componentes") == [] and i["tipo"] in ("kit", "pack"))
mes = sum(1 for i in itens if i.get("codigoMes"))
print(len(itens), com_lista, vazia, len(comp.omitidos), mes)
```

Expected (pode variar se o cadastro mudou depois de 15/10): `1026 271 321 0 410` — e nenhum `codigoMes` em item `kit`/`pack`. Registrar a saída no PR.

- [ ] **Step 6: Contra o Zod do Flow (quando a Fatia A estiver em `pcp-app` origin/main)**

Pedir à sessão do Flow (ou rodar via `tsx` com o `node_modules` do `pcp-app`, como no SP1) a validação de um lote real gerado no Step 5 contra o schema do `POST /api/portal-pedidos/catalogo`. Se a Fatia A ainda não estiver no ar, registrar no PR que este passo fica pendente e que o interruptor **não** pode ser ligado antes.

- [ ] **Step 7: Commit, PR e CI**

```bash
git add docs/ai/modules/flowpcp.md CLAUDE.md CHANGELOG.md
git commit -m "docs(catalogo): composição e codigoMes no módulo flowpcp, env e changelog"
git push -u origin <branch>
gh pr create --base main --title "feat(catalogo): composição dos kits e código do Apontaê no catálogo (desligado)"
```

Esperar o CI no SHA exato antes do merge; o merge e o pacote seguem o processo de release (tag só depois de MERGED, conferir o código no zip).
