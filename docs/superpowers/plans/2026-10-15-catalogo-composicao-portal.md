# Composição de kits e código do Apontaê no catálogo — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Revisão 2 do plano** (15/10/2026), depois da revisão independente da spec e da decisão C3 do Samuel. Muda em relação à revisão 1: kit/pack sem composição vai sem a chave (C3); leitura vazia ou incoerente conta como falha; linha com nulo; `codigoMes` só em par ativo, até 9 dígitos e sem repetição; conferência de quantidade contra o nome; interruptor por empresa; exclusão guardada; confirmações das travas; apply sem repetição em timeout; 409; tela e diário. Tarefas novas: 5 (cliente) e 6 (rota, diário e tela).

**Goal:** O catálogo que o Portal manda ao Flow passa a levar, atrás de um interruptor por empresa que nasce desligado, a composição de cada kit (`componentes`) e o código do par no Apontaê (`codigoMes`), sem nunca afirmar o que não leu.

**Architecture:** O extract do Fire ganha `CODPROD_ALTERN`, o `IS_KIT` e uma leitura separada de `PRODUTOS_KIT`. Um módulo puro (`catalogo_composicao.py`) aplica as regras da spec e decide, por SEQ, se `componentes` vai com lista, com `[]` ou sem a chave, e qual `codigoMes` vai. O mapper monta os campos; o schema tira do JSON o que não foi preenchido, então com o interruptor desligado o payload é idêntico ao de hoje. O apply passa a ter política de repetição própria (só repete se a conexão nem abriu) e timeout de 300 s; rota, diário e tela mostram o resultado.

**Tech Stack:** Python 3.11, pydantic v2, httpx + tenacity (`app/http`), firebirdsql/firebird-driver (Fire 2.5), FastAPI, HTML/JS puro na tela de admin, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-10-15-catalogo-composicao-portal-design.md` (revisão 3). Par do Flow: `pcp-app` `docs/superpowers/specs/2026-10-15-catalogo-erp-fase2-composicao-vinculo-mes-design.md`.

## Global Constraints

- Contrato aditivo em `catalogo.produtos.v1`: `componentes: [{erpProdutoId: string, quantidade: number}]`, um nível, no máximo 50 por item, `0 < quantidade ≤ 100000`.
- `componentes` **ausente** = "não mexer"; `[]` = "o ERP afirma que não tem composição"; lista = "a composição é exatamente esta".
- **C3:** kit/pack sem linha em `PRODUTOS_KIT` vai **sem a chave**. Só `simples` sem linha vai com `[]`.
- Leitura de `PRODUTOS_KIT` com erro, vazia, ou sem nenhum produto que o extract marcou com `IS_KIT=1` → chave ausente em **todos** os itens.
- `codigoMes`: só em `tipo == "simples"` **e** `ativo`; `CODPROD_ALTERN` sem espaços, só dígitos ASCII, 1 a 9 dígitos; valor repetido em mais de um par do mesmo envio → `null` em todos os repetidos. Senão `null`.
- Interruptor `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO`: lista de slugs separados por vírgula (`mm`); vazio/ausente = ninguém. Desligado para o slug = payload idêntico ao de hoje (nenhuma chave nova, nem `null`).
- `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>` (SEQs separados por vírgula): quando definida, sai do envio manual e do diário, com a composição ligada ou não. Valor inválido ou SEQ fora do extract: nada é enviado.
- `confirmarRemocaoComposicao` / `confirmarSubstituicaoComposicao`: topo do corpo, só com `dryRun=false`, só pela rota manual (`confirmar_remocao=1`, `confirmar_substituicao=1`). O diário nunca manda.
- Apply (`dryRun=false`): sem repetição depois que a conexão abriu (timeout de leitura incluído), timeout de 300 s. Dry-run: política de hoje.
- Nenhuma migração de banco no Portal; a cópia local `catalogo_fire` não muda.
- Diff mínimo, sem reformatação fora do escopo (rodar `ruff format` só nos arquivos tocados e desfazer o que cair fora do escopo).
- Testes: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider <arquivo>` a partir do worktree `importar pedidos-sp1`; lint com `"../importar pedidos/.venv/bin/ruff" check app tests`.

## Review Focus

1. Base errada ou cópia vazia do Fire: a leitura de `PRODUTOS_KIT` volta sem erro mas vazia (ou com outra base). Nenhum item pode sair com `[]` nem com lista; o resumo diz `leitura_ok=False` e o motivo. → testes na Task 2 e na Task 4.
2. Interruptor desligado, ou ligado só para outro slug: JSON byte a byte igual ao de antes, inclusive no topo do corpo (sem as confirmações). → teste na Task 4.
3. Apply demorado: o Flow processa e o Portal estoura o timeout de leitura. O Portal não pode mandar o apply de novo. → teste na Task 5.
4. Kit do listrado cujo par está na exclusão guardada: o kit vai sem a chave, no manual e no diário. → testes na Task 2 e na Task 4.
5. Código do Apontaê repetido em dois pares (cadastro duplicado) ou longo demais (código de cliente de 12 dígitos): `null` nos dois e log. → teste na Task 2.

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `app/integrations/flowpcp/catalogo_schema.py` | wire: `CatalogoComponente`, campos novos do item e do request, omissão de chave não preenchida |
| `app/integrations/flowpcp/catalogo_composicao.py` (novo) | regras puras: interruptor, exclusão guardada, `codigoMes`, composição, conferência com o nome, resumo |
| `app/erp/queries.py` | `CODPROD_ALTERN` nas duas consultas do catálogo + `LIST_PRODUTOS_KIT_COMPOSICAO` |
| `app/erp/catalog_extract.py` | `ProdutoFireDTO.codigo_alternativo`, `ProdutoFireDTO.tem_composicao` + `extract_composicao()` |
| `app/integrations/flowpcp/catalogo_mapper.py` | monta `componentes`/`codigoMes`/confirmações |
| `app/integrations/flowpcp/catalogo_sync.py` | interruptor, exclusão guardada, leitura da composição, resumo no relatório, mensagem de 400 |
| `app/http/policies.py` · `app/http/client.py` | `apply_post_policy()`; `post_json(..., retry_policy=)` |
| `app/integrations/flowpcp/client.py` | apply com política e timeout próprios; 409 vira `FlowPCPApplyEmAndamentoError`; chave de idempotência pelo conteúdo |
| `app/web/routes_environments.py` | `confirmar_remocao`, `confirmar_substituicao`; 409 |
| `app/integrations/flowpcp/catalogo_diario.py` | resumo com os blocos novos; WARNING |
| `app/web/static/admin-ambiente-edit.html` | blocos de composição, vínculo, travas e leitura; botão "Gravar liberando a trava" |

---

### Task 1: Wire — campos novos e omissão de chave

**Files:**
- Modify: `app/integrations/flowpcp/catalogo_schema.py`
- Test: `tests/test_flowpcp_catalogo_schema.py` (acrescentar no fim)

**Interfaces:**
- Produces: `CatalogoComponente(erpProdutoId: str, quantidade: float)`; `CatalogoProdutoItem.componentes: list[CatalogoComponente] | None`, `CatalogoProdutoItem.codigoMes: str | None`; `CatalogoRequest.confirmarRemocaoComposicao: bool | None`, `CatalogoRequest.confirmarSubstituicaoComposicao: bool | None`. Campo **não passado** no construtor não aparece em `model_dump(by_alias=True)`; campo passado (mesmo `None` ou `[]`) aparece.

- [ ] **Step 1: Write the failing test**

```python
# no fim de tests/test_flowpcp_catalogo_schema.py
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def _item(**extra):
    return CatalogoProdutoItem(
        fireProdutoId="2544", codigo="2544", nome="KIT 6", ativo=True, tipo="kit", **extra
    )


def _request(**extra):
    return CatalogoRequest(
        dryRun=False, fullSync=True, itens=[_item()],
        origem=CatalogoOrigem(importadorVersao="1", extraidoEm="t"), **extra,
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


def test_request_sem_confirmacoes_nao_tem_as_chaves():
    corpo = _request().model_dump(by_alias=True)
    assert "confirmarRemocaoComposicao" not in corpo
    assert "confirmarSubstituicaoComposicao" not in corpo
    assert corpo["itens"][0].keys() == _item().model_dump(by_alias=True).keys()


def test_request_com_confirmacao_no_topo_do_corpo():
    corpo = _request(confirmarSubstituicaoComposicao=True).model_dump(by_alias=True)
    assert corpo["confirmarSubstituicaoComposicao"] is True
    assert "confirmarRemocaoComposicao" not in corpo
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_schema.py`
Expected: FAIL com `ImportError: cannot import name 'CatalogoComponente'`.

- [ ] **Step 3: Write minimal implementation**

Em `app/integrations/flowpcp/catalogo_schema.py`, trocar o import e acrescentar antes de `CatalogoProdutoItem`:

```python
from pydantic import BaseModel, ConfigDict, Field, model_serializer


class CatalogoComponente(BaseModel):
    """Um filho da composição (PRODUTOS_KIT). Nome pelo papel (P-PROD-08 do Flow)."""

    model_config = ConfigDict(populate_by_name=True)

    erpProdutoId: str  # noqa: N815 — SEQ do filho
    quantidade: float


def _sem_os_nao_preenchidos(modelo: BaseModel, corpo: dict, campos: tuple[str, ...]) -> dict:
    """Chave só vai no JSON quando foi preenchida: ausente ≠ [] ≠ null (ausente =
    "não mexer" no Flow). Com o interruptor desligado o payload fica igual ao de
    antes da composição."""
    for campo in campos:
        if campo not in modelo.model_fields_set:
            corpo.pop(campo, None)
    return corpo
```

