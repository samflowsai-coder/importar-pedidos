# Flow fonte do cadastro — Portal, Etapa 1 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer o Portal mandar ao Flow os campos fiscais do produto e a carga de material (dryRun), e ler a fila de cadastros do Flow em modo `simular` (só leitura, nunca grava no Fire nem confirma no Flow).

**Architecture:** Três fluxos independentes em `app/integrations/flowpcp/`, cada um com interruptor por variável de ambiente (sem migração do `app_shared.db`) e gate por `capacidades` devolvidas pelo Flow:
- `catalogo_fiscal.py` estende o catálogo de produtos.
- `materiais_*.py` copia o padrão de `catalogo_sync`.
- `cadastro_volta.py` roda dentro de `envio_portal.ciclo`. Valida a fila com pydantic `extra="forbid"` e confere no Fire por conexão READ ONLY.

**Tech Stack:** Python 3.11, pydantic v2, fdb (Firebird 2.5), SQLite (db do ambiente), FastAPI, prometheus_client, pytest.

**Spec:** `docs/superpowers/specs/2026-10-16-flow-fonte-do-cadastro-design.md` (seções 6.0 a 6.3, 6.6 e 8). O contrato foi fechado com a sessão do Flow em 16/10. O lado Flow está na branch `feat/flow-fonte-cadastro` do pcp-app.

## Global Constraints

- Python 3.11+, `X | None`, ruff limpo (`ruff check app tests`).
- Fire de produção é **só leitura** nesta etapa. Nenhum INSERT/UPDATE/DELETE no Fire. A volta usa `FirebirdConnection().connect_somente_leitura(cfg)`.
- Nenhuma migração no `app_shared.db`. Interruptores são variáveis de ambiente. Tabelas novas no db do **ambiente** entram em `app/persistence/schema_env.py` como `CREATE TABLE IF NOT EXISTS`.
- Wire camelCase pelo papel: `erpProdutoId`, `unidadeErp`, `grupoErp`. No catálogo de produtos o `fireProdutoId` e o `unidade` existentes não mudam.
- Ausente = "não mexer". O Portal nunca manda valor inválido nem `null` nos campos fiscais novos: inválido vai **ausente**.
- `ncm`: `^\d{8}$` depois de tirar pontos e espaços. `tipoItemSped`: inteiro em `{0..10, 99}`.
- Limites das colunas do Fire (medidos em 16/10):
  - `DESCRICAO` 200, `DESCRICAO2` 200, `UNIDADE` 10;
  - `CODIGO_EAN13` 15, `CODPARANFE` 20, `CODPROD_ALTERN` 30;
  - `CLASSIF_FISCAL` 20, `CODITEMSPED` INTEGER, `GRUPO` INTEGER.
- Interruptores (valor = lista de slugs separada por vírgula, sem diferença de maiúscula):
  - `PORTAL_FLOWPCP_CATALOGO_FISCAL`;
  - `PORTAL_FLOWPCP_MATERIAIS`;
  - `PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_<SLUG>` (padrão `1,4,5,6,11,13,19`);
  - `PORTAL_FLOWPCP_CADASTRO_VOLTA=<slug>:<modo>[,<slug>:<modo>]` com modo ∈ `desligado|simular|criar|criar_e_atualizar`.
- Capacidades: `fiscal.v1`, `materiais.dryrun.v1`, `materiais.v1`, `cadastros.v1`.
- Teste nunca toca Firebird real. A cerca `_no_real_firebird` do conftest continua valendo.
- Mensagens de log e de erro em português. Comentário só onde a regra não é óbvia, com a data e o porquê, no estilo do repo.
- Rodar testes com `"/Users/samuelalves/SamFlowsAI - Projeto Cursor/importar pedidos/.venv/bin/python" -m pytest` (o worktree não tem venv).
- Commit com mensagem em português terminando em `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Sem push.

## Review Focus

1. **NCM com lixo misto:** `" 6115.95.00 "`, `"61159500 "`, `"6115-9500"`, `"6115950A"` e `""`. O esperado: os três primeiros normalizam para `61159500`, `6115950A` fica ausente com motivo `ncm_invalido` e `""` fica ausente com motivo `ncm_vazio`. Teste na Task 1.
2. **Texto do Fire com acento** (`GRUPO_PRODUTOS.GRUPO = "RESÍDUOS"`, nome com `Ç`) chegando ao JSON. O nome tem que chegar íntegro, e na volta o `"MEIA AÇÚCAR"` tem que passar na checagem WIN1252. Testes nas Tasks 1 e 3.
3. **Flow antigo sem `capacidades`** (resposta sem a chave). Tratar como lista vazia, nunca como erro de parse. Testes nas Tasks 1, 2 e 3.
4. **Fila do Flow com item malformado no meio de itens bons.** O item ruim vira erro definitivo registrado no livro, e os bons seguem. Teste na Task 3.
5. **`PORTAL_FLOWPCP_CADASTRO_VOLTA` com valor torto** (`mm`, `mm:gravar`, `mm:simular,,nasmar:xx`). O ambiente fica desligado, com ERROR no log, e nunca liga um modo de escrita por engano. Teste na Task 3.

---

## File Structure

| Arquivo | Papel |
|---|---|
| `app/integrations/flowpcp/catalogo_fiscal.py` (novo) | Puro: normaliza NCM/SPED/textos, interruptor fiscal, monta campos fiscais e pendências |
| `app/integrations/flowpcp/capacidades.py` (novo) | Puro: lê `capacidades` de qualquer resposta do Flow |
| `app/erp/catalog_extract.py` | DTO de produto ganha campos fiscais crus; `extract_materiais`, `extract_ultima_entrada` |
| `app/erp/queries.py` | Colunas fiscais nas 2 queries do catálogo; `LIST_MATERIAIS`, `LIST_ENTRADAS_NCM` |
| `app/integrations/flowpcp/catalogo_schema.py` | Campos fiscais no item |
| `app/integrations/flowpcp/catalogo_mapper.py` | Passa os campos fiscais quando ligado |
| `app/integrations/flowpcp/catalogo_sync.py` | Monta `fiscal_portal`, confere capacidade |
| `app/integrations/flowpcp/catalogo_diario.py` | Atenção quando o Flow ignorou o fiscal |
| `app/integrations/flowpcp/materiais_schema.py` (novo) | Contrato `catalogo.materiais.v1` |
| `app/integrations/flowpcp/materiais_sync.py` (novo) | Extrai, grava cópia local, envia |
| `app/persistence/materiais_fire_repo.py` (novo) | Cópia local `materiais_fire` |
| `app/integrations/flowpcp/cadastro_schema.py` (novo) | Item da fila: borda pydantic `extra="forbid"` |
| `app/integrations/flowpcp/cadastro_volta.py` (novo) | Interruptor, validação contra a cópia local e o Fire (só leitura), livro, métricas, ciclo |
| `app/persistence/cadastro_volta_repo.py` (novo) | Livro `cadastro_volta` |
| `app/persistence/schema_env.py` | Tabelas `materiais_fire` e `cadastro_volta` |
| `app/integrations/flowpcp/client.py` | `send_materiais`, `list_cadastros_pendentes` |
| `app/integrations/flowpcp/envio_portal.py` | Chama `cadastro_volta.rodar()` no ciclo |
| `app/observability/metrics.py` | Contador e gauges da volta |
| `app/web/routes_environments.py` | `POST /{env_id}/flowpcp/sync-materiais` e `GET /{env_id}/flowpcp/cadastro-volta` |
| `app/web/static/admin-ambiente-edit.html` | Bloco fiscal, bloco de materiais e bloco da volta |
| `tools/sync_materiais_fire.py` (novo) | CLI de simulação |
| `docs/ai/modules/flowpcp.md`, `CHANGELOG.md` | Seções afetadas |

---

### Task 1: Campos fiscais na ida do catálogo de produtos

**Files:**
- Create: `app/integrations/flowpcp/catalogo_fiscal.py`, `app/integrations/flowpcp/capacidades.py`, `tests/test_catalogo_fiscal.py`
- Modify:
  - `app/erp/queries.py` (`LIST_PRODUTOS_CATALOGO`, `LIST_PRODUTOS_CATALOGO_MEIAS`);
  - `app/erp/catalog_extract.py` (`ProdutoFireDTO`, `extract_produtos`);
  - `app/integrations/flowpcp/catalogo_schema.py` (`CatalogoProdutoItem`);
  - `app/integrations/flowpcp/catalogo_mapper.py`;
  - `app/integrations/flowpcp/catalogo_sync.py`;
  - `app/integrations/flowpcp/catalogo_diario.py` (`_precisa_atencao`);
  - os testes existentes que montam linha de 7 colunas para `extract_produtos`. Ache-os com `grep -rln "extract_produtos\|LIST_PRODUTOS_CATALOGO" tests`.

**Interfaces:**
- Produces:
  - `catalogo_fiscal.envio_ligado(slug: str) -> bool`
  - `catalogo_fiscal.normalizar_ncm(bruto: str | None) -> tuple[str | None, str | None]` → `(ncm, motivo)`, motivo ∈ `{None, "ncm_vazio", "ncm_invalido"}`
  - `catalogo_fiscal.normalizar_sped(bruto) -> tuple[int | None, str | None]`, motivo ∈ `{None, "sped_vazio", "sped_invalido"}`
  - `catalogo_fiscal.texto(bruto, limite: int) -> tuple[str | None, bool]` → `(valor_aparado_ou_None, excedeu)`
  - `catalogo_fiscal.campos_fiscais(d: ProdutoFireDTO) -> tuple[dict, list[str]]` → (chaves wire presentes, motivos)
  - `catalogo_fiscal.resumo(pendencias: dict[str, list[str]], enviados: int) -> dict`
  - `capacidades.de(resposta) -> frozenset[str]` (aceita modelo pydantic com `model_extra` ou dict; ausente/inválido → vazio)
  - `ProdutoFireDTO` ganha, com default `None`: `ncm_bruto: str | None`, `sped_bruto: int | None`, `grupo_codigo: int | None`, `grupo_nome: str | None`, `codigo_nfe: str | None`, `cor: str | None`
  - `CatalogoProdutoItem` ganha opcionais `ncm`, `tipoItemSped`, `grupoErp` (`CatalogoGrupoErp{codigo: str, nome: str}`), `codigoNfe`, `cor`, omitidos do JSON quando não preenchidos

- [ ] **Step 1: Testes do módulo puro (falham)**

```python
# tests/test_catalogo_fiscal.py
import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import capacidades, catalogo_fiscal as cf