No fim de `CatalogoProdutoItem` (depois de `tipo`):

```python
    componentes: list[CatalogoComponente] | None = None
    codigoMes: str | None = None  # noqa: N815 — CODPROD_ALTERN só dígitos, só em par ativo

    @model_serializer(mode="wrap")
    def _omite_nao_preenchidos(self, handler):
        return _sem_os_nao_preenchidos(self, handler(self), ("componentes", "codigoMes"))
```

No fim de `CatalogoRequest` (depois de `origem`):

```python
    # Liberam as travas de remoção/substituição de composição do Flow. Só com
    # dryRun=false e só pela rota manual (o diário nunca manda).
    confirmarRemocaoComposicao: bool | None = None  # noqa: N815
    confirmarSubstituicaoComposicao: bool | None = None  # noqa: N815

    @model_serializer(mode="wrap")
    def _omite_nao_preenchidos(self, handler):
        return _sem_os_nao_preenchidos(
            self, handler(self), ("confirmarRemocaoComposicao", "confirmarSubstituicaoComposicao")
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_schema.py tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py tests/test_flowpcp_catalogo_client.py`
Expected: PASS (os testes antigos continuam passando).

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_schema.py tests/test_flowpcp_catalogo_schema.py
git commit -m "feat(catalogo): componentes, codigoMes e confirmações no wire, omitidos quando não preenchidos"
```

---

### Task 2: Regras puras da composição

**Files:**
- Modify: `app/erp/catalog_extract.py` (só os dois campos novos do DTO; a leitura vem na Task 3)
- Create: `app/integrations/flowpcp/catalogo_composicao.py`
- Test: `tests/test_flowpcp_catalogo_composicao.py`

**Interfaces:**
- Produces (DTO): `ProdutoFireDTO.codigo_alternativo: str | None = None`, `ProdutoFireDTO.tem_composicao: bool = False` (últimos campos, com default; as construções posicionais de 7 argumentos em `tests/` continuam válidas).
- Produces (módulo):
  - `Linha = tuple[str | None, float | None]`
  - `envio_ligado(slug: str) -> bool`
  - `excluir_guardado(slug: str) -> frozenset[str]` (levanta `ValueError` com valor inválido)
  - `codigo_mes(tipo: str, ativo: bool, codigo_alternativo: str | None) -> str | None`
  - `codigos_mes(dtos: list[ProdutoFireDTO]) -> tuple[dict[str, str | None], dict[str, list[str]]]` → (SEQ → código ou `None`, código repetido → SEQs)
  - `ComposicaoDoEnvio(por_seq, omitidos, sem_linha, divergencia_nome, leitura_ok, motivo_falha)`
  - `montar_composicao(dtos: list[ProdutoFireDTO], lida: dict[str, list[Linha]] | None) -> ComposicaoDoEnvio` — `dtos` é a lista **enviada** (sem os excluídos); `lida=None` = a leitura deu erro.
  - `resumo(comp: ComposicaoDoEnvio, codigos: dict[str, str | None], repetidos: dict[str, list[str]]) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_flowpcp_catalogo_composicao.py
import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_composicao as cc


def _p(seq, tipo="simples", alt=None, nome=None, ativo=True):
    return ProdutoFireDTO(
        seq, seq, nome or f"P{seq}", "PARES", None, ativo, tipo,
        codigo_alternativo=alt, tem_composicao=tipo != "simples",
    )


# ── interruptor e exclusão guardada ─────────────────────────────────────


@pytest.mark.parametrize(
    "valor,slug,ligado",
    [("mm", "mm", True), (" MM , nasmar ", "nasmar", True), ("mm", "nasmar", False),
     ("", "mm", False), ("1", "mm", False)],
)
def test_interruptor_por_slug(monkeypatch, valor, slug, ligado):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", valor)
    assert cc.envio_ligado(slug) is ligado