@pytest.mark.parametrize("bruto,esperado", [
    ("61159500", ("61159500", None)),
    ("61.159.500", ("61159500", None)),
    (" 6115.95.00 ", ("61159500", None)),
    ("6115-9500", ("61159500", None)),
    ("6115950061159500", (None, "ncm_invalido")),
    ("611595", (None, "ncm_invalido")),
    ("6115950A", (None, "ncm_invalido")),
    ("", (None, "ncm_vazio")),
    (None, (None, "ncm_vazio")),
])
def test_normalizar_ncm(bruto, esperado):
    assert cf.normalizar_ncm(bruto) == esperado


@pytest.mark.parametrize("bruto,esperado", [
    (3, (3, None)), (4, (4, None)), (0, (0, None)), (10, (10, None)), (99, (99, None)),
    ("04", (4, None)),
    (12, (None, "sped_invalido")), (-1, (None, "sped_invalido")), ("x", (None, "sped_invalido")),
    (None, (None, "sped_vazio")),
])
def test_normalizar_sped(bruto, esperado):
    assert cf.normalizar_sped(bruto) == esperado


def test_texto_apara_e_limita():
    assert cf.texto("  BRANCO ", 120) == ("BRANCO", False)
    assert cf.texto("", 120) == (None, False)
    assert cf.texto("x" * 121, 120) == (None, True)


def _dto(**kw):
    base = dict(fire_produto_id="2645", codigo="2645", nome="KIT3 MEIA CURTO BRANCO",
                unidade="KIT", ean=None, ativo=True, tipo="kit")
    base.update(kw)
    return ProdutoFireDTO(**base)


def test_campos_fiscais_completo_com_acento():
    campos, motivos = cf.campos_fiscais(_dto(
        ncm_bruto="61.159.500", sped_bruto=4, grupo_codigo=8, grupo_nome="CONFECCIONADO",
        codigo_nfe="KIT3-MEIA-CURTO/BCOM", cor="AÇÚCAR"))
    assert campos == {"ncm": "61159500", "tipoItemSped": 4,
                      "grupoErp": {"codigo": "8", "nome": "CONFECCIONADO"},
                      "codigoNfe": "KIT3-MEIA-CURTO/BCOM", "cor": "AÇÚCAR"}
    assert motivos == []


def test_campos_fiscais_invalido_fica_ausente_com_motivo():
    campos, motivos = cf.campos_fiscais(_dto(ncm_bruto="611595", sped_bruto=12, cor="x" * 121))
    assert "ncm" not in campos and "tipoItemSped" not in campos and "cor" not in campos
    assert sorted(motivos) == ["cor_longa", "ncm_invalido", "sped_invalido"]


def test_campos_fiscais_grupo_sem_nome_fica_ausente():
    campos, _ = cf.campos_fiscais(_dto(grupo_codigo=8, grupo_nome=None))
    assert "grupoErp" not in campos