def test_interruptor_ausente_e_desligado(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    assert cc.envio_ligado("mm") is False


def test_excluir_guardado(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", " 3174, 03381 ,")
    assert cc.excluir_guardado("mm") == frozenset({"3174", "3381"})
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM")
    assert cc.excluir_guardado("mm") == frozenset()


def test_excluir_guardado_slug_com_hifen(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM_AMER", "1")
    assert cc.excluir_guardado("mm-amer") == frozenset({"1"})


@pytest.mark.parametrize("valor", ["3174;3381", "abc", "٣١", "12345678901"])
def test_excluir_guardado_invalido_falha_fechado(monkeypatch, valor):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", valor)
    with pytest.raises(ValueError, match="PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM"):
        cc.excluir_guardado("mm")


# ── codigoMes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tipo,ativo,alt,esperado",
    [
        ("simples", True, "8366", "8366"),
        ("simples", True, " 113647 ", "113647"),
        ("simples", True, "123456789", "123456789"),
        ("simples", True, "1234567890", None),  # 10 dígitos: não cabe no integer do Flow
        ("simples", True, "986388014917", None),  # código de cliente
        ("simples", True, "SAM´S-BP34", None),
        ("simples", True, "", None),
        ("simples", True, None, None),
        ("simples", True, "٣١", None),  # dígito não ASCII
        ("simples", False, "8366", None),  # par inativo
        ("kit", True, "8366", None),
        ("pack", True, "8366", None),
    ],
)
def test_codigo_mes(tipo, ativo, alt, esperado):
    assert cc.codigo_mes(tipo, ativo, alt) == esperado


def test_codigo_mes_repetido_vira_null_nos_dois():
    dtos = [_p("3759", alt="113649"), _p("3174", alt="113649"), _p("3306", alt="8366")]
    codigos, repetidos = cc.codigos_mes(dtos)
    assert codigos == {"3759": None, "3174": None, "3306": "8366"}
    assert repetidos == {"113649": ["3174", "3759"]}


# ── composição ──────────────────────────────────────────────────────────


def test_c3_kit_sem_linha_vai_sem_a_chave_e_par_sem_linha_vai_vazio():
    dtos = [_p("2544", "kit"), _p("3306"), _p("3364"), _p("2133", "kit"), _p("2645", "pack")]
    r = cc.montar_composicao(dtos, {"2544": [("3306", 2.0), ("3364", 4.0)]})
    assert r.leitura_ok is True
    assert r.por_seq["2544"] == [("3306", 2.0), ("3364", 4.0)]
    assert r.por_seq["3306"] == [] and r.por_seq["3364"] == []
    assert "2133" not in r.por_seq and "2645" not in r.por_seq
    assert r.sem_linha == ("2133", "2645")
    assert r.omitidos == {}


@pytest.mark.parametrize(
    "lida,motivo",
    [
        (None, "leitura de PRODUTOS_KIT falhou"),
        ({}, "leitura de PRODUTOS_KIT voltou vazia"),
        ({"9999": [("3306", 1.0)]}, "leitura incoerente: nenhum kit do catálogo está nela"),
    ],
)
def test_leitura_com_falha_nenhum_item_leva_a_chave(lida, motivo):
    r = cc.montar_composicao([_p("2544", "kit"), _p("3306")], lida)
    assert r.leitura_ok is False and r.motivo_falha == motivo
    assert r.por_seq == {} and r.omitidos == {} and r.sem_linha == ()


def test_catalogo_so_de_pares_com_leitura_vazia_e_falha_tambem():
    # sem kit nenhum no catálogo, leitura vazia ainda é suspeita: não afirmar [] em massa
    r = cc.montar_composicao([_p("3306")], {})
    assert r.leitura_ok is False


def test_filho_fora_do_envio_omite_a_chave_do_pai():
    # 3174 saiu pela exclusão; o kit 3381 não pode afirmar composição com par ausente
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
        ([(None, 1.0)], "linha com filho ou quantidade nulos"),
        ([("3306", None)], "linha com filho ou quantidade nulos"),
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


@pytest.mark.parametrize(
    "nome,linhas,divergente",
    [
        ("KIT C/5 SAM'S LISTRADO", [("3306", 1.0), ("3364", 4.0)], False),
        ("KIT C/5 SAM'S LISTRADO", [("3306", 1.0), ("3364", 12.0)], True),
        ("KIT 6 PRETO", [("3306", 6.0)], False),
        ("PACK C/36 KINGS", [("3306", 3.0)], True),
        ("KIT POLO MATCH", [("3306", 3.0)], False),  # nome sem quantidade: não confere
    ],
)
def test_quantidade_contra_o_nome_so_avisa(nome, linhas, divergente):
    r = cc.montar_composicao([_p("2544", "kit", nome=nome), _p("3306"), _p("3364")], {"2544": linhas})
    assert r.por_seq["2544"] == linhas  # nunca omite por isso
    assert ("2544" in r.divergencia_nome) is divergente


def test_resumo_conta_tudo():
    dtos = [_p("2544", "kit"), _p("2133", "kit"), _p("3306", alt="8366"), _p("3364")]
    comp = cc.montar_composicao(dtos, {"2544": [("3306", 2.0)]})
    codigos, repetidos = cc.codigos_mes(dtos)
    assert cc.resumo(comp, codigos, repetidos) == {
        "leitura_ok": True,
        "motivo_falha": None,
        "com_lista": 1,
        "pares_vazios": 2,
        "kits_sem_composicao": 1,
        "kits_sem_composicao_seqs": ["2133"],
        "omitidos": {},
        "divergencia_nome": {},
        "codigo_mes": 1,
        "codigo_mes_repetido": {},
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_composicao.py`
Expected: FAIL com `TypeError: ... unexpected keyword argument 'codigo_alternativo'`.

- [ ] **Step 3: Write minimal implementation**

Em `app/erp/catalog_extract.py`, no fim do `ProdutoFireDTO`:

```python
    codigo_alternativo: str | None = None  # CODPROD_ALTERN (código do par no Apontaê, se par)
    tem_composicao: bool = False  # IS_KIT: tem linha em PRODUTOS_KIT como pai
```

Criar `app/integrations/flowpcp/catalogo_composicao.py`:

```python
"""Composição de kits e código do Apontaê no catálogo enviado ao Flow.

Regra-mãe (spec 2026-10-15-catalogo-composicao-portal-design.md): o Portal só
afirma o que leu. Chave `componentes` ausente = "não mexer" no Flow; `[]` = o
Fire afirma que não tem composição (o Flow remove a dele); lista = a composição
é exatamente esta. Na dúvida, omite. Kit/pack sem linha em PRODUTOS_KIT vai sem
a chave (decisão C3 do Samuel, 15/10/2026): o PCP pode ter uma composição
provisória no Flow, que o Fire substitui quando for corrigido. Puro: sem Fire,
sem rede.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.erp.catalog_extract import ProdutoFireDTO

Linha = tuple[str | None, float | None]

MAX_FILHOS = 50
MAX_QUANTIDADE = 100_000
MAX_DIGITOS_MES = 9  # cabe no integer do Flow; código de cliente (12 dígitos) não passa
_DIGITOS = re.compile(r"[0-9]+")
_SEQ = re.compile(r"[0-9]{1,10}")
# "KIT C/5", "KIT 6", "PACK C/36", "KIT3": a quantidade que o nome promete.
_QTD_NOME = re.compile(r"\b(?:KITS?|PACKS?)\s*(?:C\s*/\s*)?(\d{1,3})(?!\d)")


def envio_ligado(slug: str) -> bool:
    """`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=mm,nasmar` liga por empresa. Chave de
    migração: só ligar depois de o Flow aceitar os campos (Fatia A); antes disso
    ele recusa o lote inteiro com 400."""
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", "")
    return slug.lower() in {s.strip().lower() for s in bruto.split(",") if s.strip()}


def _var_excluir(slug: str) -> str:
    return "PORTAL_FLOWPCP_CATALOGO_EXCLUIR_" + re.sub(r"[^A-Z0-9]", "_", slug.upper())


def excluir_guardado(slug: str) -> frozenset[str]:
    """SEQs que ficam fora de todo envio do ambiente (manual e diário) até o Fire
    ser corrigido: em 15/10/2026, os pares duplicados do listrado Sam's. Valor
    inválido levanta ValueError: melhor não enviar do que enviar o duplicado."""
    nome = _var_excluir(slug)
    seqs: set[str] = set()
    for parte in os.environ.get(nome, "").split(","):
        parte = parte.strip()
        if not parte:
            continue
        if not _SEQ.fullmatch(parte):
            raise ValueError(f"{nome}: '{parte}' não é um código do Fire (SEQ)")
        seqs.add(str(int(parte)))
    return frozenset(seqs)


def codigo_mes(tipo: str, ativo: bool, codigo_alternativo: str | None) -> str | None:
    """Código do par no Apontaê = CODPROD_ALTERN só dígitos, e só em par ativo: na
    MM, 116 kits têm código alternativo numérico que é código de cliente (0 batem
    com o Apontaê), contra 405 de 410 pares que batem (15/10/2026)."""
    if tipo != "simples" or not ativo or not codigo_alternativo:
        return None
    valor = codigo_alternativo.strip()
    if not _DIGITOS.fullmatch(valor) or len(valor) > MAX_DIGITOS_MES:
        return None
    return valor


def codigos_mes(dtos: list[ProdutoFireDTO]) -> tuple[dict[str, str | None], dict[str, list[str]]]:
    """Código por SEQ; repetido em mais de um par → None em todos (quem ganharia o
    vínculo no Flow dependeria da ordem)."""
    brutos = {d.fire_produto_id: codigo_mes(d.tipo, d.ativo, d.codigo_alternativo) for d in dtos}
    por_codigo: dict[str, list[str]] = defaultdict(list)
    for seq, codigo in brutos.items():
        if codigo:
            por_codigo[codigo].append(seq)
    repetidos = {c: sorted(s, key=int) for c, s in por_codigo.items() if len(s) > 1}
    finais = {seq: (None if c in repetidos else c) for seq, c in brutos.items()}
    return finais, repetidos


@dataclass(frozen=True)
class ComposicaoDoEnvio:
    por_seq: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    omitidos: dict[str, str] = field(default_factory=dict)
    sem_linha: tuple[str, ...] = ()
    divergencia_nome: dict[str, str] = field(default_factory=dict)
    leitura_ok: bool = True
    motivo_falha: str | None = None


def _falha(motivo: str) -> ComposicaoDoEnvio:
    return ComposicaoDoEnvio(leitura_ok=False, motivo_falha=motivo)


def _problema(seq: str, linhas: list[Linha], enviados: set[str]) -> str | None:
    if any(f is None or q is None for f, q in linhas):
        return "linha com filho ou quantidade nulos"
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


def _divergencia(nome: str, linhas: list[tuple[str, float]]) -> str | None:
    """Só aviso: pega o 12 no lugar do 2 na carga da MM (OP 6× maior)."""
    achado = _QTD_NOME.search(nome.upper())
    if not achado:
        return None
    prometido, soma = int(achado.group(1)), sum(q for _, q in linhas)
    if soma == prometido:
        return None
    return f"nome diz {prometido}, composição soma {soma:g}"


def montar_composicao(
    dtos: list[ProdutoFireDTO], lida: dict[str, list[Linha]] | None
) -> ComposicaoDoEnvio:
    """`dtos` = o que vai ao Flow nesta rodada (já sem os excluídos). Erro, leitura
    vazia ou leitura sem nenhum pai que o extract conhece = falha: nenhum item leva
    a chave (base errada ou cópia vazia não pode virar "nenhum kit tem composição")."""
    if lida is None:
        return _falha("leitura de PRODUTOS_KIT falhou")
    if not lida:
        return _falha("leitura de PRODUTOS_KIT voltou vazia")
    pais = {d.fire_produto_id for d in dtos if d.tem_composicao}
    if pais and not pais & lida.keys():
        return _falha("leitura incoerente: nenhum kit do catálogo está nela")

    enviados = {d.fire_produto_id for d in dtos}
    por_seq: dict[str, list[tuple[str, float]]] = {}
    omitidos: dict[str, str] = {}
    sem_linha: list[str] = []
    divergencia: dict[str, str] = {}
    for d in dtos:
        seq = d.fire_produto_id
        linhas = lida.get(seq, [])
        if not linhas:
            if d.tipo == "simples":
                por_seq[seq] = []
            else:
                sem_linha.append(seq)  # C3: sem a chave
            continue
        motivo = _problema(seq, linhas, enviados)
        if motivo:
            omitidos[seq] = motivo
            continue
        validas = [(str(f), float(q)) for f, q in linhas]
        por_seq[seq] = validas
        aviso = _divergencia(d.nome, validas)
        if aviso:
            divergencia[seq] = aviso
    return ComposicaoDoEnvio(
        por_seq=por_seq,
        omitidos=omitidos,
        sem_linha=tuple(sorted(sem_linha, key=int)),
        divergencia_nome=divergencia,
    )


def resumo(
    comp: ComposicaoDoEnvio, codigos: dict[str, str | None], repetidos: dict[str, list[str]]
) -> dict:
    """O que o Portal mandou, para o log, a tela e o diário (`composicao_portal`)."""
    com_lista = sum(1 for v in comp.por_seq.values() if v)
    return {
        "leitura_ok": comp.leitura_ok,
        "motivo_falha": comp.motivo_falha,
        "com_lista": com_lista,
        "pares_vazios": len(comp.por_seq) - com_lista,
        "kits_sem_composicao": len(comp.sem_linha),
        "kits_sem_composicao_seqs": list(comp.sem_linha[:50]),
        "omitidos": dict(sorted(comp.omitidos.items(), key=lambda kv: int(kv[0]))),
        "divergencia_nome": dict(sorted(comp.divergencia_nome.items(), key=lambda kv: int(kv[0]))),
        "codigo_mes": sum(1 for c in codigos.values() if c),
        "codigo_mes_repetido": repetidos,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_composicao.py tests/test_catalog_extract.py tests/test_catalogo_fire_repo.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/catalog_extract.py app/integrations/flowpcp/catalogo_composicao.py tests/test_flowpcp_catalogo_composicao.py
git commit -m "feat(catalogo): regras da composição e do codigoMes (puro, falha fechada, C3)"
```

---

### Task 3: Extract — código alternativo, IS_KIT e leitura de PRODUTOS_KIT

**Files:**
- Modify: `app/erp/queries.py` (`LIST_PRODUTOS_CATALOGO`, `LIST_PRODUTOS_CATALOGO_MEIAS`; nova `LIST_PRODUTOS_KIT_COMPOSICAO`)
- Modify: `app/erp/catalog_extract.py`
- Test: `tests/test_catalog_extract.py`

**Interfaces:**
- Consumes: `ProdutoFireDTO.codigo_alternativo`, `ProdutoFireDTO.tem_composicao` (Task 2).
- Produces: `extract_produtos` preenche os dois campos; `extract_composicao(fire_conn) -> dict[str, list[tuple[str | None, float | None]]]` (levanta a exceção do driver em erro: quem decide o que fazer é o sync). Linha com pai nulo é descartada (não dá para atribuir); filho ou quantidade nulos ficam como `None` para a regra omitir o kit.

- [ ] **Step 1: Write the failing test**

Em `tests/test_catalog_extract.py`, as linhas falsas do catálogo passam a ter 7 colunas (a 7ª é `CODPROD_ALTERN`). Atualizar **todas** as tuplas `rows = [...]` existentes do arquivo acrescentando `, None` no fim de cada tupla, e acrescentar:

```python
from app.erp.catalog_extract import extract_composicao
from app.erp.queries import (
    LIST_PRODUTOS_CATALOGO,
    LIST_PRODUTOS_CATALOGO_MEIAS,
    LIST_PRODUTOS_KIT_COMPOSICAO,
)


def test_extract_traz_codigo_alternativo_limpo_e_is_kit():
    rows = [
        (3759, "SAM'S 34/38 BCO C/ LISTRAS PTO C.A.", "PARES", None, "Nao", 0, " 113649 "),
        (3174, "BRANCO COM PRETO", "PAR", None, "Nao", 0, "  "),
        (2544, "KIT 6", "KIT", None, "Nao", 1, "986388"),
    ]
    out = extract_produtos(_FakeConn(rows))
    assert out[0].codigo_alternativo == "113649" and out[0].tem_composicao is False
    assert out[1].codigo_alternativo is None
    assert out[2].tem_composicao is True


def test_extract_composicao_agrupa_por_pai_na_ordem_lida():
    conn = _FakeConn([(2544, 3306, 2.0), (2544, 3364, 2.0), (2406, 3712, 3)])
    out = extract_composicao(conn)
    assert conn._cur.executed == LIST_PRODUTOS_KIT_COMPOSICAO
    assert out == {"2544": [("3306", 2.0), ("3364", 2.0)], "2406": [("3712", 3.0)]}


def test_extract_composicao_guarda_nulos_e_descarta_pai_nulo():
    out = extract_composicao(_FakeConn([(2544, None, 2.0), (2544, 3364, None), (None, 3306, 1.0)]))
    assert out == {"2544": [(None, 2.0), ("3364", None)]}


def test_consultas_do_catalogo_trazem_codprod_altern():
    for sql in (LIST_PRODUTOS_CATALOGO, LIST_PRODUTOS_CATALOGO_MEIAS):
        assert "P.CODPROD_ALTERN" in sql
```

(Se `_FakeConn`/`_FakeCursor` do arquivo guardarem o SQL em outro atributo, usar o nome que o arquivo já usa; o teste existente de `LIST_PRODUTOS_CATALOGO_MEIAS` mostra qual é.)

- [ ] **Step 2: Run test to verify it fails**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_catalog_extract.py`
Expected: FAIL (`ImportError: extract_composicao` / `ValueError: too many values to unpack`).

- [ ] **Step 3: Write minimal implementation**

`app/erp/queries.py` — nas duas consultas, trocar o fim da expressão `IS_KIT` do SELECT final

```sql
           ) THEN 1 ELSE 0 END AS IS_KIT
```

por

```sql
           ) THEN 1 ELSE 0 END AS IS_KIT,
           P.CODPROD_ALTERN
```

(`CODPROD_ALTERN` vira a 7ª coluna; o resto da consulta fica igual). E acrescentar logo depois de `LIST_PRODUTOS_CATALOGO_MEIAS`:

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

`app/erp/catalog_extract.py` — import:

```python
from app.erp.queries import (
    LIST_PRODUTOS_CATALOGO,
    LIST_PRODUTOS_CATALOGO_MEIAS,
    LIST_PRODUTOS_KIT_COMPOSICAO,
)
```

no laço de `extract_produtos`, trocar o desempacotamento e acrescentar os campos:

```python
    for seq, desc, uni, ean, bloqueado, is_kit, alternativo in rows:
        ...
                tipo=_tipo(is_kit, _clean(uni), nome),
                codigo_alternativo=_clean(alternativo),
                tem_composicao=bool(is_kit),
```

e no fim do arquivo:

```python
def extract_composicao(fire_conn) -> dict[str, list[tuple[str | None, float | None]]]:
    """PRODUTOS_KIT inteiro, agrupado por pai: {SEQ do pai: [(SEQ do filho, QTD)]},
    na ordem de cadastro. Read-only. Erro do driver sobe: o sync trata como
    "composição ausente em todos" (nunca como "sem composição"). Filho ou QTD
    nulos ficam None para a regra omitir o kit; pai nulo não tem a quem pertencer."""
    cur = fire_conn.cursor()
    try:
        cur.execute(LIST_PRODUTOS_KIT_COMPOSICAO)
        rows = cur.fetchall()
    finally:
        cur.close()
    out: dict[str, list[tuple[str | None, float | None]]] = {}
    for pai, filho, qtd in rows:
        if pai is None:
            continue
        out.setdefault(str(pai), []).append(
            (None if filho is None else str(filho), None if qtd is None else float(qtd))
        )
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_catalog_extract.py tests/test_catalogo_fire_repo.py tests/test_flowpcp_catalogo_composicao.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/erp/queries.py app/erp/catalog_extract.py tests/test_catalog_extract.py
git commit -m "feat(catalogo): extract lê CODPROD_ALTERN, IS_KIT e a composição (PRODUTOS_KIT)"
```

---

### Task 4: Mapper e sync

**Files:**
- Modify: `app/integrations/flowpcp/catalogo_mapper.py`
- Modify: `app/integrations/flowpcp/catalogo_sync.py`
- Test: `tests/test_flowpcp_catalogo_mapper.py`, `tests/test_catalogo_sync.py`

**Interfaces:**
- Consumes: Task 1 (schema), Task 2 (`envio_ligado`, `excluir_guardado`, `codigos_mes`, `montar_composicao`, `resumo`, `ComposicaoDoEnvio`), Task 3 (`extract_composicao`).
- Produces:
  - `build_catalogo_request(dtos, *, dry_run, full_sync, importador_versao, extraido_em, composicao: ComposicaoDoEnvio | None = None, codigos_mes: dict[str, str | None] | None = None, confirmar_remocao: bool = False, confirmar_substituicao: bool = False) -> CatalogoRequest`
  - `run_catalogo_sync(slug, *, dry_run=True, full_sync=True, now_iso=None, fora_do_envio=frozenset(), confirmar_remocao=False, confirmar_substituicao=False, _client=None, _fire_conn=None, _env_conn=None)`: mesmo retorno de hoje. Com a composição ligada, o relatório devolvido ganha o extra `composicao_portal` (dict de `resumo`). Com exclusão guardada, ganha `fora_do_envio_guardado` (lista ordenada). Confirmação com `dry_run=True` levanta `ValueError` antes de qualquer leitura.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_flowpcp_catalogo_mapper.py`:

```python
from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request


def _dtos():
    return [
        ProdutoFireDTO("2544", "2544", "KIT 6", "KIT", None, True, "kit", codigo_alternativo="986388"),
        ProdutoFireDTO("3306", "3306", "PAR PTO", "PARES", None, True, "simples", codigo_alternativo="8366"),
        ProdutoFireDTO("3381", "3381", "KIT C/5", "KIT", None, True, "kit"),
    ]


def _req(composicao, codigos=None, **kw):
    return build_catalogo_request(
        _dtos(), dry_run=False, full_sync=True, importador_versao="1", extraido_em="t",
        composicao=composicao, codigos_mes=codigos, **kw,
    ).model_dump(by_alias=True)


def test_mapper_sem_composicao_nao_manda_campos_novos():
    corpo = _req(None)
    assert "confirmarRemocaoComposicao" not in corpo
    for item in corpo["itens"]:
        assert "componentes" not in item and "codigoMes" not in item


def test_mapper_com_composicao():
    comp = ComposicaoDoEnvio(por_seq={"2544": [("3306", 6.0)], "3306": []}, sem_linha=("3381",))
    kit, par, c5 = _req(comp, {"2544": None, "3306": "8366", "3381": None})["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert kit["codigoMes"] is None
    assert par["componentes"] == [] and par["codigoMes"] == "8366"
    assert "componentes" not in c5 and c5["codigoMes"] is None


def test_mapper_leitura_falhou_so_codigo_mes():
    itens = _req(ComposicaoDoEnvio(leitura_ok=False), {"2544": None, "3306": "8366", "3381": None})["itens"]
    assert all("componentes" not in i for i in itens)
    assert itens[1]["codigoMes"] == "8366"


def test_mapper_confirmacoes_so_quando_pedidas():
    corpo = _req(None, confirmar_remocao=True)
    assert corpo["confirmarRemocaoComposicao"] is True
    assert "confirmarSubstituicaoComposicao" not in corpo
```

Em `tests/test_catalogo_sync.py`:

```python
import json

import pytest

from app.integrations.flowpcp.client import FlowPCPClientError


class _FireComKit:
    """Conexão fake: PRODUTOS_KIT devolve linhas ou falha."""

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


_DTOS_KIT = [
    ProdutoFireDTO("2544", "2544", "KIT", "KIT", None, True, "kit", tem_composicao=True),
    ProdutoFireDTO("3306", "3306", "PAR", "PARES", None, True, "simples", codigo_alternativo="8366"),
    ProdutoFireDTO("3174", "3174", "PAR DUP", "PARES", None, True, "simples"),
]


@pytest.fixture
def _sync(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", raising=False)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: list(_DTOS_KIT))

    def rodar(*, ligado_para="mm", fire=None, cliente=None, **kw):
        monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", ligado_para)
        cliente = cliente or _FakeClient()
        rep = catalogo_sync.run_catalogo_sync(
            "mm", now_iso="t", _client=cliente, _env_conn=_FakeEnvConn(),
            _fire_conn=fire or _FireComKit([(2544, 3306, 6)]), **kw,
        )
        return rep, cliente.sent.model_dump(by_alias=True)

    return rodar


@pytest.mark.parametrize("ligado_para", ["", "nasmar"])
def test_sync_desligado_payload_identico_ao_de_hoje(_sync, ligado_para):
    rep, corpo = _sync(ligado_para=ligado_para)
    esperado = {
        "fireProdutoId": "2544", "codigo": "2544", "nome": "KIT", "unidade": "KIT",
        "ean": None, "ativo": True, "tipo": "kit",
    }
    assert json.dumps(corpo["itens"][0], sort_keys=True) == json.dumps(esperado, sort_keys=True)
    assert set(corpo) == {"schema", "dryRun", "fullSync", "itens", "origem"}
    assert "composicao_portal" not in (rep.model_extra or {})


def test_sync_ligado_manda_composicao_e_resumo(_sync):
    rep, corpo = _sync()
    kit, par, _ = corpo["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert par["componentes"] == [] and par["codigoMes"] == "8366"
    assert rep.model_extra["composicao_portal"]["com_lista"] == 1


@pytest.mark.parametrize("fire", [_FireComKit(falha=True), _FireComKit([])])
def test_sync_leitura_com_falha_nenhum_item_com_a_chave(_sync, fire):
    rep, corpo = _sync(fire=fire)
    assert all("componentes" not in i for i in corpo["itens"])
    assert corpo["itens"][1]["codigoMes"] == "8366"
    assert rep.model_extra["composicao_portal"]["leitura_ok"] is False


def test_sync_exclusao_guardada_vale_mesmo_desligado(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "3174")
    rep, corpo = _sync(ligado_para="")
    assert [i["fireProdutoId"] for i in corpo["itens"]] == ["2544", "3306"]
    assert rep.model_extra["fora_do_envio_guardado"] == ["3174"]


def test_sync_exclusao_guardada_tira_a_chave_do_kit_que_usa_o_par(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "3306")
    rep, corpo = _sync()
    assert "componentes" not in corpo["itens"][0]
    assert rep.model_extra["composicao_portal"]["omitidos"] == {"2544": "filho fora do envio: 3306"}


def test_sync_exclusao_guardada_invalida_nao_envia_nada(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "abc")
    cliente = _FakeClient()
    with pytest.raises(ValueError):
        _sync(cliente=cliente)
    assert cliente.sent is None


def test_sync_confirmacao_so_com_apply(_sync):
    with pytest.raises(ValueError, match="dryRun"):
        _sync(dry_run=True, confirmar_remocao=True)
    _, corpo = _sync(dry_run=False, confirmar_substituicao=True)
    assert corpo["confirmarSubstituicaoComposicao"] is True


def test_sync_400_com_composicao_ligada_diz_para_desligar(_sync):
    class _Recusa(_FakeClient):
        def send_catalogo(self, request):
            self.sent = request
            raise FlowPCPClientError("catalogo status 400", status_code=400, body="zod")

    with pytest.raises(FlowPCPClientError, match="PORTAL_FLOWPCP_CATALOGO_COMPOSICAO"):
        _sync(cliente=_Recusa())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py`
Expected: FAIL (`unexpected keyword argument 'composicao'`).

- [ ] **Step 3: Write minimal implementation**

`app/integrations/flowpcp/catalogo_mapper.py` (arquivo inteiro):

```python
from __future__ import annotations

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp.catalogo_composicao import ComposicaoDoEnvio
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoComponente,
    CatalogoOrigem,
    CatalogoProdutoItem,
    CatalogoRequest,
)


def _item(
    d: ProdutoFireDTO,
    composicao: ComposicaoDoEnvio | None,
    codigos_mes: dict[str, str | None] | None,
) -> CatalogoProdutoItem:
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
        campos["codigoMes"] = (codigos_mes or {}).get(d.fire_produto_id)
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
    codigos_mes: dict[str, str | None] | None = None,
    confirmar_remocao: bool = False,
    confirmar_substituicao: bool = False,
) -> CatalogoRequest:
    """`composicao=None` (interruptor desligado): itens iguais aos de antes.
    Confirmações só entram no corpo quando pedidas."""
    extras = {}
    if confirmar_remocao:
        extras["confirmarRemocaoComposicao"] = True
    if confirmar_substituicao:
        extras["confirmarSubstituicaoComposicao"] = True
    return CatalogoRequest(
        dryRun=dry_run,
        fullSync=full_sync,
        itens=[_item(d, composicao, codigos_mes) for d in dtos],
        origem=CatalogoOrigem(importadorVersao=importador_versao, extraidoEm=extraido_em),
        **extras,
    )
```

`app/integrations/flowpcp/catalogo_sync.py`:

imports:

```python
from app.erp.catalog_extract import extract_composicao, extract_produtos
from app.integrations.flowpcp import catalogo_composicao
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
```

assinatura: acrescentar `confirmar_remocao: bool = False, confirmar_substituicao: bool = False,` depois de `fora_do_envio`, e no docstring um parágrafo:

```
    Composição (`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO` com o slug): lê PRODUTOS_KIT
    e manda `componentes`/`codigoMes` pelas regras de `catalogo_composicao`; o
    relatório ganha `composicao_portal`. `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>`
    soma-se a `fora_do_envio` em toda rodada, manual ou diária. As confirmações
    liberam as travas do Flow e só valem com `dry_run=False`.
```

logo no começo do corpo, antes de `cfg = ...`:

```python
    if dry_run and (confirmar_remocao or confirmar_substituicao):
        raise ValueError("confirmar trava de composição só vale com dryRun=false (apply)")
```

dentro do `with fire_ctx as fire_conn:`, depois do `extract_produtos`:

```python
        ligado = getattr(cfg, "catalogo_push", False) and catalogo_composicao.envio_ligado(slug)
        lida = None
        if ligado:
            try:
                lida = extract_composicao(fire_conn)
            except Exception as exc:  # noqa: BLE001 — falha vira "ausente em todos", nunca "vazio"
                logger.error(f"catalogo sync env={slug}: leitura de PRODUTOS_KIT falhou: {exc!r}")
```

trocar o bloco `if fora_do_envio:` … `enviados = [...]` por:

```python
    guardado = catalogo_composicao.excluir_guardado(slug)
    fora = fora_do_envio | guardado
    if fora:
        nao_achados = sorted(fora - {d.fire_produto_id for d in dtos}, key=int)
        if nao_achados:
            raise ForaDoEnvioInexistenteError(nao_achados)
        logger.info(
            f"catalogo sync env={slug}: {len(fora)} itens fora do envio "
            f"(solicitados: {sorted(fora_do_envio, key=int)}, guardados: {sorted(guardado, key=int)})"
        )
    enviados = [d for d in dtos if d.fire_produto_id not in fora]

    composicao = codigos = resumo = None
    if ligado:
        composicao = catalogo_composicao.montar_composicao(enviados, lida)
        codigos, repetidos = catalogo_composicao.codigos_mes(enviados)
        resumo = catalogo_composicao.resumo(composicao, codigos, repetidos)
        _logar_composicao(slug, resumo)
```

no `build_catalogo_request(...)`, acrescentar:

```python
            composicao=composicao,
            codigos_mes=codigos,
            confirmar_remocao=confirmar_remocao,
            confirmar_substituicao=confirmar_substituicao,
```

e trocar `return client.send_catalogo(request)` por:

```python
        try:
            rep = client.send_catalogo(request)
        except FlowPCPClientError as exc:
            if ligado and exc.status_code == 400:
                raise FlowPCPClientError(
                    f"{exc} — o Flow recusou o catálogo com composição. Se a Fatia A não "
                    f"estiver no ar, tire '{slug}' de PORTAL_FLOWPCP_CATALOGO_COMPOSICAO",
                    status_code=exc.status_code,
                    body=exc.body,
                ) from exc
            raise
        if resumo is not None:
            rep.composicao_portal = resumo
        if guardado:
            rep.fora_do_envio_guardado = sorted(guardado, key=int)
        return rep
```

e no fim do arquivo:

```python
def _logar_composicao(slug: str, resumo: dict) -> None:
    logger.info(
        f"catalogo sync env={slug} composicao: leitura_ok={resumo['leitura_ok']} "
        f"com_lista={resumo['com_lista']} pares_vazios={resumo['pares_vazios']} "
        f"kits_sem_composicao={resumo['kits_sem_composicao']} omitidos={len(resumo['omitidos'])} "
        f"codigo_mes={resumo['codigo_mes']}"
    )
    if not resumo["leitura_ok"]:
        logger.warning(f"catalogo sync env={slug} composicao não enviada: {resumo['motivo_falha']}")
    for seq, motivo in resumo["omitidos"].items():
        logger.warning(f"catalogo sync env={slug} composicao omitida SEQ {seq}: {motivo}")
    for seq, aviso in resumo["divergencia_nome"].items():
        logger.warning(f"catalogo sync env={slug} quantidade diverge do nome SEQ {seq}: {aviso}")
    for codigo, seqs in resumo["codigo_mes_repetido"].items():
        logger.warning(f"catalogo sync env={slug} codigoMes {codigo} repetido em {seqs}: vai null")
```

(`CatalogoReconciliacaoResponse` tem `extra="allow"`: atribuir um nome novo grava em `model_extra` e sai no `model_dump()` da rota.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py tests/test_flowpcp_catalogo_diario.py tests/test_admin_environments_routes.py`
Expected: PASS. Os testes antigos de `tests/test_catalogo_sync.py` não definem `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO`; se o ambiente de quem roda tiver a variável, a fixture `_sync` não cobre os antigos: acrescentar no topo do arquivo uma fixture `autouse` que faz `monkeypatch.delenv` das duas variáveis.

- [ ] **Step 5: Commit**

```bash
git add app/integrations/flowpcp/catalogo_mapper.py app/integrations/flowpcp/catalogo_sync.py tests/test_flowpcp_catalogo_mapper.py tests/test_catalogo_sync.py
git commit -m "feat(catalogo): manda composição e codigoMes por empresa, exclusão guardada e confirmações"
```

---

### Task 5: Cliente — apply sem repetição, 300 s, 409 e idempotência pelo conteúdo

**Files:**
- Modify: `app/http/policies.py`, `app/http/client.py`
- Modify: `app/integrations/flowpcp/client.py`
- Test: `tests/test_outbound_client.py`, `tests/test_flowpcp_catalogo_client.py`

**Interfaces:**
- Produces: `apply_post_policy() -> RetryPolicy`; `OutboundClient.post_json(..., retry_policy: RetryPolicy | None = None)`; `APPLY_TIMEOUT_SECONDS = 300.0`; `class FlowPCPApplyEmAndamentoError(FlowPCPClientError)`; `_idem_catalogo(request: CatalogoRequest) -> str`.

- [ ] **Step 1: Write the failing tests**

Em `tests/test_outbound_client.py`:

```python
from app.http.policies import apply_post_policy


def test_apply_nao_repete_em_timeout_de_leitura():
    tentativas = []

    def handler(request):
        tentativas.append(1)
        raise httpx.ReadTimeout("lento", request=request)

    with OutboundClient(base_url="http://test", transport=_transport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post_json("/x", json={}, retry_policy=apply_post_policy())
    assert len(tentativas) == 1


def test_apply_nao_repete_em_5xx():
    tentativas = []

    def handler(request):
        tentativas.append(1)
        return httpx.Response(503)

    with OutboundClient(base_url="http://test", transport=_transport(handler)) as client:
        r = client.post_json("/x", json={}, retry_policy=apply_post_policy())
    assert r.status_code == 503 and len(tentativas) == 1


def test_apply_so_repete_quando_a_conexao_nem_abriu():
    p = apply_post_policy()
    assert httpx.ConnectError in p.retry_on_exceptions
    assert httpx.ReadTimeout not in p.retry_on_exceptions
    assert httpx.WriteTimeout not in p.retry_on_exceptions
    assert httpx.RemoteProtocolError not in p.retry_on_exceptions
```

Em `tests/test_flowpcp_catalogo_client.py`: trocar a assinatura do `_FakeOutbound.post_json` para guardar os extras

```python
    def post_json(self, path, *, json, idempotency_key, **kw):
        self.last_path = path
        self.last_json = json
        self.last_key = idempotency_key
        self.last_kw = kw
        return self._resp
```

e acrescentar:

```python
from app.integrations.flowpcp.client import (
    APPLY_TIMEOUT_SECONDS,
    FlowPCPApplyEmAndamentoError,
    _idem_catalogo,
)


def _req_apply(nome="X"):
    r = _req()
    return r.model_copy(update={"dryRun": False, "itens": [r.itens[0].model_copy(update={"nome": nome})]})


def _cliente(resp):
    out = _FakeOutbound(resp)
    return out, FlowPCPClient(base_url="http://x", service_token="t", tenant_id="tn", outbound=out)


def test_apply_vai_com_politica_e_timeout_proprios():
    out, client = _cliente(_Resp(200, {"contagens": {}}))
    client.send_catalogo(_req_apply())
    assert out.last_kw["timeout"] == APPLY_TIMEOUT_SECONDS
    assert out.last_kw["retry_policy"].max_attempts >= 1
    assert out.last_kw["retry_policy"].retry_on_status == frozenset()


def test_dry_run_segue_a_politica_de_hoje():
    out, client = _cliente(_Resp(200, {"contagens": {}}))
    client.send_catalogo(_req())
    assert out.last_kw == {}


def test_409_vira_apply_em_andamento():
    _, client = _cliente(_Resp(409, text="execução em andamento"))
    with pytest.raises(FlowPCPApplyEmAndamentoError):
        client.send_catalogo(_req_apply())


def test_idempotencia_muda_quando_o_conteudo_muda_com_mesma_contagem():
    a, b = _req_apply("X"), _req_apply("X NOVO")
    assert _idem_catalogo(a) != _idem_catalogo(b)
    assert _idem_catalogo(a) == _idem_catalogo(_req_apply("X"))  # estável em retry
```

(`model_copy(update=...)` não marca o campo como preenchido para o serializer, mas `dryRun` e `nome` não são omitíveis; os testes não dependem disso.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_outbound_client.py tests/test_flowpcp_catalogo_client.py`
Expected: FAIL (`cannot import name 'apply_post_policy'`).

- [ ] **Step 3: Write minimal implementation**

`app/http/policies.py` — depois de `idempotent_post_policy`, e no `__all__`:

```python
def apply_post_policy() -> RetryPolicy:
    """POST que grava e NÃO é idempotente no servidor (apply do catálogo do Flow).
    Repete só quando a conexão nem abriu: timeout de leitura, 5xx ou conexão caída
    no meio podem ter chegado lá, e repetir seria um segundo apply."""
    return RetryPolicy(
        max_attempts=3,
        retry_on_status=frozenset(),
        wait_initial_seconds=1.0,
        wait_max_seconds=4.0,
        retry_on_exceptions=(httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout),
    )
```

`app/http/client.py`:
- `post_json` ganha `retry_policy: RetryPolicy | None = None` e passa `retry_policy=retry_policy` ao `_send_with_retry`;
- `_send_with_retry` ganha `retry_policy: RetryPolicy | None = None` e troca as duas leituras de `self._retry_policy` por uma variável local `policy = retry_policy or self._retry_policy` (`policy.build()` e `policy.should_retry_status(...)`);
- importar `RetryPolicy` de `app.http.policies` se ainda não estiver importado.

`app/integrations/flowpcp/client.py`:

```python
import json
...
from app.http.policies import apply_post_policy, idempotent_post_policy
...
# Apply do catálogo: o Flow tem maxDuration 300 s e um apply por vez (409).
APPLY_TIMEOUT_SECONDS = 300.0


class FlowPCPApplyEmAndamentoError(FlowPCPClientError):
    """409 do /catalogo: o Flow já está gravando um catálogo deste tenant."""


def _idem_catalogo(request: CatalogoRequest) -> str:
    """Chave estável em retry e nova quando o conteúdo muda (só a contagem colidia:
    os mesmos 1.026 itens com composição nova). `origem` fica fora: muda a cada
    extração mesmo sem nada novo."""
    corpo = {k: v for k, v in request.model_dump(by_alias=True).items() if k != "origem"}
    digest = hashlib.sha256(json.dumps(corpo, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"catalogo-{int(request.dryRun)}-{len(request.itens)}-{digest}"
```

`send_catalogo`:

```python
    def send_catalogo(self, request: CatalogoRequest) -> CatalogoReconciliacaoResponse:
        body = request.model_dump(by_alias=True)
        idem = _idem_catalogo(request)
        # Apply grava e o Flow não deduplica pela chave: não repetir depois que a
        # conexão abriu, e esperar até o maxDuration do Flow. Dry-run segue como era.
        extras = (
            {}
            if request.dryRun
            else {"timeout": APPLY_TIMEOUT_SECONDS, "retry_policy": apply_post_policy()}
        )
        try:
            resp = self._client.post_json(_CATALOGO_PATH, json=body, idempotency_key=idem, **extras)
        except HttpError as exc:
            raise FlowPCPClientError(
                f"send_catalogo falhou: {exc}", status_code=exc.status_code, body=exc.body
            ) from exc
        if resp.status_code == 409:
            raise FlowPCPApplyEmAndamentoError(
                "o Flow já está gravando um catálogo deste ambiente",
                status_code=409,
                body=(resp.text or "")[:500],
            )
        if not resp.is_success:
            ...  # igual a hoje
```

Atenção: `httpx.ReadTimeout` não é `HttpError` do Portal; hoje ele sobe cru até a rota, que já devolve 502 com a mensagem. Manter assim: o teste da rota na Task 6 cobre a mensagem.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_outbound_client.py tests/test_flowpcp_catalogo_client.py tests/test_flowpcp_client.py tests/test_gestor_integration.py tests/test_catalogo_sync.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/http/policies.py app/http/client.py app/integrations/flowpcp/client.py tests/test_outbound_client.py tests/test_flowpcp_catalogo_client.py
git commit -m "feat(catalogo): apply sem repetição depois de conectar, 300 s, 409 e idempotência pelo conteúdo"
```

---

### Task 6: Rota manual, diário e tela de admin

**Files:**
- Modify: `app/web/routes_environments.py` (`sync_catalogo_flowpcp`)
- Modify: `app/integrations/flowpcp/catalogo_diario.py` (`_resumo`, log em `rodar`)
- Modify: `app/web/static/admin-ambiente-edit.html` (`rodarSyncCatalogo` e o bloco `#sync-catalogo-result`)
- Test: `tests/test_admin_environments_routes.py`, `tests/test_flowpcp_catalogo_diario.py`

**Interfaces:**
- Consumes: `run_catalogo_sync(..., confirmar_remocao=, confirmar_substituicao=)` (Task 4); `FlowPCPApplyEmAndamentoError` (Task 5).
- Produces: `POST /api/admin/environments/{env_id}/flowpcp/sync-catalogo?apply=true&confirmar_remocao=1&confirmar_substituicao=1`; resposta igual à de hoje mais os blocos do Flow (`composicao`, `vinculoMes`) e do Portal (`composicao_portal`, `fora_do_envio_guardado`).

- [ ] **Step 1: Write the failing tests**

Em `tests/test_admin_environments_routes.py` (reaproveita `setup`, `_client`, `_enable_flowpcp`, `_FakeReport` do arquivo):

```python
def _env_mm(setup):
    env = environments_repo.create(
        slug="mm", name="MM", watch_dir=str(setup), output_dir=str(setup),
        fb_path=str(setup / "x.fdb"),
    )
    _enable_flowpcp(env["id"])
    return env


def test_sync_catalogo_confirmacoes_chegam_ao_sync(setup, monkeypatch):
    env = _env_mm(setup)
    import app.integrations.flowpcp.catalogo_sync as cs

    capturado = {}
    monkeypatch.setattr(cs, "run_catalogo_sync", lambda slug, **kw: capturado.update(kw) or _FakeReport())
    url = f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo"
    r = _client().post(url + "?apply=true&confirmar_remocao=1&confirmar_substituicao=1")
    assert r.status_code == 200
    assert capturado["confirmar_remocao"] is True and capturado["confirmar_substituicao"] is True


def test_sync_catalogo_sem_confirmacao_manda_false(setup, monkeypatch):
    env = _env_mm(setup)
    import app.integrations.flowpcp.catalogo_sync as cs

    capturado = {}
    monkeypatch.setattr(cs, "run_catalogo_sync", lambda slug, **kw: capturado.update(kw) or _FakeReport())
    _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo?apply=true")
    assert capturado["confirmar_remocao"] is False and capturado["confirmar_substituicao"] is False


def test_sync_catalogo_confirmacao_sem_apply_e_422(setup, monkeypatch):
    env = _env_mm(setup)
    import app.integrations.flowpcp.catalogo_sync as cs

    chamado = []
    monkeypatch.setattr(cs, "run_catalogo_sync", lambda *a, **k: chamado.append(1))
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo?confirmar_remocao=1")
    assert r.status_code == 422 and chamado == []


def test_sync_catalogo_409_do_flow_vira_mensagem_clara(setup, monkeypatch):
    env = _env_mm(setup)
    import app.integrations.flowpcp.catalogo_sync as cs
    from app.integrations.flowpcp.client import FlowPCPApplyEmAndamentoError

    def ocupado(*a, **k):
        raise FlowPCPApplyEmAndamentoError("ocupado", status_code=409)

    monkeypatch.setattr(cs, "run_catalogo_sync", ocupado)
    r = _client().post(f"/api/admin/environments/{env['id']}/flowpcp/sync-catalogo?apply=true")
    assert r.status_code == 409
    assert "já está gravando" in r.json()["detail"]
```

Em `tests/test_flowpcp_catalogo_diario.py`:

```python
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse


def _rep(**extra):
    return CatalogoReconciliacaoResponse.model_validate({"contagens": {"matchLimpo": 1}, **extra})


def test_resumo_traz_os_blocos_de_composicao():
    texto = catalogo_diario._resumo(
        _rep(composicao={"aCriar": 3}, vinculoMes={"novos": 2}, composicao_portal={"com_lista": 271})
    )
    assert "'aCriar': 3" in texto and "'novos': 2" in texto and "'com_lista': 271" in texto


@pytest.mark.parametrize(
    "extra",
    [
        {"composicao": {"travaRemocaoAtiva": True}},
        {"composicao": {"travaSubstituicaoAtiva": True, "travaSubstituicao": ["2544"]}},
        {"composicao": {"erpSemChave": 4}},
        {"composicao_portal": {"leitura_ok": False}},
    ],
)
def test_atencao_quando_trava_falha_ou_kit_sem_chave(extra):
    assert catalogo_diario._precisa_atencao(_rep(**extra)) is True


def test_sem_atencao_no_normal():
    rep = _rep(composicao={"travaRemocaoAtiva": False, "erpSemChave": 0}, composicao_portal={"leitura_ok": True})
    assert catalogo_diario._precisa_atencao(rep) is False


def test_diario_nunca_manda_confirmacao(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", "gravar")
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None)
    (_, kw), = sync.chamadas
    assert "confirmar_remocao" not in kw and "confirmar_substituicao" not in kw
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_admin_environments_routes.py tests/test_flowpcp_catalogo_diario.py`
Expected: FAIL (`KeyError: 'confirmar_remocao'`, `AttributeError: _precisa_atencao`).

- [ ] **Step 3: Write minimal implementation**

`app/web/routes_environments.py` — `sync_catalogo_flowpcp`:
- parâmetros novos `confirmar_remocao: bool = False, confirmar_substituicao: bool = False` (o FastAPI aceita `1`/`true`);
- docstring ganha a linha: `- confirmar_remocao=1 / confirmar_substituicao=1: liberam as travas de composição do Flow; só com apply=true (o diário nunca manda).`;
- antes de chamar o sync:

```python
    if (confirmar_remocao or confirmar_substituicao) and not apply:
        raise HTTPException(422, "Liberar trava só vale junto com apply=true (gravar)")
```

- import `FlowPCPApplyEmAndamentoError` de `app.integrations.flowpcp.client`, passar `confirmar_remocao=confirmar_remocao, confirmar_substituicao=confirmar_substituicao` ao `run_catalogo_sync`, e acrescentar antes do `except Exception`:

```python
    except FlowPCPApplyEmAndamentoError as exc:
        raise HTTPException(
            409, "O Flow já está gravando um catálogo deste ambiente. Espere terminar e simule de novo."
        ) from exc
```

`app/integrations/flowpcp/catalogo_diario.py`:

```python
_BLOCOS = ("composicao", "vinculoMes", "composicao_portal", "fora_do_envio_guardado")


def _precisa_atencao(rep: object) -> bool:
    """Trava do Flow ativa, leitura da composição com falha no Portal, ou kit com
    composição do ERP que deixou de vir (C3: apagar no Fire não chega ao Flow)."""
    extra = getattr(rep, "model_extra", None) or {}
    comp = extra.get("composicao") or {}
    portal = extra.get("composicao_portal") or {}
    trava = any(k.startswith("trava") and v for k, v in comp.items())
    return bool(trava or comp.get("erpSemChave") or portal.get("leitura_ok") is False)
```

em `_resumo`, trocar o filtro de extras para também levar os blocos:

```python
    extras = {
        k: v
        for k, v in (getattr(rep, "model_extra", None) or {}).items()
        if isinstance(v, int | str) or k in _BLOCOS
    }
```

e em `rodar`, trocar o `logger.info(...)` final por:

```python
        log = logger.warning if _precisa_atencao(rep) else logger.info
        log(f"flowpcp.catalogo env={slug}: sync diário ({modo}) enviado: {_resumo(rep)}")
```

`app/web/static/admin-ambiente-edit.html` — em `rodarSyncCatalogo(apply, liberar)`:
- assinatura `async function rodarSyncCatalogo(apply, liberar = [])`; a URL acrescenta `&confirmar_remocao=1` / `&confirmar_substituicao=1` conforme `liberar` (`'remocao'`, `'substituicao'`);
- texto da confirmação do apply: `'GRAVAR NO FLOW: grava os produtos do Fire no catálogo do Flow. Se o envio de composição estiver ligado para esta empresa, grava também de que cada kit é feito e o código de cada par no Apontaê. Confirmar?'`; com `liberar`, a confirmação diz quantos kits cada trava segura e que eles vão ser trocados ou ficar sem composição;
- depois das linhas de contagem que já existem, montar com `document.createElement`/`textContent` (nada do relatório entra por `innerHTML`) três grupos, cada um só quando o bloco veio:
  - **Composição enviada pelo Portal** (`rep.composicao_portal`): leitura (ok, ou o `motivo_falha` em destaque de erro), kits com composição, kits sem composição no Fire (com os SEQs), omitidos (SEQ: motivo), quantidade que não bate com o nome (SEQ: aviso), códigos do Apontaê enviados e repetidos;
  - **Composição no Flow** (`rep.composicao`): iguais, a adotar (`aAdotar`: composição do Flow igual à do Fire que passa a ser do Fire), a criar, a substituir, a remover, adiadas (`adiadas`: remoção na 1ª leitura), bloqueados (inclusive `bloqueadosFilhoDuplicado`), kits do ERP que deixaram de vir (`erpSemChave` com `erpSemChaveIds`), e cada trava ativa em destaque de aviso;
  - **Vínculo com o Apontaê** (`rep.vinculoMes`): novos, iguais, conflitos, inexistentes no Apontaê, formato inválido;
- quando `apply` e o relatório tiver trava ativa (`travaRemocaoAtiva === true` ou `travaSubstituicaoAtiva === true`; as listas `travaRemocao`/`travaSubstituicao` com os `erpProdutoId` retidos só vêm quando não vazias), mostrar um botão secundário **"Gravar liberando a trava"** que chama `rodarSyncCatalogo(true, [...travas ativas])`. Sem trava, o botão não existe;
- `rep.fora_do_envio_guardado`, quando vier, aparece numa linha: "Fora do envio até o Fire ser corrigido: 3174, 3381…".

Os nomes das chaves foram fechados com a sessão do Flow em 15/10 (plano do Flow, commit 9e14e070): `travaRemocaoAtiva`/`travaSubstituicaoAtiva` booleanos sempre presentes, iguais no dry-run e no apply. A Task 7 Step 6 confere com o relatório real da Fatia A.

- [ ] **Step 4: Run tests to verify they pass**

Run: `"../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests/test_admin_environments_routes.py tests/test_flowpcp_catalogo_diario.py`
Expected: PASS.

- [ ] **Step 5: Validar a tela**

Subir o portal local (`portless`, conforme o CLAUDE.md global) com um `run_catalogo_sync` falso que devolve um relatório com os três blocos e uma trava ativa, abrir `/admin/ambientes/{id}` e rodar `/hm-designer` no bloco do catálogo: 320/768/1280 px, foco visível no botão novo, contraste dos destaques, números com `tabular-nums`, SEQs longos sem estourar a largura. Corrigir os críticos antes do commit.

- [ ] **Step 6: Commit**

```bash
git add app/web/routes_environments.py app/integrations/flowpcp/catalogo_diario.py app/web/static/admin-ambiente-edit.html tests/test_admin_environments_routes.py tests/test_flowpcp_catalogo_diario.py
git commit -m "feat(catalogo): confirmações das travas na rota, 409, diário com composição e tela com os blocos"
```

---

### Task 7: Docs, suíte, validação no Fire real e PR

**Files:**
- Modify: `docs/ai/modules/flowpcp.md` (§4, depois do parágrafo "Fora do envio")
- Modify: `CLAUDE.md` (bloco de variáveis de ambiente)
- Modify: `CHANGELOG.md` (seção `## Não publicado`)

- [ ] **Step 1: Doc do módulo**

Em `docs/ai/modules/flowpcp.md`, depois do parágrafo **Fora do envio**:

```markdown
**Composição e código do Apontaê** (`catalogo_composicao.py`, desde 15/10/2026, por empresa em
`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=mm`): cada item leva `componentes`
(`[{erpProdutoId, quantidade}]`, de `PRODUTOS_KIT`, um nível) e `codigoMes`
(`CODPROD_ALTERN` só dígitos, até 9, só em `simples` ativo, sem repetição no envio).
Chave `componentes` ausente = "não mexer" no Flow; `[]` só em par; kit/pack sem
`PRODUTOS_KIT` vai sem a chave (C3: o PCP pode ter composição provisória no Flow).
Leitura com erro, vazia ou sem nenhum kit do extract = ausente em todos. Filho fora do
envio, repetido, nulo, quantidade fora de 0 < q ≤ 100000, mais de 50 filhos ou kit dentro
dele mesmo: ausente naquele item, com log. Quantidade que não bate com o nome: só aviso.
O relatório ganha `composicao_portal`. Apply sem repetição depois de conectar, 300 s;
409 = o Flow já está gravando. Travas do Flow liberadas só pela rota manual
(`confirmar_remocao=1`, `confirmar_substituicao=1`).
**Exclusão guardada:** `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>` sai de todo envio (manual
e diário), com a composição ligada ou não; valor inválido não envia nada.
Só ligar a composição depois de o Flow aceitar os campos (Fatia A); antes disso ele
recusa o lote com 400. Spec: `docs/superpowers/specs/2026-10-15-catalogo-composicao-portal-design.md`.
```

- [ ] **Step 2: CLAUDE.md**

No bloco de variáveis, logo depois das linhas de `PORTAL_FLOWPCP_CATALOGO_DIARIO`:

```env
PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=  # slugs (ex.: mm) que mandam composição dos kits e código
                                     # do Apontaê no catálogo. Só com o Flow aceitando (Fatia A).
PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM=  # SEQs fora de todo envio de catálogo da empresa (manual e
                                     # diário) até o cadastro no Fire ser corrigido.
```

- [ ] **Step 3: CHANGELOG (seção `## Não publicado`, substituindo `_(nada ainda)_`)**

```markdown
1) **O catálogo pode levar ao Flow de que cada kit é feito e o código de cada par no Apontaê.** Fica desligado até o Flow estar pronto para receber, e liga por empresa. Kit que não tem composição no Fire não apaga a que o PCP cadastrou no Flow. Se o portal não conseguir ler a composição no Fire, ele não manda nada sobre composição, e o Flow mantém a que tem.
2) **Produto com cadastro errado no Fire pode ficar fora de todo envio de catálogo**, inclusive do envio diário, até ser corrigido.
3) **Gravar o catálogo no Flow não repete o envio quando o Flow demora**, e a tela avisa quando o Flow já está gravando.
```

- [ ] **Step 4: Lint e suíte completa**

Run: `"../importar pedidos/.venv/bin/ruff" check app tests && "../importar pedidos/.venv/bin/pytest" -q -p no:cacheprovider tests`
Expected: `All checks passed!` e todos os testes passando.

- [ ] **Step 5: Validação real, só leitura, no Fire de produção da MM**

Script no scratchpad (nunca no repo), com `FB_PASSWORD` no ambiente, conexão `READ_COMMITED_RO`, assert de `SELECT` em todo SQL. Monta o request real com o código da branch e confere contra a medição de 15/10:

```python
import os, re, sys, firebirdsql
sys.path.insert(0, sys.argv[1])
from app.erp.catalog_extract import extract_produtos, extract_composicao
from app.integrations.flowpcp import catalogo_composicao as cc
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request

c = firebirdsql.connect(host="192.168.15.7", port=3050, database=r"C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB",
                        user="SYSDBA", password=os.environ["FB_PASSWORD"], charset="ISO8859_1")
c.set_isolation_level(firebirdsql.ISOLATION_LEVEL_READ_COMMITED_RO)
_cur = c.cursor
def _so_select():
    cur = _cur()
    _exec = cur.execute
    def execute(sql, *a):
        assert re.match(r"\s*(SELECT|WITH)\b", sql, re.I), sql[:60]
        return _exec(sql, *a)
    cur.execute = execute
    return cur
c.cursor = _so_select

dtos = extract_produtos(c, apenas_meias=True)
comp = cc.montar_composicao(dtos, extract_composicao(c))
codigos, repetidos = cc.codigos_mes(dtos)
itens = build_catalogo_request(dtos, dry_run=True, full_sync=True, importador_versao="1",
                               extraido_em="t", composicao=comp, codigos_mes=codigos
                               ).model_dump(by_alias=True)["itens"]
r = cc.resumo(comp, codigos, repetidos)
print(len(itens), r["leitura_ok"], r["com_lista"], r["kits_sem_composicao"], r["pares_vazios"],
      len(r["omitidos"]), r["codigo_mes"], r["codigo_mes_repetido"], r["divergencia_nome"])
assert not [i for i in itens if i["tipo"] != "simples" and i.get("codigoMes")]
assert not [i for i in itens if i["tipo"] != "simples" and i.get("componentes") == []]
```

Expected (pode variar se o cadastro mudou depois de 15/10): `1026 True 271 321 ~434 0 ≤410`, repetidos com os pares do listrado Sam's, e a lista de divergência de nome (registrar inteira: é insumo para a MM). Registrar a saída no PR.

- [ ] **Step 6: Contra o Zod do Flow (quando a Fatia A estiver em `pcp-app` origin/main)**

Pedir à sessão do Flow (ou rodar via `tsx` com o `node_modules` do `pcp-app`, como no SP1) a validação de um lote real gerado no Step 5 contra o schema do `POST /api/portal-pedidos/catalogo`, com e sem as confirmações, e conferir os nomes das chaves do relatório (`composicao`, `vinculoMes`, travas) usados na Task 6. Se a Fatia A ainda não estiver no ar, registrar no PR que este passo fica pendente e que o interruptor **não** pode ser ligado antes.

- [ ] **Step 7: Commit, PR e CI**

```bash
git add docs/ai/modules/flowpcp.md CLAUDE.md CHANGELOG.md
git commit -m "docs(catalogo): composição, exclusão guardada e apply no módulo flowpcp, env e changelog"
git push -u origin <branch>
gh pr create --base main --title "feat(catalogo): composição dos kits e código do Apontaê no catálogo (desligado)"
```

Esperar o CI no SHA exato antes do merge; o merge (com ok do Samuel) e o pacote seguem o processo de release (tag só depois de MERGED, conferir o código no zip).