def test_envio_ligado(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", " MM , nasmar")
    assert cf.envio_ligado("mm") and cf.envio_ligado("NASMAR")
    assert not cf.envio_ligado("outra")
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_FISCAL")
    assert not cf.envio_ligado("mm")


def test_capacidades_tolera_ausente_e_lixo():
    assert capacidades.de({}) == frozenset()
    assert capacidades.de({"capacidades": None}) == frozenset()
    assert capacidades.de({"capacidades": "fiscal.v1"}) == frozenset()
    assert capacidades.de({"capacidades": ["fiscal.v1", 3]}) == frozenset({"fiscal.v1"})

    class _M:
        model_extra = {"capacidades": ["materiais.dryrun.v1"]}
    assert capacidades.de(_M()) == frozenset({"materiais.dryrun.v1"})


def test_resumo():
    r = cf.resumo({"10": ["ncm_vazio"], "11": ["ncm_invalido", "sped_invalido"]}, enviados=5)
    assert r == {"enviados": 5, "com_pendencia": 2,
                 "por_motivo": {"ncm_vazio": 1, "ncm_invalido": 1, "sped_invalido": 1},
                 "pendencias": {"10": ["ncm_vazio"], "11": ["ncm_invalido", "sped_invalido"]},
                 "capacidade_ok": None}
```

- [ ] **Step 2: Rodar e ver falhar.** `... -m pytest tests/test_catalogo_fiscal.py -q` → ImportError.

- [ ] **Step 3: Implementar `capacidades.py` e `catalogo_fiscal.py`**

```python
# app/integrations/flowpcp/capacidades.py
"""O que o Flow diz que aceita (`capacidades` na resposta). O Zod do Flow
descarta chave desconhecida em silêncio (medido em 16/10/2026): sem a
capacidade, campo novo some sem 400. Ausente ou malformado = nenhuma."""

from __future__ import annotations

FISCAL = "fiscal.v1"
MATERIAIS_DRYRUN = "materiais.dryrun.v1"
MATERIAIS = "materiais.v1"
CADASTROS = "cadastros.v1"


def de(resposta) -> frozenset[str]:
    if isinstance(resposta, dict):
        bruto = resposta.get("capacidades")
    else:
        bruto = (getattr(resposta, "model_extra", None) or {}).get("capacidades")
        if bruto is None:
            bruto = getattr(resposta, "capacidades", None)
    if not isinstance(bruto, list):
        return frozenset()
    return frozenset(c for c in bruto if isinstance(c, str))
```

```python
# app/integrations/flowpcp/catalogo_fiscal.py
"""Campos fiscais do produto no catálogo enviado ao Flow (spec
2026-10-16-flow-fonte-do-cadastro, §6.1). Puro: sem Fire, sem rede.

O Portal só manda valor válido: o que não passa na regra vai AUSENTE ("não
mexer" no Flow) e vira pendência no relatório. Em 16/10/2026, das 771 meias da
MM, 51 tinham NCM com pontos no lugar errado, 3 lixo e 19 vazio."""

from __future__ import annotations

import os
import re
from collections import Counter

from app.erp.catalog_extract import ProdutoFireDTO

SPED_VALIDOS = frozenset({*range(0, 11), 99})
LIMITE_CODIGO_NFE = 60
LIMITE_COR = 120
LIMITE_GRUPO_NOME = 80
_NCM = re.compile(r"\d{8}")
_SEPARADORES = re.compile(r"[\s.\-]")


def envio_ligado(slug: str) -> bool:
    """`PORTAL_FLOWPCP_CATALOGO_FISCAL=mm` liga por empresa. Só ligar depois de
    o Flow devolver `fiscal.v1` num dryRun."""
    bruto = os.environ.get("PORTAL_FLOWPCP_CATALOGO_FISCAL", "")
    return slug.lower() in {s.strip().lower() for s in bruto.split(",") if s.strip()}


def normalizar_ncm(bruto: str | None) -> tuple[str | None, str | None]:
    s = _SEPARADORES.sub("", str(bruto or ""))
    if not s:
        return None, "ncm_vazio"
    if _NCM.fullmatch(s):
        return s, None
    return None, "ncm_invalido"


def normalizar_sped(bruto) -> tuple[int | None, str | None]:
    if bruto is None or (isinstance(bruto, str) and not bruto.strip()):
        return None, "sped_vazio"
    try:
        v = int(str(bruto).strip())
    except ValueError:
        return None, "sped_invalido"
    return (v, None) if v in SPED_VALIDOS else (None, "sped_invalido")


def texto(bruto, limite: int) -> tuple[str | None, bool]:
    s = str(bruto).strip() if bruto is not None else ""
    if not s:
        return None, False
    if len(s) > limite:
        return None, True
    return s, False


def campos_fiscais(d: ProdutoFireDTO) -> tuple[dict, list[str]]:
    campos: dict = {}
    motivos: list[str] = []
    ncm, m = normalizar_ncm(d.ncm_bruto)
    if ncm:
        campos["ncm"] = ncm
    elif m:
        motivos.append(m)
    sped, m = normalizar_sped(d.sped_bruto)
    if sped is not None:
        campos["tipoItemSped"] = sped
    elif m == "sped_invalido":
        motivos.append(m)
    nome_grupo, _ = texto(d.grupo_nome, LIMITE_GRUPO_NOME)
    if d.grupo_codigo is not None and nome_grupo:
        campos["grupoErp"] = {"codigo": str(d.grupo_codigo), "nome": nome_grupo}
    nfe, _ = texto(d.codigo_nfe, LIMITE_CODIGO_NFE)
    if nfe:
        campos["codigoNfe"] = nfe
    cor, longa = texto(d.cor, LIMITE_COR)
    if cor:
        campos["cor"] = cor
    elif longa:
        motivos.append("cor_longa")
    return campos, motivos


def resumo(pendencias: dict[str, list[str]], enviados: int) -> dict:
    por_motivo = Counter(m for ms in pendencias.values() for m in ms)
    return {
        "enviados": enviados,
        "com_pendencia": len(pendencias),
        "por_motivo": dict(por_motivo),
        "pendencias": pendencias,
        "capacidade_ok": None,
    }
```

Note: `sped_vazio` não vira pendência. O tipo SPED vazio é comum em material antigo e não é erro do cadastro de meias. Já o NCM vazio vira pendência, porque a nota exige NCM.

- [ ] **Step 4: DTO e queries.**
  - Em `ProdutoFireDTO`, adicione no fim, todos com default `None`: `ncm_bruto: str | None = None`, `sped_bruto: int | None = None`, `grupo_codigo: int | None = None`, `grupo_nome: str | None = None`, `codigo_nfe: str | None = None`, `cor: str | None = None`.
  - Nas duas queries do catálogo:
    - adicione `LEFT JOIN GRUPO_PRODUTOS G ON G.CODIGO = P.GRUPO`;
    - inclua as colunas, nesta ordem, depois de `P.CODPROD_ALTERN`: `P.CLASSIF_FISCAL, P.CODITEMSPED, P.GRUPO, G.GRUPO AS GRUPO_NOME, P.CODPARANFE, P.DESCRICAO2`;
    - na query MEIAS, o JOIN entra depois de `JOIN PRODUTOS P ON P.SEQ = R.SEQ`.
  - Em `extract_produtos`, desempacote as 13 colunas: `for seq, desc, uni, ean, bloqueado, is_kit, alternativo, ncm, sped, grupo, grupo_nome, nfe, cor in rows`. Preencha os campos novos com `_clean(...)` nos textos e `int(...)`/`None` em `grupo`/`sped`, com o mesmo cuidado de `_clean` para não quebrar com valor vazio.
  - Ajuste os testes antigos que montam linhas de 7 colunas, acrescentando `None, None, None, None, None, None` a cada linha. Não mude o que eles testam.

- [ ] **Step 5: Schema e mapper.** Em `catalogo_schema.py`:

```python
class CatalogoGrupoErp(BaseModel):
    codigo: str
    nome: str
```

`CatalogoProdutoItem` ganha `ncm: str | None = None`, `tipoItemSped: int | None = None`, `grupoErp: CatalogoGrupoErp | None = None`, `codigoNfe: str | None = None`, `cor: str | None = None` (com `# noqa: N815` onde o ruff pedir). Acrescente esses 5 nomes à tupla de `_omite_nao_preenchidos`, para que só apareçam quando forem preenchidos explicitamente.

Em `catalogo_mapper._item` e `build_catalogo_request`, adicione o parâmetro `fiscal: dict[str, dict] | None = None`: o mapa SEQ → campos de `campos_fiscais`. Quando `fiscal` não é `None`, faça `campos.update(fiscal.get(d.fire_produto_id, {}))`. O `grupoErp` entra como `CatalogoGrupoErp(**...)`.

- [ ] **Step 6: Testes do schema e do sync (falham), em `tests/test_catalogo_fiscal.py`**

```python
import json

from app.integrations.flowpcp import catalogo_sync
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import CatalogoContagens, CatalogoReconciliacaoResponse


def _req(dtos, fiscal=None):
    return build_catalogo_request(dtos, dry_run=True, full_sync=True, importador_versao="1",
                                  extraido_em="2026-10-16T00:00:00Z", fiscal=fiscal)


def test_sem_fiscal_payload_igual_ao_de_hoje():
    d = _dto(ncm_bruto="61159500", sped_bruto=4)
    corpo = _req([d]).model_dump(by_alias=True)["itens"][0]
    assert set(corpo) == {"fireProdutoId", "codigo", "nome", "unidade", "ean", "ativo", "tipo"}


def test_com_fiscal_so_chaves_validas_e_sem_null():
    d = _dto(ncm_bruto="611595", sped_bruto=4, grupo_codigo=8, grupo_nome="RESÍDUOS")
    campos, _ = cf.campos_fiscais(d)
    corpo = _req([d], fiscal={d.fire_produto_id: campos}).model_dump(by_alias=True)["itens"][0]
    assert corpo["tipoItemSped"] == 4 and corpo["grupoErp"] == {"codigo": "8", "nome": "RESÍDUOS"}
    assert "ncm" not in corpo and "codigoNfe" not in corpo and "cor" not in corpo
    assert "RESÍDUOS" in json.dumps(corpo, ensure_ascii=False)


class _Cfg:
    enabled = True
    catalogo_push = True
    catalogo_apenas_meias = False


class _Client:
    def __init__(self, caps):
        self.caps, self.sent = caps, None

    def send_catalogo(self, request):
        self.sent = request
        extra = {} if self.caps is None else {"capacidades": self.caps}
        return CatalogoReconciliacaoResponse(contagens=CatalogoContagens(), **extra)

    def close(self):
        pass


class _Env:
    def execute(self, *a, **k):
        class C:
            fetchone = staticmethod(lambda: (0,))
            fetchall = staticmethod(lambda: [])
        return C()

    def executemany(self, *a):
        pass


def _rodar(monkeypatch, caps, ligado=True):
    dtos = [_dto(ncm_bruto="61159500", sped_bruto=4), _dto(fire_produto_id="9", codigo="9", ncm_bruto="")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    if ligado:
        monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", "mm")
    else:
        monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", raising=False)
    client = _Client(caps)
    rep = catalogo_sync.run_catalogo_sync("mm", now_iso="2026-10-16T00:00:00Z",
                                          _client=client, _fire_conn=object(), _env_conn=_Env())
    return rep, client


def test_sync_fiscal_ligado_com_capacidade(monkeypatch):
    rep, client = _rodar(monkeypatch, ["fiscal.v1"])
    assert client.sent.itens[0].ncm == "61159500"
    fp = rep.model_extra["fiscal_portal"]
    assert fp["capacidade_ok"] is True and fp["pendencias"] == {"9": ["ncm_vazio"]}


def test_sync_fiscal_ligado_sem_capacidade_marca_e_loga(monkeypatch, caplog_loguru):
    rep, _ = _rodar(monkeypatch, None)
    assert rep.model_extra["fiscal_portal"]["capacidade_ok"] is False
    assert any("ignorou os campos fiscais" in m for m in caplog_loguru)


def test_sync_fiscal_desligado_nao_manda_nem_relata(monkeypatch):
    rep, client = _rodar(monkeypatch, ["fiscal.v1"], ligado=False)
    assert client.sent.itens[0].ncm is None
    assert "fiscal_portal" not in (rep.model_extra or {})
```

Se o conftest não tiver uma fixture `caplog_loguru`, crie-a neste arquivo de teste:

```python
@pytest.fixture
def caplog_loguru():
    from app.utils.logger import logger
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    logger.remove(hid)
```

Antes, confira com `grep -rn "caplog_loguru\|logger.add(" tests/conftest.py`: se já existir uma fixture equivalente, use-a e não duplique.

- [ ] **Step 7: Sync.** Em `run_catalogo_sync`, depois de calcular `enviados`:

```python
    fiscal = fiscal_resumo = None
    if catalogo_fiscal.envio_ligado(slug):
        fiscal, pendencias = {}, {}
        for d in enviados:
            campos, motivos = catalogo_fiscal.campos_fiscais(d)
            fiscal[d.fire_produto_id] = campos
            if motivos:
                pendencias[d.fire_produto_id] = motivos
        fiscal_resumo = catalogo_fiscal.resumo(pendencias, enviados=len(enviados))
```

Passe `fiscal=fiscal` para `build_catalogo_request`. Depois de `rep = client.send_catalogo(request)`:

```python
        if fiscal_resumo is not None:
            ok = capacidades.FISCAL in capacidades.de(rep)
            fiscal_resumo["capacidade_ok"] = ok
            rep.fiscal_portal = fiscal_resumo
            if not ok:
                logger.error(
                    f"catalogo sync env={slug}: o Flow ignorou os campos fiscais (sem "
                    f"'{capacidades.FISCAL}' na resposta) — tire '{slug}' de PORTAL_FLOWPCP_CATALOGO_FISCAL "
                    "até o Flow aceitar"
                )
            elif fiscal_resumo["com_pendencia"]:
                logger.warning(
                    f"catalogo sync env={slug} fiscal: {fiscal_resumo['com_pendencia']} com pendência "
                    f"{fiscal_resumo['por_motivo']}"
                )
```

Acrescente esse comportamento à docstring de `run_catalogo_sync`, numa frase.

- [ ] **Step 8: Diário.** Em `catalogo_diario._precisa_atencao`, adicione `fiscal = extra.get("fiscal_portal") or {}` e `or fiscal.get("capacidade_ok") is False` no `return`. Inclua `"fiscal_portal"` em `_BLOCOS`. Teste novo em `tests/test_catalogo_diario.py`, no estilo dos casos de atenção que já existem: `CatalogoReconciliacaoResponse(contagens=..., fiscal_portal={"capacidade_ok": False})` precisa dar `True`.

- [ ] **Step 9: Rodar direcionado.** `... -m pytest tests/test_catalogo_fiscal.py tests/test_catalogo_sync.py tests/test_catalogo_diario.py tests/test_catalogo_composicao.py -q` passa. Depois `ruff check app tests`.

- [ ] **Step 10: Commit.** `feat(catalogo): campos fiscais do produto na ida ao Flow (gate por capacidade fiscal.v1)`

---

### Task 2: Carga de material (dryRun) Fire → Portal → Flow

**Files:**
- Create:
  - `app/integrations/flowpcp/materiais_schema.py`, `app/integrations/flowpcp/materiais_sync.py`;
  - `app/persistence/materiais_fire_repo.py`;
  - `tools/sync_materiais_fire.py`;
  - `tests/test_materiais_sync.py`, `tests/test_materiais_extract.py`.
- Modify:
  - `app/erp/queries.py`, `app/erp/catalog_extract.py`;
  - `app/persistence/schema_env.py`;
  - `app/integrations/flowpcp/client.py`;
  - `app/web/routes_environments.py`;
  - `tests/test_admin_environments_routes.py` (ou um arquivo novo `tests/test_sync_materiais_route.py`).

**Interfaces:**
- Consumes: `catalogo_fiscal.normalizar_ncm`, `normalizar_sped`, `texto` (Task 1); `capacidades.de`, `MATERIAIS`, `MATERIAIS_DRYRUN` (Task 1).
- Produces:
  - `MaterialFireDTO` (dataclass frozen em `catalog_extract.py`): `erp_produto_id: str`, `codigo: str`, `nome: str`, `unidade: str | None`, `ean: str | None`, `ativo: bool`, `ncm_bruto: str | None`, `sped_bruto: int | None`, `grupo_codigo: int | None`, `grupo_nome: str | None`
  - `extract_materiais(fire_conn) -> list[MaterialFireDTO]`
  - `extract_ultima_entrada(fire_conn) -> dict[str, tuple[str, date]]` (SEQ → (NCM bruto, data de emissão) da entrada mais recente)
  - `materiais_sync.envio_ligado(slug) -> bool`, `grupos_producao(slug) -> frozenset[int]`, `exige_aprovacao(grupo, sped, grupos) -> bool`
  - `materiais_sync.run_materiais_sync(slug, *, dry_run=True, full_sync=True, now_iso=None, _client=None, _fire_conn=None, _env_conn=None) -> MateriaisResposta | MateriaisLocalResult | None`
  - `materiais_sync.ApplySemCapacidadeError(ValueError)` e `MateriaisDesligadoError(ValueError)`
  - `FlowPCPClient.send_materiais(request: MateriaisRequest) -> MateriaisResposta` (path `/api/portal-pedidos/materiais`)

- [ ] **Step 1: Queries.** Em `app/erp/queries.py`:

```python
# Carga de material (spec 2026-10-16-flow-fonte-do-cadastro §6.2): tudo que não é
# CONFECCIONADO (grupo 8). Vai com BLOQUEADO para o Flow saber o ativo; o Flow não
# cria insumo inativo novo. Em 16/10/2026: ~381 na MM (.7).
LIST_MATERIAIS = """
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
           P.CLASSIF_FISCAL, P.CODITEMSPED, P.GRUPO, G.GRUPO AS GRUPO_NOME
    FROM PRODUTOS P
    LEFT JOIN GRUPO_PRODUTOS G ON G.CODIGO = P.GRUPO
    WHERE P.GRUPO IS NOT NULL AND P.GRUPO <> 8
    ORDER BY P.SEQ
"""

# NCM das notas de ENTRADA (CFOP 1xxx/2xxx/3xxx) de material nos últimos 24
# meses; a mais recente por produto é escolhida em Python (Firebird 2.5 não tem
# janela). Referência para o fiscal, nunca valor do cadastro.
LIST_ENTRADAS_NCM = """
    SELECT I.CODPRODUTO, N.NF_EMISSAO, I.NCM
    FROM NOTAPROD I
    JOIN NOTA N ON N.CODIGO = I.CODNOTA
    JOIN PRODUTOS P ON P.SEQ = I.CODPRODUTO
    WHERE P.GRUPO <> 8
      AND N.NF_EMISSAO >= DATEADD(-24 MONTH TO CURRENT_DATE)
      AND SUBSTRING(TRIM(I.CFOP) FROM 1 FOR 1) IN ('1', '2', '3')
      AND I.NCM IS NOT NULL
"""
```

Nota: `LIST_MATERIAIS` traz também os bloqueados, com `ativo=false`. Isso **simplifica** a spec §6.2, que mandava só os bloqueados já conhecidos. Atualize a frase da spec na Task 5, porque o Flow não cria insumo inativo novo (combinado; ver o ledger).

- [ ] **Step 2: Testes do extract (falham)**

```python
# tests/test_materiais_extract.py
from datetime import date

from app.erp.catalog_extract import MaterialFireDTO, extract_materiais, extract_ultima_entrada


class _Cur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, *a):
        self.sql = sql

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _Cur(self.rows)


def test_extract_materiais_limpa_e_marca_ativo():
    rows = [(250, " FIO 70/2 ", "KG ", None, "Nao", "5402.33.10", 1, 1, "FIO - MATERIA-PRIMA"),
            (251, "SACO PLASTICO", "PC", "789", "Sim", None, None, 19, "EMBALAGEM")]
    out = extract_materiais(_Conn(rows))
    assert out[0] == MaterialFireDTO("250", "250", "FIO 70/2", "KG", None, True, "5402.33.10", 1, 1, "FIO - MATERIA-PRIMA")
    assert out[1].ativo is False and out[1].sped_bruto is None and out[1].grupo_codigo == 19


def test_extract_ultima_entrada_pega_a_mais_recente():
    rows = [(250, date(2026, 1, 5), "54023310"), (250, date(2026, 7, 1), "5402.33.90"),
            (251, date(2025, 3, 1), " 39232190 ")]
    assert extract_ultima_entrada(_Conn(rows)) == {
        "250": ("5402.33.90", date(2026, 7, 1)), "251": ("39232190", date(2025, 3, 1))}
```

- [ ] **Step 3: Implementar em `catalog_extract.py`.**
  - Crie o dataclass `MaterialFireDTO`.
  - `extract_materiais`: mesmo padrão de cursor e `finally: close` de `extract_produtos`; `ativo = str(bloqueado or "").strip().lower() != "sim"`; `grupo_codigo`/`sped_bruto` com `int(...)` quando não forem `None`.
  - `extract_ultima_entrada`: para cada linha, `seq = str(cod)`; mantenha a de maior data; o NCM passa por `_clean`.

- [ ] **Step 4: Schema `materiais_schema.py`**

```python
"""Contrato `catalogo.materiais.v1` (Portal → Flow), fechado com o Flow em 16/10/2026."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_serializer

from app.integrations.flowpcp.catalogo_schema import CatalogoGrupoErp, CatalogoOrigem

_OPCIONAIS = ("ncm", "tipoItemSped", "ncmUltimaEntrada", "ultimaEntradaEm")


class MaterialItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    erpProdutoId: str  # noqa: N815
    codigo: str
    nome: str = Field(min_length=1, max_length=255)
    unidadeErp: str | None = Field(default=None, max_length=16)  # noqa: N815
    ncm: str | None = Field(default=None, pattern=r"^\d{8}$")
    tipoItemSped: int | None = None  # noqa: N815
    grupoErp: CatalogoGrupoErp  # noqa: N815
    ean: str | None = Field(default=None, max_length=32)
    ativo: bool
    exigeAprovacao: bool  # noqa: N815
    ncmUltimaEntrada: str | None = Field(default=None, pattern=r"^\d{8}$")  # noqa: N815
    ultimaEntradaEm: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # noqa: N815

    @model_serializer(mode="wrap")
    def _omite(self, handler):
        corpo = handler(self)
        for campo in _OPCIONAIS:
            if corpo.get(campo) is None:
                corpo.pop(campo, None)
        return corpo


class MateriaisRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default="catalogo.materiais.v1", alias="schema")
    dryRun: bool  # noqa: N815
    fullSync: bool  # noqa: N815
    itens: list[MaterialItem] = Field(min_length=1, max_length=5000)
    origem: CatalogoOrigem


class MateriaisResposta(BaseModel):
    """Relatório do Flow. `extra="allow"`: o Flow é dono do shape (resumo, amostras, capacidades)."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    dry_run: bool | None = Field(default=None, alias="dryRun")
    resumo: dict = Field(default_factory=dict)
    amostras: dict = Field(default_factory=dict)
```

- [ ] **Step 5: Tabela e repo.** Em `schema_env.py`, logo depois de `catalogo_fire`:

```sql
-- Cópia local dos materiais do Fire (tudo fora do grupo 8 CONFECCIONADO), spec
-- 2026-10-16-flow-fonte-do-cadastro §6.2. Snapshot substitutivo a cada sync.
CREATE TABLE IF NOT EXISTS materiais_fire (
    erp_produto_id  TEXT PRIMARY KEY,
    nome            TEXT NOT NULL,
    unidade         TEXT,
    ean             TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1,
    grupo_codigo    INTEGER,
    ncm             TEXT,
    tipo_item_sped  INTEGER,
    exige_aprovacao INTEGER NOT NULL DEFAULT 0,
    extraido_em     TEXT NOT NULL
);
```

`materiais_fire_repo.py` segue o modelo de `catalogo_fire_repo.py`:
- `replace_all(conn, linhas: list[dict], *, extraido_em)`, onde as chaves de cada dict são as colunas;
- `seqs(conn) -> set[str]`.

- [ ] **Step 6: Testes do sync (falham)**, em `tests/test_materiais_sync.py`:
  - `exige_aprovacao`:
    - `(1, None, {1,4})` → True;
    - `(12, 1, {1,4})` → True (tipo SPED de matéria-prima);
    - `(12, 7, {1,4})` → False;
    - `(20, 10, {1,4})` → True;
    - `(None, None, {1,4})` → False.
  - `grupos_producao("mm")` sem variável → `{1,4,5,6,11,13,19}`. Com `PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_MM="4, 19"` → `{4,19}`. Com um valor inválido (`"4,x"`) → ERROR no log e o padrão.
  - Com o interruptor desligado e `apply=False`: grava a cópia local e devolve `MateriaisLocalResult`, sem chamar o client.
  - Com o interruptor ligado e dryRun:
    - o client recebe `MateriaisRequest`;
    - um item com NCM `"5402.33.10"` vai com `ncm="54023310"`;
    - a entrada de `date(2026,7,1)` vira `ultimaEntradaEm="2026-07-01"`;
    - um item sem nome do grupo vai com `grupoErp={"codigo": "12", "nome": "GRUPO 12"}`. O Flow exige `grupoErp`; o nome de reserva é `f"GRUPO {codigo}"`, e o motivo `grupo_sem_nome` entra em `materiais_portal.pendencias`;
    - um item com `grupo_codigo=None` fica fora do envio, com o motivo `sem_grupo` em `materiais_portal.fora`.
  - `dry_run=False` com resposta prévia sem `materiais.v1`: levanta `ApplySemCapacidadeError` **antes** de enviar o apply. O teste confere que o client recebeu só 1 chamada, com `dryRun=True`. O apply faz primeiro um dryRun de sondagem e confere `capacidades`.
  - Resposta sem `materiais.dryrun.v1` (Flow antigo) → ERROR no log, `materiais_portal.capacidade_ok=False`.
  - Item com nome maior que 255, ou EAN maior que 32: fora do envio, com o motivo `campo_excede` em `materiais_portal.fora`. O lote não falha.

- [ ] **Step 7: Implementar `materiais_sync.py`**, no esqueleto de `run_catalogo_sync`:
  1. Lê a config.
  2. Abre o Fire (`connect_with_config`), roda `extract_materiais` e `extract_ultima_entrada`.
  3. Monta os itens.
  4. Grava a cópia local sempre.
  5. Envia se o interruptor estiver ligado. `dry_run=False` faz antes o dryRun de sondagem e exige `capacidades.MATERIAIS`.

  Monte cada item com `normalizar_ncm` e `normalizar_sped` (inválido vai ausente + pendência), `texto(nome, 255)`, `texto(unidade, 16)` e `texto(ean, 32)`. A resposta ganha `materiais_portal = {"enviados", "fora": {seq: motivo}, "pendencias": {seq: [motivos]}, "capacidade_ok"}`.

  O cliente HTTP vem de `catalogo_sync._build_client`: importe a função, não duplique.

  `client.send_materiais` copia `send_catalogo`:
  - `_idem_materiais` com o mesmo esquema de hash, prefixo `materiais-`;
  - apply com `_apply_timeout()` e `apply_post_policy()`;
  - 409 → `FlowPCPApplyEmAndamentoError`.

- [ ] **Step 8: Rota.** Em `routes_environments.py`, depois de `sync_clientes_flowpcp`:

```python
@router.post("/{env_id}/flowpcp/sync-materiais")
def sync_materiais_flowpcp(env_id: str, apply: bool = False, _=Depends(require_admin)):
    """Carga de material (Fire → FlowPCP), spec 2026-10-16 §6.2. `apply=false` (padrão):
    o Flow só calcula o relatório. `apply=true` exige a capacidade `materiais.v1` do Flow
    (422 antes de enviar se faltar). A cópia local (`materiais_fire`) é sempre atualizada."""
```

  Corpo, no estilo de `sync_clientes_flowpcp`:
  - 404 sem ambiente; 409 sem flowpcp;
  - `ApplySemCapacidadeError` → 422 com a mensagem;
  - `FlowPCPApplyEmAndamentoError` → 409;
  - outra exceção → 502 "Falha na carga de materiais: ...";
  - `MateriaisLocalResult` → `{"local_only": True, "itens", "extraido_em"}`;
  - senão, `rep.model_dump(by_alias=True)`.

  Teste de rota com `TestClient` e `run_materiais_sync` em monkeypatch, cobrindo 404, 409, 422 e o caminho feliz. Use a fixture `setup` de `tests/test_admin_environments_routes.py` como modelo; o bypass de admin segue o que esse arquivo já faz.

- [ ] **Step 9: CLI.** `tools/sync_materiais_fire.py <slug> [--apply]` imprime o relatório em JSON e segue o formato de `tools/sync_catalogo_fire.py`. Leia esse arquivo e copie a estrutura. Sem teste: é um invólucro de 20 linhas sobre `run_materiais_sync`.

- [ ] **Step 10: Rodar direcionado + ruff. Commit.** `feat(materiais): carga de material do Fire para o Flow em dryRun (catalogo.materiais.v1)`

---

### Task 3: Volta em modo `simular`

**Files:**
- Create:
  - `app/integrations/flowpcp/cadastro_schema.py`, `app/integrations/flowpcp/cadastro_volta.py`;
  - `app/persistence/cadastro_volta_repo.py`;
  - `tests/test_cadastro_schema.py`, `tests/test_cadastro_volta.py`.
- Modify:
  - `app/persistence/schema_env.py`;
  - `app/integrations/flowpcp/client.py` (`list_cadastros_pendentes`);
  - `app/integrations/flowpcp/envio_portal.py` (`ciclo`);
  - `app/observability/metrics.py`;
  - `app/web/routes_environments.py` (`GET /{env_id}/flowpcp/cadastro-volta`).

**Interfaces:**
- Consumes: `capacidades.de` e `CADASTROS` (Task 1); `materiais_fire_repo.seqs` (Task 2); `catalogo_fire_repo.list_all` (existente).
- Produces:
  - `cadastro_schema.CadastroPendente` (pydantic, `extra="forbid"`) e `PendentesResposta(capacidades: list[str] = [], cadastros: list[dict])`. A lista vem crua, e cada item é validado à parte.
  - `cadastro_schema.validar_item(bruto: dict) -> tuple[CadastroPendente | None, str | None, str | None]` → `(item, codigo_erro, mensagem)`
  - `cadastro_schema.codifica_win1252(s: str) -> bool`
  - `cadastro_volta.modo(slug) -> str`, onde `MODOS = ("desligado", "simular", "criar", "criar_e_atualizar")`
  - `cadastro_volta.processar_ambiente(slug, *, _client=None, _fire_conn=None, _env_conn=None) -> dict` (resumo do ciclo)
  - `cadastro_volta.rodar() -> dict[str, dict]` (todos os ambientes; nunca levanta)
  - `cadastro_volta_repo.registrar(conn, *, cadastro_id, versao, estado, acao, tipo, erro_codigo, erro_mensagem, plano_json) -> bool` (False se `(cadastro_id, versao)` já existir); `listar(conn, limite=50) -> list[dict]`; `contar_por_estado(conn) -> dict[str, int]`
  - `FlowPCPClient.list_cadastros_pendentes(limite: int = 20) -> PendentesResposta` (GET `/api/portal-pedidos/cadastros/pendentes`)

- [ ] **Step 1: Testes do schema (falham)**

```python
# tests/test_cadastro_schema.py
import pytest

from app.integrations.flowpcp.cadastro_schema import codifica_win1252, validar_item

BOM = {"cadastroId": "7b0c1c3e-0f7e-4a51-9c39-2a8f3f1d9a10", "versao": 1, "acao": "criar",
       "erpProdutoId": None, "tipo": "kit", "nome": "KIT3 MEIA CURTO PRETO", "unidadeErp": "KIT",
       "ean": None, "ncm": "61159500", "tipoItemSped": 4, "grupoErp": {"codigo": "8"},
       "codigoNfe": "KIT3-MEIA-CURTO/PRETO", "cor": "PRETO",
       "componentes": [{"erpProdutoId": "3170", "quantidade": 3}], "codigoMes": None,
       "aprovadoEm": "2026-10-20T12:00:00Z"}


def test_item_bom_passa():
    item, cod, _ = validar_item(dict(BOM))
    assert cod is None and item.nome == "KIT3 MEIA CURTO PRETO"


@pytest.mark.parametrize("campo,valor,codigo", [
    ("extra", 1, "contrato_invalido"),
    ("acao", "apagar", "contrato_invalido"),
    ("tipo", "servico", "contrato_invalido"),
    ("nome", "x" * 201, "campo_excede_fire"),
    ("cor", "x" * 201, "campo_excede_fire"),
    ("unidadeErp", "UNIDADES123", "campo_excede_fire"),
    ("ean", "1" * 16, "campo_excede_fire"),
    ("codigoNfe", "x" * 21, "campo_excede_fire"),
    ("codigoMes", "1" * 31, "campo_excede_fire"),
    ("ncm", "6115950", "contrato_invalido"),
    ("tipoItemSped", 12, "contrato_invalido"),
    ("nome", "MEIA ☃ NEVE", "caractere_invalido"),
])
def test_item_ruim_vira_erro_definitivo(campo, valor, codigo):
    bruto = dict(BOM)
    bruto[campo] = valor
    item, cod, msg = validar_item(bruto)
    assert item is None and cod == codigo and len(msg) <= 500


def test_acento_portugues_passa_win1252():
    assert codifica_win1252("MEIA AÇÚCAR ÇÃO") and not codifica_win1252("☃")
    bruto = dict(BOM, nome="MEIA AÇÚCAR")
    assert validar_item(bruto)[1] is None


def test_kit_sem_componentes_e_regras_de_componente():
    assert validar_item(dict(BOM, componentes=None))[1] == "componente_invalido"
    assert validar_item(dict(BOM, componentes=[]))[1] == "componente_invalido"
    assert validar_item(dict(BOM, componentes=[{"erpProdutoId": "31a", "quantidade": 1}]))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, componentes=[{"erpProdutoId": "3170", "quantidade": 0}]))[1] == "componente_invalido"
    muitos = [{"erpProdutoId": str(i), "quantidade": 1} for i in range(1, 52)]
    assert validar_item(dict(BOM, componentes=muitos))[1] == "componente_invalido"


def test_atualizar_exige_erp_id_e_criar_proibe():
    assert validar_item(dict(BOM, acao="atualizar"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, erpProdutoId="2645"))[1] == "contrato_invalido"
    assert validar_item(dict(BOM, acao="atualizar", erpProdutoId="2645"))[1] is None


def test_mensagem_nao_vaza_valor_inteiro():
    _, _, msg = validar_item(dict(BOM, nome="SEGREDO" * 100))
    assert "SEGREDO" * 3 not in msg
```

- [ ] **Step 2: Implementar `cadastro_schema.py`.**

**Modelo `CadastroPendente`** (`ConfigDict(extra="forbid")`):
- `cadastroId: UUID`, `versao: int = Field(ge=1)`;
- `acao: Literal["criar","atualizar","inativar"]`;
- `erpProdutoId: str | None` com pattern `^\d{1,10}$`;
- `tipo: Literal["simples","kit","pack","material"]`;
- `nome: str`, `unidadeErp: str | None`, `ean: str | None`;
- `ncm: str | None` com pattern `^\d{8}$`;
- `tipoItemSped: int | None` (validator: em {0..10, 99});
- `grupoErp: GrupoRef{codigo: str pattern ^\d{1,10}$}` (extra forbid);
- `codigoNfe`, `cor`, `codigoMes: str | None`;
- `componentes: list[ComponenteRef{erpProdutoId: str ^\d{1,10}$, quantidade: float}] | None` (extra forbid);
- `aprovadoEm: datetime`.

**`validar_item`, em ordem:**
1. `model_validate` dentro de `try`. Se der `ValidationError`, devolve `("contrato_invalido", <tipo do erro + campo, sem o valor>)`. Monte a mensagem só com `loc` e `type` de cada erro, nunca com `input`.
2. Limites das colunas do Fire, com a tabela `LIMITES = {"nome": 200, "cor": 200, "unidadeErp": 10, "ean": 15, "codigoNfe": 20, "codigoMes": 30}`. Um valor maior dá `campo_excede_fire` com a mensagem `"<campo> tem <n> caracteres; o Fire aceita <limite>"`.
3. WIN1252: qualquer campo de texto que não codifica (`s.encode("cp1252")`) dá `caractere_invalido`, com a mensagem `"<campo> tem caractere que o Fire não grava"`.
4. Regras de ação: `criar` exige `erpProdutoId is None`, e `atualizar`/`inativar` exigem que ele esteja presente. Senão, `contrato_invalido`.
5. Regras de componente: kit e pack exigem de 1 a 50 componentes, `0 < quantidade <= 100000` e nenhum igual ao próprio `erpProdutoId`. `simples` e `material` com componentes não vazios também dão `componente_invalido`.

Toda mensagem é cortada em 500 caracteres.

- [ ] **Step 3: Tabela e repo do livro.** Em `schema_env.py`:

```sql
-- Livro da volta Flow → Fire (spec 2026-10-16 §6.3). Na Etapa 1 só registra
-- `simulado`/`erro_definitivo`: nada é gravado no Fire nem confirmado no Flow.
-- (cadastro_id, versao) é o lease: o mesmo item não é processado duas vezes.
CREATE TABLE IF NOT EXISTS cadastro_volta (
    cadastro_id    TEXT NOT NULL,
    versao         INTEGER NOT NULL,
    estado         TEXT NOT NULL,
    acao           TEXT,
    tipo           TEXT,
    seq_reservado  INTEGER,
    erro_codigo    TEXT,
    erro_mensagem  TEXT,
    plano_json     TEXT,
    iniciado_em    TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cadastro_id, versao)
);
```

`cadastro_volta_repo.registrar` usa `INSERT OR IGNORE` e devolve `cursor.rowcount == 1`. `cadastro_id` e `versao` vêm do item já validado; para item que nem validou, use o `cadastroId` cru, cortado em 64 caracteres, e `versao=0`.

- [ ] **Step 4: Testes da volta (falham)**, em `tests/test_cadastro_volta.py`, com um env SQLite **real** em `tmp_path`. Para iniciar o schema do ambiente, siga o padrão de um teste existente que usa `router.env_connect` ou `schema_env`; procure com `grep -rln "schema_env\|env_connect" tests | head`. Casos:

**`modo(slug)`:**
- sem variável → `desligado`;
- `"mm:simular"` → `simular`;
- `"MM:Simular , nasmar:desligado"` → `simular` em mm;
- `"mm"` → `desligado` + ERROR;
- `"mm:gravar"` → `desligado` + ERROR;
- `"mm:simular,,nasmar:xx"` → mm `simular`, nasmar `desligado` + ERROR.

**`processar_ambiente` em `simular`:**
- **Fila vazia:** nenhum livro, e o resumo é `{"modo": "simular", "lidos": 0, ...}`.
- **Sem `cadastros.v1` nas capacidades:** processa assim mesmo (simular é só leitura) e o resumo leva `capacidade_ok=False`.
- **Três itens** (um bom, um com `extra`, um `atualizar` com SEQ fora da cópia local):
  - o livro fica com `simulado`, `erro_definitivo/contrato_invalido` e `erro_definitivo/seq_fora_do_escopo`;
  - o client **nunca** recebe chamada de confirmação (o fake não tem o método; se for chamado, explode);
  - a conexão com o Fire é o fake somente leitura, que registra todo `execute` e falha se o SQL não começar com `SELECT`.
- **Item bom de `criar`:**
  - nome já existe no Fire (o fake devolve uma linha em `SELECT SEQ FROM PRODUTOS WHERE UPPER(TRIM(DESCRICAO)) = ?`) → `erro_definitivo/nome_existe`, com o SEQ na mensagem;
  - componente que não existe no Fire, ou com `BLOQUEADO='Sim'` → `componente_invalido`;
  - os dois casos aparecem no livro.
- **Plano de um item bom:** `plano_json` guarda `{"acao":"criar","tabela":"PRODUTOS","campos":{...colunas do Fire...},"componentes":[...]}`. O mapa wire → coluna é:

| Wire | Coluna do Fire |
|---|---|
| `nome` | DESCRICAO |
| `cor` | DESCRICAO2 |
| `unidadeErp` | UNIDADE |
| `ean` | CODIGO_EAN13 |
| `ncm` | CLASSIF_FISCAL |
| `tipoItemSped` | CODITEMSPED |
| `grupoErp.codigo` | GRUPO |
| `codigoNfe` | CODPARANFE |
| `codigoMes` | CODPROD_ALTERN |

- **Lease:** o mesmo `(cadastroId, versao)` numa segunda chamada não é reprocessado, e o resumo leva `ja_no_livro=1`.
- **Modo `criar`:** devolve o resumo `{"modo":"criar","recusado":"gravação exige P0 e cadastros.v1 (Etapa 3)"}`, sem ler a fila, com ERROR no log.
- **Fire fora** (o fake levanta ao conectar): o resumo leva `erro_transitorio`, nada vai para o livro e não levanta.
- **`rodar()`** com um ambiente que levanta exceção inesperada: loga ERROR e segue para o próximo ambiente, sem levantar.

- [ ] **Step 5: Implementar `cadastro_volta.py`**

**Interruptor:** `modo()` lê `PORTAL_FLOWPCP_CADASTRO_VOLTA`, faz o parse por vírgula e `slug:modo`, sem diferença de maiúscula. Um valor inválido loga ERROR uma vez por valor (use um set de módulo `_avisados`) e devolve `desligado`.

**Validação contra a cópia local:**
- `atualizar`/`inativar` com tipo `material` → o SEQ tem que estar em `materiais_fire_repo.seqs(env_conn)`;
- outros tipos → o SEQ tem que estar em `catalogo_fire`. Leia com `conn.execute("SELECT fire_produto_id FROM catalogo_fire").fetchall()`; não traga o catálogo inteiro para a memória;
- fora disso → `seq_fora_do_escopo`.

**Validação contra o Fire, só com SELECT e parâmetros:**
- `criar`: `SELECT FIRST 1 SEQ FROM PRODUTOS WHERE UPPER(TRIM(DESCRICAO)) = ?`, com `nome.strip().upper()`;
- cada componente: `SELECT SEQ, BLOQUEADO FROM PRODUTOS WHERE SEQ = ?`.

**Conexão com o Fire:** `FirebirdConnection().connect_somente_leitura(environments_repo.to_fb_config(env))`, aberta uma vez por ciclo de ambiente e só se a fila tiver itens.

**Métricas** (em `metrics.py`, no padrão do arquivo):
- `cadastro_volta_total = Counter("portal_flowpcp_cadastro_volta_total", "...", ["env", "resultado"])`;
- `cadastro_volta_pendentes = Gauge("portal_flowpcp_cadastro_volta_pendentes", "...", ["env"])`;
- `cadastro_volta_mais_antigo = Gauge("portal_flowpcp_cadastro_volta_mais_antigo_segundos", "...", ["env"])`, medido pelo `aprovadoEm` mais antigo da fila lida.

**`rodar()`:** para `slug, cfg in enabled_flowpcp_envs().items()`, só os ambientes com `modo != "desligado"`. Usa `try/except Exception` por ambiente. Devolve `{slug: resumo}`. O client vem de `catalogo_sync._build_client(cfg)` e fecha no `finally`.

**Docstring do módulo:** a regra da Etapa 1, numa frase: só lê, nunca grava no Fire nem confirma no Flow, e os modos de escrita recusam iniciar até a Etapa 3.

- [ ] **Step 6: Ciclo.** Em `envio_portal.ciclo`, depois do bloco `catalogo_diario.rodar()`:

```python
    try:
        cadastro_volta.rodar_se_devido(agora)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"flowpcp.envio: volta de cadastro falhou: {exc!r}")
```

Implemente `rodar_se_devido(agora: datetime)` em `cadastro_volta`. Ele chama `rodar()` no máximo a cada `INTERVALO = timedelta(minutes=5)`, guardando a última vez num global de módulo, no mesmo estilo de `_pausa` do `catalogo_diario`. Teste: duas chamadas com 1 min de diferença rodam uma vez só; com 6 min, rodam duas.

- [ ] **Step 7: Rota de leitura.** `GET /{env_id}/flowpcp/cadastro-volta` (admin) devolve:
- `modo` (de `cadastro_volta.modo(slug)`);
- `por_estado` (`cadastro_volta_repo.contar_por_estado`);
- `ultimos` (`cadastro_volta_repo.listar(conn, limite=50)`, sem `plano_json` acima de 2 KB: corte o texto e marque `plano_truncado=true`).

Faz só leitura do SQLite do ambiente. Teste: 404 sem ambiente, 200 com o livro vazio e 200 com 2 linhas.

- [ ] **Step 8: Rodar direcionado + suíte de flowpcp + ruff. Commit.** `feat(cadastro-volta): fila de cadastros do Flow em modo simular (só leitura, sem confirmar)`

---

### Task 4: Tela do ambiente — fiscal, materiais e volta

**Files:**
- Modify: `app/web/static/admin-ambiente-edit.html`
- Test: `tests/test_admin_ambiente_edit_cadastro.py` (novo, guardas de string no estilo de `tests/test_admin_ambiente_edit_catalogo.py`)

**Interfaces:**
- Consumes: `fiscal_portal` na resposta de `sync-catalogo` (Task 1); `POST sync-materiais` (Task 2); `GET cadastro-volta` (Task 3).

- [ ] **Step 1: Leia o arquivo**, principalmente o bloco do catálogo (procure `impactoPoolTotal` e `composicao_portal`) e o padrão dos botões "Simular"/"Gravar" do catálogo e dos clientes. Leia também `tests/test_admin_ambiente_edit_catalogo.py`.

- [ ] **Step 2: Testes de string (falham).** O arquivo HTML precisa conter:
  - `fiscal_portal`, `capacidade_ok`, o texto `O Flow ignorou os campos fiscais`, `com_pendencia` e `por_motivo`;
  - `sync-materiais` e o texto `Simular carga de material`. Não pode conter um botão de gravar material: é dryRun só nesta etapa, então `sync-materiais?apply=1` não aparece no arquivo;
  - `cadastro-volta`, `por_estado` e o texto `Volta do Flow para o Fire`;
  - `textContent` nos blocos novos, e nenhum `innerHTML =` com dado da resposta (copie a forma de verificar do teste existente).

- [ ] **Step 3: Implementar**, seguindo o padrão visual e as funções auxiliares de linha já usadas no bloco do catálogo:
  - **Catálogo:** depois das linhas de composição, a linha `Campos fiscais`, com "N enviados, M com pendência", mais a lista `por_motivo`. Se `capacidade_ok === false`, mostra o aviso `O Flow ignorou os campos fiscais (o Flow ainda não aceita). Tire a empresa de PORTAL_FLOWPCP_CATALOGO_FISCAL.` com a classe de alerta já usada na página.
  - **Material:** uma seção nova "Materiais (Fire → Flow)" com o botão secundário `Simular carga de material` (POST `sync-materiais`). O resultado mostra:
    - `resumo` do Flow (casados por id/código/nome, criar, atualizar, ambíguos, `codigoNomeDiverge`, `unidadeSemMapa`, `exigeAprovacaoDiverge`, `soNoFlow`);
    - `materiais_portal.fora` e as pendências.
  - **Volta:** a seção "Volta do Flow para o Fire" carrega ao abrir a página com `GET cadastro-volta`. Mostra o modo, as contagens por estado e os últimos 10 itens (estado, ação, tipo, código do erro). Se o modo for `desligado`, mostra "Desligada" e nada mais.

- [ ] **Step 4: Harness visual.** Rode o harness em `ui-harness/harness.py` do scratchpad (caminho no ledger) para renderizar a página com respostas fictícias e tirar screenshots a 375 e 1280 px. Confira:
  - texto que não estoura;
  - foco visível no botão novo;
  - números com `tabular-nums` (a classe já usada no bloco do catálogo).

  Corrija o que aparecer.

- [ ] **Step 5: Testes + commit.** `feat(admin): blocos de fiscal, materiais e volta na tela do ambiente`

---

### Task 5: Docs e changelog

**Files:**
- Modify:
  - `docs/ai/modules/flowpcp.md`: um parágrafo novo em §4 para fiscal e materiais, e uma seção curta "§5 Volta de cadastro (Flow → Fire)";
  - a tabela de rotas admin, com `sync-materiais` e `cadastro-volta`;
  - a tabela de configuração, com as variáveis novas;
  - `docs/ai/modules/persistence.md`: as tabelas `materiais_fire` e `cadastro_volta`;
  - `CHANGELOG.md`: um item em `## Não publicado` por task, uma frase para a operação cada;
  - a spec §6.2: a frase do recorte de bloqueados passa a ser "vão todos com `ativo=false`; o Flow não cria insumo inativo novo".

- [ ] **Step 1:** Editar só as seções afetadas (regra do CLAUDE.md do repo: doc incremental).
- [ ] **Step 2:** Suíte completa: `... -m pytest tests -q` e `ruff check app tests`. Registrar no ledger o número de testes antes e depois.
- [ ] **Step 3: Commit.** `docs(flowpcp): fiscal, materiais e volta de cadastro (Etapa 1)`
