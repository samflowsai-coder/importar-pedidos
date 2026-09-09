# Roteamento intercompany Nasmar → MM — Fases 0 a 1c — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Portal deixa de depender do operador para saber em que empresa o pedido
entra: ele lê o fornecedor no próprio documento, cai para o histórico do Fire quando o
documento é mudo, e só pergunta quando as duas fontes calam — tudo atrás de um
interruptor de três estados que permite rodar em observação antes de valer.

**Architecture:** Quatro camadas independentes. (1) O mapper do Firebird passa a escrever
todas as colunas que um pedido faturável precisa — hoje escreve 9 de 98 e nunca rodou em
produção. (2) O pipeline passa a marcar no `Order` qual CNPJ de ambiente aparece no
documento, e um roteador puro resolve o ambiente numa escada estrita de quatro degraus:
documento > histórico > memória > perguntar. (3) Um interruptor global (`desligado` /
`observando` / `ligado`) decide se a resposta do roteador vale, é só registrada, ou nem é
calculada. (4) Com o roteamento valendo, a seleção de ambiente no login perde a razão de
existir e vira filtro.

**Tech Stack:** Python 3.11+, pydantic v2, SQLite (`app_shared.db` + `app_state_<slug>.db`),
firebird-driver, FastAPI, APScheduler, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`](../specs/2026-08-24-roteamento-intercompany-nasmar-design.md) — **Revisão 4** (2026-09-09)

**Substitui:** [`2026-08-25-roteamento-intercompany-nasmar.md`](2026-08-25-roteamento-intercompany-nasmar.md),
escrito contra a Revisão 2. As Tasks 1 a 3 daquele plano (Fase 0) vieram para cá
inalteradas — a Revisão 4 não tocou no mapper. As Tasks 4 a 6 morreram junto com a
tabela `rota_intercompany`. As Tasks 7 a 12 (lote) continuam válidas como desenho e
serão a base do plano da Fase 2.

**Escopo deste plano:** Fases **0, 1, 1a, 1c e 1b** da spec.

- A **Fase 2** (consolidador, lote, perna espelho, `/lotes`) ganha plano próprio. Ela
  depende de uma resposta comercial que ainda não temos — *"a Nasmar revende alguma linha
  que ela não compra da MM?"* (spec, "Questões abertas") — e nada nela é pré-requisito do
  que está aqui.
- A **Fase 3** (perna de volta no FlowPCP) depende do `pcp-app`, outro repositório.

**Ordem das fases neste plano difere da tabela da spec, de propósito:** `1c` (o
interruptor) vem **antes** de `1b` (fim da seleção de ambiente). Duas razões, e as duas
são de dependência, não de preferência. Primeiro: o wiring do roteamento não pode entrar
sem o interruptor, senão o roteamento nasce ligado em produção — por isso o interruptor
aparece já na Fase 1 (Task 8), e a Fase 1c só acrescenta a tela. Segundo: tirar a seleção
de ambiente do login só faz sentido depois que o roteamento estiver em `ligado`, e quem
decide isso é a taxa de acerto que a Fase 1c mede.

---

## Global Constraints

- **Python 3.11+** — `X | Y` e `match` liberados (`pyproject.toml: requires-python = ">=3.11"`).
- **Toda mutação de `portal_status`/`production_status` passa por `app.state.transition()`.** Nunca atribuir direto (`state.md`, "Armadilhas").
- **Tabelas transversais usam `db.connect_shared()`** (ou `router.shared_connect()`); tabelas operacionais de pedido usam `db.connect()` (`environments.md`, "connect() vs connect_shared()").
- **`environment_id` em `imports` é bind imutável** — populado no INSERT, jamais em UPDATE. Corolário que manda em toda a Fase 1: **a decisão de roteamento acontece antes do INSERT, e escrever no ambiente decidido exige `env_context.active_env(...)` em volta do bloco de persistência** — só setar `entry["environment_id"]` grava a linha na DB errada, porque `imports` vive em `app_state_<slug>.db`.
- **Normalização de CNPJ é uma só:** `app/erp/cnpj.py::cnpj_digits`. Não duplicar regex de CNPJ em módulo novo.
- **Cálculo de preço em `Decimal`, nunca `float`.** `OrderItem.unit_price` é `float` (`app/models/order.py:20`); converter na borda, quantizar explicitamente antes de gravar.
- **Charset do Firebird é `WIN1252`.** Flags booleanas são strings `'Sim'`/`'Nao'`. `STATUS` inicial é `'PEDIDO'`.
- **Nunca rodar script de escrita contra `.fdb` de produção.** Validação de Firebird usa cópia.
- **O default de instalação nova é `roteamento_modo = 'desligado'`**, e `'desligado'` tem que se comportar **exatamente** como o Portal de hoje. Isso é testado, não presumido.
- **Lint antes de dar por pronto:** `ruff check app/ tests/` e `ruff format app/ tests/`.
- **Suíte completa antes do commit final:** `.venv/bin/pytest tests/ -v`.

---

## Decisão de desenho que este plano toma além da spec

A spec diz *"`supplier_cnpj` nos 11 parsers"*. Este plano implementa o degrau do documento
em **um lugar só, o `app/pipeline.py`**, e deixa o campo aberto para os parsers refinarem
depois. A razão é que o fato 14 foi medido exatamente assim — *"rodei a extração de texto
nos 29 arquivos procurando os dois CNPJs"* — e a propriedade que torna o degrau seguro
(**zero arquivos com os dois CNPJs**) é uma propriedade da varredura de texto, não da
leitura de rótulo. Onze parsers editados entregariam a mesma cobertura de 21/29 com onze
vezes o risco de regressão.

`OrderHeader.supplier_cnpj` continua existindo como contrato do modelo, e continua sendo o
degrau 1: quando um parser souber ler o rótulo `FORNECEDOR` e preencher o campo, o valor
dele **ganha** da varredura. O que muda é que a feature não espera por isso para funcionar.

Consequência que a Task 5 tranca em teste: a varredura só responde quando encontra
**exatamente um** CNPJ de ambiente conhecido no texto. Dois CNPJs no mesmo documento =
sem resposta, cai para o degrau seguinte. Nunca "o primeiro que apareceu".

---

## File Structure

**Criados:**

| Arquivo | Responsabilidade |
|---|---|
| `app/erp/fiscal.py` | Perfil fiscal por ambiente — as constantes que o Fire exige e o mapper não escrevia |
| `app/routing/__init__.py` | Pacote novo |
| `app/routing/documento.py` | Degrau 1: varredura do texto por CNPJ de ambiente. Puro |
| `app/routing/historico.py` | Degrau 2: onde este cliente já comprou, nos dois Firebird, na janela de 12 meses |
| `app/routing/ambiente.py` | A escada: documento > histórico > memória > perguntar. Puro, deps injetadas |
| `app/persistence/decisao_ambiente_repo.py` | Degrau 3: memória das escolhas do operador (shared) |
| `app/persistence/roteamento_repo.py` | Interruptor, tabela sombra, fila de pendências (shared) |
| `app/web/routes_roteamento.py` | `/api/roteamento/*` — modo, taxa de acerto, pendências |
| `app/web/static/admin-roteamento.html` | Tela do interruptor + taxa de acerto |
| `tests/test_erp_fiscal.py`, `tests/test_erp_mapper_colunas.py` | Fase 0 |
| `tests/test_routing_documento.py`, `tests/test_routing_historico.py`, `tests/test_routing_ambiente.py`, `tests/test_routing_modo.py`, `tests/test_supplier_cnpj_samples.py` | Fase 1 |
| `tests/test_decisao_ambiente_repo.py`, `tests/test_roteamento_repo.py` | Fase 1 |
| `tests/test_routing_wiring.py`, `tests/test_scan_environments_roteamento.py` | Fase 1 |
| `tests/test_imports_cross_env.py` | Fase 1b |

**Modificados:**

| Arquivo | O quê |
|---|---|
| `app/persistence/schema_shared.py` | `environments.cnpj` + colunas fiscais; tabelas `decisao_ambiente`, `roteamento_modo`, `roteamento_sombra`, `roteamento_pendencia` |
| `app/persistence/environments_repo.py` | `cnpj` no CRUD e no public view; `find_by_cnpj()` |
| `app/persistence/repo.py` | `list_imports_all_envs()` / `count_imports_all_envs()` (Fase 1b) |
| `app/erp/queries.py` | `INSERT_CAB_VENDAS` e `INSERT_CORPO_VENDAS` completos; `COUNT_PEDIDOS_CLIENTE_DESDE` |
| `app/erp/mapper.py` | Escrever as colunas que faltam; `UNID` do cadastro |
| `app/exporters/firebird_exporter.py` | Passar perfil fiscal; devolver o `CODIGO` gerado |
| `app/models/order.py` | `OrderHeader.supplier_cnpj` |
| `app/pipeline.py` | Varredura do fornecedor depois do parser, antes do normalizer |
| `app/parsers/desmembramento_xls_parser.py` | `customer_cnpj` derivado das colunas de loja (Fase 1a) |
| `app/web/server.py` | Roteamento no `/api/commit`; decisão no payload do preview; `/admin/roteamento`; index sem redirect quando `ligado` |
| `app/web/middleware/environment.py` | Cookie deixa de ser gate quando `ligado` (Fase 1b) |
| `app/worker/jobs/scan_environments.py` | Roteamento no watcher + retenção do que não resolve |
| `app/web/static/js/shell.js`, `index.html` | Selo do ambiente na listagem; item de menu |
| `docs/ai/modules/erp.md`, `environments.md`, `pipeline.md`, `models.md` | Seções afetadas |

---

# FASE 0 — Mapper completo

Pré-requisito de tudo. O caminho `EXPORT_MODE=db` nunca rodou em produção (spec, fato 1):
zero `ULT_INS_USER='IMPORTADOR'` nos dois bancos em 12 meses.

---

### Task 1: Perfil fiscal por ambiente

**Files:**
- Create: `app/erp/fiscal.py`
- Modify: `app/persistence/schema_shared.py` (COLUMN_MIGRATIONS, fim do arquivo)
- Test: `tests/test_erp_fiscal.py`

**Interfaces:**
- Produces: `PerfilFiscal` (frozen dataclass) com campos `codfigfiscal: int`,
  `tipo_cob: int`, `cod_class_finan: int`, `desc_class_finan: str`, `classif_fat: int`,
  `mecanico: int`, `icms_porc: Decimal`, `reducao: Decimal`, `cfop_principal: str`;
  `perfil_para(env: dict | None) -> PerfilFiscal`.

**Contexto:** os valores default vêm de medição na Fire viva em 2026-08-24 — 373 pedidos
do `.7` e 90 do `.4`, todos de 2026-06-01 em diante. `CODFIGFISCAL` é o único que difere
entre as empresas: `1` na MM Americanense, `5` na Nasmar.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_erp_fiscal.py
from __future__ import annotations

from decimal import Decimal

from app.erp.fiscal import PerfilFiscal, perfil_para


def test_perfil_default_usa_valores_da_mm():
    """Sem env, cai no perfil da MM Americanense (medido em producao)."""
    p = perfil_para(None)
    assert p.codfigfiscal == 1
    assert p.tipo_cob == 4
    assert p.cod_class_finan == 335
    assert p.desc_class_finan == "Venda de Produtos"
    assert p.classif_fat == 1
    assert p.mecanico == 99
    assert p.icms_porc == Decimal("18")
    assert p.reducao == Decimal("61.11")
    assert p.cfop_principal == "5.101"


def test_perfil_le_codfigfiscal_do_ambiente():
    """A Nasmar usa figura fiscal 5; a MM usa 1. E a unica que difere."""
    p = perfil_para({"fiscal_codfigfiscal": 5})
    assert p.codfigfiscal == 5
    assert p.tipo_cob == 4  # os demais seguem o default


def test_perfil_ignora_campo_vazio():
    """Coluna NULL no ambiente nao zera o default — cai no valor medido."""
    p = perfil_para({"fiscal_codfigfiscal": None, "fiscal_mecanico": None})
    assert p.codfigfiscal == 1
    assert p.mecanico == 99


def test_perfil_e_imutavel():
    p = perfil_para(None)
    with __import__("pytest").raises(Exception):
        p.codfigfiscal = 9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_erp_fiscal.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.erp.fiscal'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/erp/fiscal.py
"""Perfil fiscal por ambiente: as constantes que o Fire exige num pedido
faturavel e que o mapper nao escrevia.

Todos os defaults vem de medicao na Fire viva (2026-08-24): 373 pedidos do
.7 (MM Americanense) e 90 do .4 (Nasmar), de 2026-06-01 em diante. Sao os
valores presentes em 82% a 100% dos pedidos digitados pela operacao.

CODFIGFISCAL e o unico que difere entre as empresas (1 na MM, 5 na Nasmar),
por isso mora em `environments` e nao aqui.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class PerfilFiscal:
    codfigfiscal: int
    tipo_cob: int
    cod_class_finan: int
    desc_class_finan: str
    classif_fat: int
    mecanico: int
    icms_porc: Decimal
    reducao: Decimal
    cfop_principal: str


_DEFAULTS = PerfilFiscal(
    codfigfiscal=1,
    tipo_cob=4,
    cod_class_finan=335,
    desc_class_finan="Venda de Produtos",
    classif_fat=1,
    mecanico=99,
    icms_porc=Decimal("18"),
    reducao=Decimal("61.11"),
    cfop_principal="5.101",
)


def perfil_para(env: dict | None) -> PerfilFiscal:
    """Perfil do ambiente. Campo ausente ou NULL cai no default medido."""
    if not env:
        return _DEFAULTS
    codfig = env.get("fiscal_codfigfiscal")
    if codfig in (None, ""):
        return _DEFAULTS
    return PerfilFiscal(
        codfigfiscal=int(codfig),
        tipo_cob=_DEFAULTS.tipo_cob,
        cod_class_finan=_DEFAULTS.cod_class_finan,
        desc_class_finan=_DEFAULTS.desc_class_finan,
        classif_fat=_DEFAULTS.classif_fat,
        mecanico=_DEFAULTS.mecanico,
        icms_porc=_DEFAULTS.icms_porc,
        reducao=_DEFAULTS.reducao,
        cfop_principal=_DEFAULTS.cfop_principal,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_erp_fiscal.py -v`
Expected: 4 passed

- [ ] **Step 5: Add the environments column**

Append to `COLUMN_MIGRATIONS` at the end of `app/persistence/schema_shared.py`:

```python
    # Figura fiscal do ambiente. Medido na Fire viva: 1 na MM Americanense,
    # 5 na Nasmar. NULL = usa o default de app/erp/fiscal.py.
    ("environments", "fiscal_codfigfiscal",
     "ALTER TABLE environments ADD COLUMN fiscal_codfigfiscal INTEGER"),
```

Add the same column to the `CREATE TABLE IF NOT EXISTS environments` block (after
`flowpcp_clientes_push`, before `created_at`):

```sql
    fiscal_codfigfiscal       INTEGER,
```

- [ ] **Step 6: Verify the migration is idempotent**

Run: `.venv/bin/pytest tests/test_environments_repo.py tests/test_persistence_router.py -v`
Expected: PASS — a migration roda duas vezes sem erro (o mecanismo já checa a coluna).

- [ ] **Step 7: Lint and commit**

```bash
ruff check app/erp/fiscal.py tests/test_erp_fiscal.py app/persistence/schema_shared.py
ruff format app/erp/fiscal.py tests/test_erp_fiscal.py
git add app/erp/fiscal.py tests/test_erp_fiscal.py app/persistence/schema_shared.py
git commit -m "feat(erp): perfil fiscal por ambiente (CODFIGFISCAL 1=MM, 5=Nasmar)"
```

---

### Task 2: `CAB_VENDAS` completo

**Files:**
- Modify: `app/erp/queries.py:203` (INSERT_CAB_VENDAS)
- Modify: `app/erp/mapper.py:50-77` (`order_to_cabvendas`)
- Test: `tests/test_erp_mapper_colunas.py`

**Interfaces:**
- Consumes: `PerfilFiscal`, `perfil_para` (Task 1).
- Produces: `FireSistemasMapper.order_to_cabvendas(order, header_pk, client_id, *, perfil: PerfilFiscal, valor_total: Decimal, obs: str | None = None, dt_entrega: date | None = None) -> tuple` — tupla posicional de **23** elementos, na ordem exata de `INSERT_CAB_VENDAS`.

**Contexto:** a assinatura atual (`mapper.py:50`) devolve 9 elementos e deixa `OBS` e
`DT_ENTREGA` sempre `None` (`mapper.py:74-75`). As colunas abaixo estão preenchidas em
**100%** dos 373 pedidos medidos no `.7`, e um pedido sem elas provavelmente não fatura.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_erp_mapper_colunas.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.erp.fiscal import perfil_para
from app.erp.mapper import FireSistemasMapper
from app.models.order import Order, OrderHeader, OrderItem

# Ordem posicional de INSERT_CAB_VENDAS apos esta task.
CAB = [
    "CODIGO", "CODEMPRESA", "DATA_PEDIDO", "CLIENTE", "STATUS", "PEDIDO_CLIENTE",
    "OBS", "DT_ENTREGA", "DT_BASE_FAT", "VALOR_TOTAL", "TOTAL_PRODUTO", "DESCONTO",
    "TIPO_COB", "COD_CLASS_FINAN", "DESC_CLASS_FINAN", "CLASSIF_FAT", "CODFIGFISCAL",
    "MECANICO", "SEM_IMP", "PED_ZF", "EH_VENDACONSUMIDOR", "VENDEDOR_COMI",
    "ULT_INS_USER",
]


def _pedido():
    return Order(
        header=OrderHeader(
            order_number="OC-70610",
            issue_date="24/08/2026",
            customer_name="DAJU LTDA",
            customer_cnpj="76.917.624/0004-82",
        ),
        items=[OrderItem(description="KIT", quantity=300.0, unit_price=16.12)],
    )


def _campos(t):
    assert len(t) == len(CAB), f"esperado {len(CAB)} colunas, veio {len(t)}"
    return dict(zip(CAB, t))


def test_cabvendas_preenche_colunas_de_100_porcento():
    """As colunas presentes em 100% dos pedidos digitados nao podem sair NULL."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(), header_pk=4676, client_id=2,
        perfil=perfil_para(None), valor_total=Decimal("71899.08"),
    )
    c = _campos(t)
    assert c["VALOR_TOTAL"] == Decimal("71899.08")
    assert c["TOTAL_PRODUTO"] == Decimal("71899.08")
    assert c["DESCONTO"] == Decimal("0")
    assert c["TIPO_COB"] == 4
    assert c["COD_CLASS_FINAN"] == 335
    assert c["DESC_CLASS_FINAN"] == "Venda de Produtos"
    assert c["SEM_IMP"] == "Nao"
    assert c["PED_ZF"] == "Nao"
    assert c["EH_VENDACONSUMIDOR"] == "Nao"
    assert c["VENDEDOR_COMI"] == Decimal("0")
    assert c["MECANICO"] == 99
    assert c["CLASSIF_FAT"] == 1
    assert c["ULT_INS_USER"] == "IMPORTADOR"


def test_cabvendas_usa_codfigfiscal_do_perfil():
    """Nasmar (5) e MM (1) escrevem figura fiscal diferente."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(), header_pk=1, client_id=2,
        perfil=perfil_para({"fiscal_codfigfiscal": 5}), valor_total=Decimal("10"),
    )
    assert _campos(t)["CODFIGFISCAL"] == 5


def test_cabvendas_grava_obs_e_dt_entrega():
    """Hoje mapper.py:74-75 crava None nos dois. O lote depende dos dois."""
    t = FireSistemasMapper().order_to_cabvendas(
        _pedido(), header_pk=1, client_id=2, perfil=perfil_para(None),
        valor_total=Decimal("10"), obs="LOTE NAS-2026-S34 | PEDIDOS .4: 1157, 1158",
        dt_entrega=date(2026, 10, 8),
    )
    c = _campos(t)
    assert c["OBS"] == "LOTE NAS-2026-S34 | PEDIDOS .4: 1157, 1158"
    assert c["DT_ENTREGA"] == date(2026, 10, 8)
    assert c["DT_BASE_FAT"] == date(2026, 10, 8)


def test_cabvendas_codped_pai_nao_esta_no_insert():
    """CODPED_PAI e auto-referencia (=CODIGO). Vai por UPDATE pos-insert,
    nao pela tupla — senao o valor teria que ser conhecido antes do PK."""
    assert "CODPED_PAI" not in CAB
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_erp_mapper_colunas.py -v`
Expected: FAIL — `TypeError: order_to_cabvendas() got an unexpected keyword argument 'perfil'`

- [ ] **Step 3: Rewrite INSERT_CAB_VENDAS**

Replace the `INSERT_CAB_VENDAS` block in `app/erp/queries.py` (starts at line 203):

```python
# Insert sales order header (CAB_VENDAS).
#
# Colunas e valores conferidos contra 373 pedidos do .7 e 90 do .4 na Fire
# viva (2026-08-24, todos de 2026-06-01 em diante). As 14 primeiras estao
# preenchidas em 100% dos pedidos digitados pela operacao; CLASSIF_FAT,
# CODFIGFISCAL, DT_BASE_FAT e MECANICO em 82% a 100%.
#
# CODPED_PAI (auto-referencia = CODIGO) NAO entra aqui: o valor so existe
# depois do INSERT. Vai por UPDATE_CODPED_PAI logo em seguida, na mesma
# transacao.
INSERT_CAB_VENDAS = """
    INSERT INTO CAB_VENDAS (
        CODIGO, CODEMPRESA, DATA_PEDIDO,
        CLIENTE, STATUS, PEDIDO_CLIENTE,
        OBS, DT_ENTREGA, DT_BASE_FAT,
        VALOR_TOTAL, TOTAL_PRODUTO, DESCONTO,
        TIPO_COB, COD_CLASS_FINAN, DESC_CLASS_FINAN,
        CLASSIF_FAT, CODFIGFISCAL, MECANICO,
        SEM_IMP, PED_ZF, EH_VENDACONSUMIDOR, VENDEDOR_COMI,
        ULT_INS_USER, ULT_ALT_USER,
        ULT_INS_DTHR, ULT_ALT_DTHR, DTHORA_PEDIDO
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?,
        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    )
"""

# CODPED_PAI aponta pro proprio pedido em 100% dos pedidos medidos.
UPDATE_CODPED_PAI = """
    UPDATE CAB_VENDAS SET CODPED_PAI = CODIGO WHERE CODIGO = ?
"""
```

**Contagem de binds — confira antes de seguir.** O SQL tem **24** placeholders; o mapper
devolve **23**. A diferença é `ULT_ALT_USER`, que repete `ULT_INS_USER`: o exporter
executa `cur.execute(INSERT_CAB_VENDAS, (*tupla, tupla[-1]))`. O mapper não duplica
porque a tupla é o contrato testável, e um valor repetido nela esconderia um erro de
ordem. Os três `CURRENT_TIMESTAMP` finais não consomem placeholder.

- [ ] **Step 4: Rewrite `order_to_cabvendas`**

Replace `order_to_cabvendas` in `app/erp/mapper.py` (lines 50-77):

```python
    def order_to_cabvendas(
        self,
        order: Order,
        header_pk: int,
        client_id: int,
        *,
        perfil: PerfilFiscal,
        valor_total: Decimal,
        obs: str | None = None,
        dt_entrega: date | None = None,
    ) -> tuple:
        """Tupla posicional para INSERT_CAB_VENDAS (23 elementos).

        `valor_total` vem calculado de fora (soma dos itens) porque o mapper
        nao conhece o resultado do de-para de produto nem do fator de preco.

        `obs` e `dt_entrega` eram cravados em None ate 2026-08. O lote
        intercompany depende dos dois: OBS carrega a lista de pedidos de
        origem, DT_ENTREGA a menor data das pernas.
        """
        import os

        empresa = int(os.environ.get("FB_CODEMPRESA", self.EMPRESA_CODIGO))
        pedido_cliente = (order.header.order_number or "")[:20] or None
        data_pedido = _parse_date(order.header.issue_date) or date.today()

        return (
            header_pk,                    # CODIGO
            empresa,                      # CODEMPRESA
            data_pedido,                  # DATA_PEDIDO
            client_id,                    # CLIENTE
            self.STATUS_INICIAL,          # STATUS = 'PEDIDO'
            pedido_cliente,               # PEDIDO_CLIENTE
            obs,                          # OBS
            dt_entrega,                   # DT_ENTREGA
            dt_entrega,                   # DT_BASE_FAT (= DT_ENTREGA em 100% dos medidos)
            valor_total,                  # VALOR_TOTAL
            valor_total,                  # TOTAL_PRODUTO
            Decimal("0"),                 # DESCONTO
            perfil.tipo_cob,              # TIPO_COB
            perfil.cod_class_finan,       # COD_CLASS_FINAN
            perfil.desc_class_finan,      # DESC_CLASS_FINAN
            perfil.classif_fat,           # CLASSIF_FAT
            perfil.codfigfiscal,          # CODFIGFISCAL
            perfil.mecanico,              # MECANICO
            "Nao",                        # SEM_IMP
            "Nao",                        # PED_ZF
            "Nao",                        # EH_VENDACONSUMIDOR
            Decimal("0"),                 # VENDEDOR_COMI
            self.USUARIO_SISTEMA,         # ULT_INS_USER
        )
```

Add to the imports at the top of `app/erp/mapper.py`:

```python
from decimal import Decimal

from app.erp.fiscal import PerfilFiscal
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_erp_mapper_colunas.py -v`
Expected: 4 passed

- [ ] **Step 6: Fix the exporter call-site**

`app/exporters/firebird_exporter.py` calls `order_to_cabvendas`. Update the call to pass
the new kwargs and to duplicate the last element for `ULT_ALT_USER`, then run
`UPDATE_CODPED_PAI` in the same transaction. Run the existing exporter tests:

Run: `.venv/bin/pytest tests/test_smoke_erp_mapper.py tests/test_firebird_exporter_override.py -v`
Expected: PASS

- [ ] **Step 7: Lint and commit**

```bash
ruff check app/erp/ tests/test_erp_mapper_colunas.py
ruff format app/erp/ tests/test_erp_mapper_colunas.py
git add app/erp/queries.py app/erp/mapper.py app/exporters/firebird_exporter.py tests/test_erp_mapper_colunas.py
git commit -m "feat(erp): CAB_VENDAS completo (23 colunas) + OBS e DT_ENTREGA"
```

---

### Task 3: `CORPO_VENDAS` completo e `UNID` do cadastro

**Files:**
- Modify: `app/erp/queries.py` (INSERT_CORPO_VENDAS)
- Modify: `app/erp/mapper.py:79-103` (`item_to_corpovendas`)
- Test: `tests/test_erp_mapper_colunas.py` (append)

**Interfaces:**
- Consumes: `PerfilFiscal` (Task 1).
- Produces: `FireSistemasMapper.item_to_corpovendas(item, item_pk, header_pk, product_seq, *, perfil: PerfilFiscal, unid: str = "UN") -> tuple` — 15 elementos.

**Contexto:** medição de 1.285 linhas de item no `.7`. `ICMS_PORC`, `ICMS_BASE`,
`REDUCAO`, `DESC_SOBRE_TOTAL`, `PESO_BRUTO`, `PESO_LIQUIDO` em 100%; `CFOP_PRINCIPAL` em
96%. **Bug:** `mapper.py:101` crava `UNID = "UN"` e a produção usa `'KIT'` nos kits.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_erp_mapper_colunas.py

CORPO = [
    "CODIGO", "CODVENDA", "CODPRODUTO", "DESCRICAO", "QTD", "PRECO_UNITARIO",
    "TOTAL", "UNID", "DT_ENTREGA_ITEM", "ICMS_PORC", "ICMS_BASE", "REDUCAO",
    "DESC_SOBRE_TOTAL", "PESO_BRUTO", "PESO_LIQUIDO",
]


def _item_campos(t):
    assert len(t) == len(CORPO), f"esperado {len(CORPO)} colunas, veio {len(t)}"
    return dict(zip(CORPO, t))


def test_corpovendas_preenche_colunas_fiscais():
    from app.models.order import ERPRow

    row = ERPRow(pedido="OC-70610", descricao="KIT C/3", quantidade=300.0,
                 preco_unitario=15.0654, data_entrega="08/10/2026")
    t = FireSistemasMapper().item_to_corpovendas(
        row, item_pk=1, header_pk=4676, product_seq=3905, perfil=perfil_para(None)
    )
    c = _item_campos(t)
    assert c["ICMS_PORC"] == Decimal("18")
    assert c["REDUCAO"] == Decimal("61.11")
    assert c["ICMS_BASE"] == Decimal("0")
    assert c["DESC_SOBRE_TOTAL"] == Decimal("0")
    assert c["PESO_BRUTO"] == Decimal("0")
    assert c["PESO_LIQUIDO"] == Decimal("0")


def test_corpovendas_unid_vem_do_cadastro_nao_cravado():
    """mapper.py:101 cravava 'UN'. A producao usa 'KIT' nos kits."""
    from app.models.order import ERPRow

    row = ERPRow(pedido="X", descricao="KIT C/3", quantidade=1.0, preco_unitario=1.0)
    m = FireSistemasMapper()
    assert _item_campos(m.item_to_corpovendas(
        row, 1, 1, None, perfil=perfil_para(None), unid="KIT"))["UNID"] == "KIT"
    assert _item_campos(m.item_to_corpovendas(
        row, 1, 1, None, perfil=perfil_para(None)))["UNID"] == "UN"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_erp_mapper_colunas.py -k corpovendas -v`
Expected: FAIL — `TypeError: item_to_corpovendas() got an unexpected keyword argument 'perfil'`

- [ ] **Step 3: Rewrite INSERT_CORPO_VENDAS**

Replace the block in `app/erp/queries.py`:

```python
# Insert order item (CORPO_VENDAS).
#
# Conferido contra 1.285 linhas de item no .7 (2026-08). ICMS_PORC, ICMS_BASE,
# REDUCAO, DESC_SOBRE_TOTAL, PESO_BRUTO e PESO_LIQUIDO estao em 100% das linhas;
# CFOP_PRINCIPAL em 96%.
INSERT_CORPO_VENDAS = """
    INSERT INTO CORPO_VENDAS (
        CODIGO, CODVENDA, CODPRODUTO,
        DESCRICAO, QTD, PRECO_UNITARIO, TOTAL,
        UNID, DT_ENTREGA_ITEM,
        ICMS_PORC, ICMS_BASE, REDUCAO,
        DESC_SOBRE_TOTAL, PESO_BRUTO, PESO_LIQUIDO,
        CFOP_PRINCIPAL
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?
    )
"""
```

- [ ] **Step 4: Rewrite `item_to_corpovendas`**

```python
    def item_to_corpovendas(
        self,
        item: ERPRow,
        item_pk: int,
        header_pk: int,
        product_seq: int | None,
        *,
        perfil: PerfilFiscal,
        unid: str = "UN",
    ) -> tuple:
        """Tupla posicional para INSERT_CORPO_VENDAS (15 elementos).

        `unid` vem do cadastro do produto no Fire. Ate 2026-08 era cravado
        "UN" aqui, o que estava errado para kits ("KIT" na producao).
        CFOP_PRINCIPAL e passado pelo exporter junto com o perfil.
        """
        qty = item.quantidade or 0.0
        unit_price = item.preco_unitario or 0.0
        total = item.valor_total if item.valor_total is not None else round(qty * unit_price, 4)
        desc = (item.descricao or "")[:100]
        delivery = _parse_date(item.data_entrega)

        return (
            item_pk,               # CODIGO
            header_pk,             # CODVENDA
            product_seq,           # CODPRODUTO
            desc,                  # DESCRICAO
            qty,                   # QTD
            unit_price,            # PRECO_UNITARIO
            total,                 # TOTAL
            unid,                  # UNID
            delivery,              # DT_ENTREGA_ITEM
            perfil.icms_porc,      # ICMS_PORC
            Decimal("0"),          # ICMS_BASE
            perfil.reducao,        # REDUCAO
            Decimal("0"),          # DESC_SOBRE_TOTAL
            Decimal("0"),          # PESO_BRUTO
            Decimal("0"),          # PESO_LIQUIDO
        )
```

The exporter appends `perfil.cfop_principal` as the 16th bind value.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_erp_mapper_colunas.py -v`
Expected: 6 passed

- [ ] **Step 6: Wire `unid` from the product catalog**

In `app/exporters/firebird_exporter.py`, the product lookup already returns the Fire
product row. Pass its `UNID` (falling back to `"UN"`) into `item_to_corpovendas`.

Run: `.venv/bin/pytest tests/test_smoke_erp_mapper.py tests/test_product_check.py -v`
Expected: PASS

- [ ] **Step 7: Validate against a copy of the .fdb**

Manual gate — **not automated, and not optional** (`erp.md`, "Testes"). Restore a copy of
`MM_AMERICANENSE.FDB`, insert one order via the Portal with `EXPORT_MODE=db`, then compare
column by column against a real order inserted by the operation (e.g. `CODIGO=4676`).
Record the diff in the PR description. **Never run against production.**

- [ ] **Step 8: Lint, full suite, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/erp/ app/exporters/firebird_exporter.py tests/test_erp_mapper_colunas.py
git commit -m "feat(erp): CORPO_VENDAS completo + UNID do cadastro (era 'UN' cravado)"
```

---

# FASE 1 — Roteamento pelo documento

Entrega valor sozinha: acaba o pedido da Nasmar caindo no ambiente da MM. Não escreve nada
novo no Fire — é decisão de destino, não de conteúdo.

**Ordem interna que importa:** o campo e os degraus primeiro (Tasks 4 a 7), o interruptor
antes do wiring (Task 8), o wiring por último (Tasks 10 e 11). Nunca ligar o wiring sem o
interruptor: `desligado` é o default, e é ele que garante que a Fase 1 pode ser mergeada e
deployada sem mudar nada para o operador.

---

### Task 4: CNPJ no cadastro do ambiente

O roteamento inteiro pendura em `supplier_cnpj → environments.cnpj → ambiente`. A coluna
não existe hoje: `environments` tem slug, nome, pastas e Firebird, mas não sabe qual é o
CNPJ da empresa que ela representa.

**Files:**
- Modify: `app/persistence/schema_shared.py` (TABLES_SQL, COLUMN_MIGRATIONS)
- Modify: `app/persistence/environments_repo.py` (`_PUBLIC_FIELDS`, `create`, `update`, novo `find_by_cnpj`)
- Modify: `app/web/static/admin-ambiente-edit.html` (campo no formulário)
- Modify: `app/web/routes_environments.py` (aceitar `cnpj` no payload)
- Test: `tests/test_environments_repo.py` (estender)

**Interfaces:**
- Produces: `environments_repo.find_by_cnpj(cnpj: str) -> dict | None` — casa por dígitos,
  só ambientes com `is_active = 1`. `environments_repo.create(..., cnpj: str | None = None)`
  e `update(..., cnpj: str | None = None)`. O valor é **sempre gravado em dígitos**
  (`cnpj_digits`), nunca formatado — a comparação tem que ser byte a byte.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_environments_repo.py
from app.erp.cnpj import cnpj_digits


def test_cnpj_e_gravado_em_digitos(fresh_shared):
    """Admin digita formatado; o banco guarda so os digitos."""
    env = environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="34.513.679/0001-34",
    )
    assert env["cnpj"] == "34513679000134"


def test_find_by_cnpj_casa_formatado_ou_nao(fresh_shared):
    environments_repo.create(
        slug="mm", name="MM Americanense",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    achado = environments_repo.find_by_cnpj("35.394.871/0001-11")
    assert achado is not None
    assert achado["slug"] == "mm"
    assert environments_repo.find_by_cnpj("00000000000000") is None
    assert environments_repo.find_by_cnpj("") is None
    assert environments_repo.find_by_cnpj(None) is None


def test_find_by_cnpj_ignora_ambiente_inativo(fresh_shared):
    """Ambiente desativado nao roteia nada — some do lookup."""
    env = environments_repo.create(
        slug="velho", name="Empresa antiga",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="11222333000144",
    )
    environments_repo.soft_delete(env["id"])
    assert environments_repo.find_by_cnpj("11222333000144") is None


def test_update_limpa_cnpj_com_string_vazia(fresh_shared):
    """None mantem, "" limpa — mesma semantica de fb_password."""
    env = environments_repo.create(
        slug="mm2", name="MM",
        watch_dir="/x", output_dir="/y", fb_path="/z.fdb",
        cnpj="35394871000111",
    )
    assert environments_repo.update(env["id"], name="MM 2")["cnpj"] == "35394871000111"
    assert environments_repo.update(env["id"], cnpj="")["cnpj"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_environments_repo.py -v -k cnpj`
Expected: FAIL — `TypeError: create() got an unexpected keyword argument 'cnpj'`

- [ ] **Step 3: Write minimal implementation**

Em `app/persistence/schema_shared.py`, dentro do `CREATE TABLE environments`, logo depois
de `fb_password_enc`:

```sql
    -- CNPJ da empresa que este ambiente representa, SO DIGITOS. E a chave do
    -- roteamento pelo documento: o CNPJ do fornecedor impresso no pedido casa
    -- aqui. NULL = ambiente nao participa do roteamento automatico.
    cnpj            TEXT,
```

E no fim de `COLUMN_MIGRATIONS`:

```python
    ("environments", "cnpj",
     "ALTER TABLE environments ADD COLUMN cnpj TEXT"),
```

Em `INDEXES_SQL`:

```sql
CREATE INDEX IF NOT EXISTS idx_environments_cnpj ON environments(cnpj)
    WHERE cnpj IS NOT NULL;
```

Em `app/persistence/environments_repo.py` — importar o normalizador no topo:

```python
from app.erp.cnpj import cnpj_digits
```

Somar `"cnpj"` a `_PUBLIC_FIELDS` (depois de `"fb_charset"`), aceitar o parâmetro em
`create()` (`cnpj: str | None = None`), gravar `cnpj_digits(cnpj) or None` na coluna, e em
`update()` tratar a semântica de três estados junto com a senha:

```python
    if cnpj is not None:
        fields["cnpj"] = cnpj_digits(cnpj) or None
```

E a função nova:

```python
def find_by_cnpj(cnpj: str | None) -> dict[str, Any] | None:
    """Ambiente ATIVO cujo CNPJ casa. Chave do roteamento pelo documento.

    Aceita formatado ou em dígitos — normaliza antes de comparar, porque a
    coluna guarda só dígitos. Ambiente inativo nunca casa: desativar um
    ambiente tem que tirá-lo do roteamento, não só da UI.
    """
    digits = cnpj_digits(cnpj)
    if not digits:
        return None
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM environments WHERE cnpj = ? AND is_active = 1 LIMIT 1",
            (digits,),
        ).fetchone()
    return _row_to_dict(row) if row else None
```

> Atenção ao dialeto: aqui é **SQLite**, então `LIMIT 1`. O `ROWS 1` que aparece em
> `app/erp/queries.py` é Firebird — aquele arquivo fala com outro banco.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_environments_repo.py -v`
Expected: PASS (todos, inclusive os antigos — a coluna é aditiva)

- [ ] **Step 5: Expor no admin**

Em `app/web/routes_environments.py`, somar `cnpj: str | None = None` ao modelo pydantic do
POST e do PUT e repassar para `environments_repo.create/update`. Em
`app/web/static/admin-ambiente-edit.html`, um campo de texto `CNPJ da empresa` ao lado de
`Nome`, com o hint: *"usado para reconhecer os pedidos que têm esta empresa como
fornecedor"*.

- [ ] **Step 6: Popular os dois ambientes reais**

Documentar no PR (não automatizar — é dado de produção, entra pela tela):

| ambiente | CNPJ |
|---|---|
| `nasmar` — NASMAR COMÉRCIO DE ROUPAS LTDA | `34513679000134` |
| `americanense` — M.M. AMERICANENSE | `35394871000111` |

- [ ] **Step 7: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/test_environments_repo.py -v
git add app/persistence/ app/web/routes_environments.py app/web/static/admin-ambiente-edit.html tests/test_environments_repo.py
git commit -m "feat(environments): CNPJ da empresa no cadastro do ambiente"
```

---

### Task 5: `supplier_cnpj` no modelo e a varredura no pipeline

**Files:**
- Modify: `app/models/order.py`
- Create: `app/routing/__init__.py`, `app/routing/documento.py`
- Modify: `app/pipeline.py`
- Test: `tests/test_routing_documento.py`, `tests/test_supplier_cnpj_samples.py`

**Interfaces:**
- Consumes: `environments_repo.find_by_cnpj` (Task 4), `app.erp.cnpj.cnpj_digits`.
- Produces: `OrderHeader.supplier_cnpj: str | None`;
  `documento.cnpjs_no_texto(texto: str) -> set[str]`;
  `documento.detectar_fornecedor(texto: str, conhecidos: dict[str, str]) -> str | None`;
  `documento.cnpjs_de_ambientes() -> dict[str, str]` (mapa `{cnpj_digits: env_slug}`).

**Contexto:** o fato 14 da spec mediu 29 arquivos — 7 com o CNPJ da Nasmar, 14 com o da
MM, **0 com os dois**, 8 sem nenhum. Dois daqueles arquivos nunca foram commitados;
**medido neste checkout em 2026-09-09 com a regex desta task: 27 arquivos, 20 resolvem,
0 ambíguos, 7 sem CNPJ.** A ausência de ambiguidade — a propriedade que torna o degrau
seguro — se confirma nos dois recortes, e é ela que o teste tranca.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_documento.py
from __future__ import annotations

from app.routing import documento

NASMAR = "34513679000134"
MM = "35394871000111"
CONHECIDOS = {NASMAR: "nasmar", MM: "americanense"}


def test_acha_cnpj_formatado():
    texto = "PEDIDO DE COMPRA\nFornecedor: NASMAR CNPJ 34.513.679/0001-34\nCliente: DAJU"
    assert documento.cnpjs_no_texto(texto) == {NASMAR}


def test_acha_cnpj_sem_formatacao():
    assert documento.cnpjs_no_texto("CNPJ:35394871000111 M.M.") == {MM}


def test_acha_cnpj_com_separador_de_espaco():
    """PDFs quebram a mascara: '35.394.871 / 0001-11' aparece no Sam's Club."""
    assert documento.cnpjs_no_texto("CNPJ 35.394.871 / 0001-11") == {MM}


def test_ignora_numero_de_14_digitos_que_nao_e_cnpj():
    """Codigo de variante da Centauro tem 12 digitos; EAN tem 13. Nem um nem
    outro pode virar CNPJ por acidente."""
    achados = documento.cnpjs_no_texto("EAN 7891234567890 COD 986388014917")
    assert MM not in achados and NASMAR not in achados


def test_detecta_fornecedor_quando_so_um_ambiente_aparece():
    texto = "Fornecedor NASMAR 34.513.679/0001-34 — pedido 4711"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) == NASMAR


def test_recusa_quando_os_dois_ambientes_aparecem():
    """A regra que impede o caso Centauro: dois CNPJs = sem resposta, nunca
    'o primeiro que apareceu'."""
    texto = f"Faturar {MM} — entregar via {NASMAR}"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) is None


def test_devolve_none_quando_nenhum_aparece():
    assert documento.detectar_fornecedor("PEDIDO KALLAN K01", CONHECIDOS) is None


def test_cnpj_repetido_no_mesmo_documento_ainda_resolve():
    """O CNPJ do fornecedor aparece no cabecalho e no rodape — e um so."""
    texto = f"{MM} ... corpo do pedido ... {MM}"
    assert documento.detectar_fornecedor(texto, CONHECIDOS) == MM
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_documento.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routing'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/routing/__init__.py
"""Roteamento de pedido para ambiente (multi-empresa).

Quem decide em que empresa um pedido entra é o **documento**, não um cadastro
de clientes — ver `docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`,
Revisão 4. Este pacote é a escada dessa decisão e nada mais: não abre conexão
por conta própria fora de `historico.py`, não escreve em lugar nenhum, e nunca
devolve um ambiente default.
"""
```

```python
# app/routing/documento.py
"""Degrau 1 do roteamento: o CNPJ do fornecedor impresso no pedido.

Todo pedido de compra identifica o fornecedor, e fornecedor é, por definição,
quem vai faturar. Medido em 29 samples reais (spec, fato 14): 21 trazem o CNPJ
de uma das duas empresas e NENHUM traz os dois.

Essa ausência de ambiguidade é o que torna o degrau seguro, e é uma propriedade
que este módulo **preserva por construção**: dois CNPJs conhecidos no mesmo
documento devolvem `None`. Sem resposta é um estado válido; palpite não é.
"""
from __future__ import annotations

import re

from app.erp.cnpj import cnpj_digits

# Aceita 12.345.678/0001-99, 12345678000199 e as variantes que o pdfplumber
# devolve quando o PDF quebra a máscara com espaços. Os separadores são
# opcionais e individualmente tolerantes a espaço em volta.
_CNPJ_RE = re.compile(
    r"\b\d{2}\s*[.\s]?\s*\d{3}\s*[.\s]?\s*\d{3}\s*[/\s]?\s*\d{4}\s*[-\s]?\s*\d{2}\b"
)


def cnpjs_no_texto(texto: str) -> set[str]:
    """Todos os CNPJs do texto, em dígitos. Vazio se não houver nenhum."""
    if not texto:
        return set()
    achados = {cnpj_digits(m.group(0)) for m in _CNPJ_RE.finditer(texto)}
    return {c for c in achados if len(c) == 14}


def detectar_fornecedor(texto: str, conhecidos: dict[str, str]) -> str | None:
    """CNPJ do ambiente que aparece no documento, ou `None`.

    `conhecidos` é `{cnpj_digits: env_slug}`. Devolve `None` em dois casos que
    são o mesmo caso: nenhum CNPJ conhecido no texto, ou mais de um. Nos dois,
    o documento não respondeu — quem responde é o degrau seguinte.
    """
    if not conhecidos:
        return None
    candidatos = cnpjs_no_texto(texto) & set(conhecidos)
    if len(candidatos) != 1:
        return None
    return candidatos.pop()


def cnpjs_de_ambientes() -> dict[str, str]:
    """Mapa `{cnpj_digits: env_slug}` dos ambientes ativos que têm CNPJ.

    Import local: `app.routing.documento` é chamado de dentro do pipeline, que
    roda também no CLI e nos testes de parser — nenhum dos dois deve pagar o
    custo de importar a camada de persistência quando não há ambiente algum.
    """
    from app.persistence import environments_repo

    return {
        e["cnpj"]: e["slug"]
        for e in environments_repo.list_active()
        if e.get("cnpj")
    }
```

Em `app/models/order.py`, dentro de `OrderHeader`:

```python
class OrderHeader(BaseModel):
    order_number: str | None = None
    issue_date: str | None = None
    customer_name: str | None = None
    customer_cnpj: str | None = None
    # CNPJ do FORNECEDOR impresso no documento, em dígitos. Preenchido pelo
    # pipeline (varredura) ou por um parser que saiba ler o rótulo. É a chave
    # do degrau 1 do roteamento — ver app/routing/documento.py.
    supplier_cnpj: str | None = None
```

Em `app/pipeline.py`, depois de `order.source_file = str(file.path)` e **antes** de
`_normalizer.normalize(order)`:

```python
    _marcar_fornecedor(order, extracted)
    order = _normalizer.normalize(order)
```

E o helper, no fim do arquivo:

```python
def _marcar_fornecedor(order: Order, extracted: dict) -> None:
    """Preenche `header.supplier_cnpj` pela varredura do texto do documento.

    Um parser que já tenha lido o rótulo FORNECEDOR ganha: só preenche se o
    campo estiver vazio.

    Nunca levanta. Um erro aqui (banco compartilhado ausente no CLI, ambiente
    sem CNPJ cadastrado) tem que deixar o pedido passar sem fornecedor — o
    roteamento cai para o degrau seguinte, que é exatamente o desenho. Parsing
    não pode quebrar por causa de roteamento.
    """
    if order.header.supplier_cnpj:
        return
    try:
        conhecidos = documento.cnpjs_de_ambientes()
        if not conhecidos:
            return
        order.header.supplier_cnpj = documento.detectar_fornecedor(
            extracted.get("text", "") or "", conhecidos
        )
    except Exception as exc:  # noqa: BLE001 — roteamento nunca derruba parsing
        logger.debug(f"fornecedor não detectado ({exc!r}); segue sem supplier_cnpj")
```

E o import no topo: `from app.routing import documento`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_documento.py -v`
Expected: PASS

- [ ] **Step 5: O teste que tranca a propriedade medida (29 samples reais)**

```python
# tests/test_supplier_cnpj_samples.py
"""Trava a propriedade que torna o degrau do documento seguro.

Spec, fato 14 (medido 2026-09-09): dos 29 samples, 21 trazem o CNPJ de UMA das
duas empresas e NENHUM traz os dois. Se um parser ou um sample novo quebrar
isso, este teste quebra antes da produção.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.extractors.pdf_extractor import PDFExtractor
from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.routing import documento

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
NASMAR = "34513679000134"
MM = "35394871000111"
CONHECIDOS = {NASMAR: "nasmar", MM: "americanense"}


def _texto(p: Path) -> str:
    raw = p.read_bytes()
    loaded = LoadedFile(path=p, extension=p.suffix.lower(), raw=raw)
    ext = PDFExtractor() if p.suffix.lower() == ".pdf" else XLSExtractor()
    return ext.extract(loaded).get("text", "") or ""


def _arquivos() -> list[Path]:
    return sorted(
        p for p in SAMPLES.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".xls", ".xlsx")
    )


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_nenhum_sample_traz_os_dois_cnpjs():
    """A propriedade central: o documento nunca resolve para dois ambientes."""
    ambiguos = []
    for p in _arquivos():
        achados = documento.cnpjs_no_texto(_texto(p)) & set(CONHECIDOS)
        if len(achados) > 1:
            ambiguos.append((p.name, sorted(achados)))
    assert ambiguos == [], f"sample com dois fornecedores: {ambiguos}"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_cobertura_medida_nao_regride():
    """20 dos 27 samples DO REPO resolvem pelo documento. Pode subir, nao descer.

    O fato 14 da spec fala em 21 de 29 porque foi medido com dois arquivos que
    nunca foram commitados em samples/ (um deles o PEDIDO TENNIS STATION).
    Medido neste checkout em 2026-09-09: 27 arquivos, 20 resolvem, 0 ambiguos.
    O piso e o numero do repo, nao o da spec — teste tem que rodar no que
    existe.
    """
    resolvidos = [
        p.name for p in _arquivos()
        if documento.detectar_fornecedor(_texto(p), CONHECIDOS) is not None
    ]
    assert len(resolvidos) >= 20, f"cobertura caiu para {len(resolvidos)}: {resolvidos}"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_samples_sem_cnpj_devolvem_none_e_nao_palpite():
    """Os 7 sem CNPJ de fornecedor caem para o degrau seguinte, nao para um default.

    Lista fechada e verificada no checkout em 2026-09-09 — sao exatamente estes
    sete, nem um a mais.
    """
    sem_cnpj = [
        "Desmembramento Authentic feet (1).xlsx",
        "Desmembramento Magic Feet.xlsx",
        "PEDIDO KALLAN K01.xlsx",
        "PEDIDO NBA 3.xlsx",
        "PEDIDO BEIRA RIO.pdf",
        "Pedido Authentic Fit.xlsx",
        "Pedido Magic Feet MF048.xlsx",
    ]
    for nome in sem_cnpj:
        p = SAMPLES / nome
        if not p.exists():
            continue
        assert documento.detectar_fornecedor(_texto(p), CONHECIDOS) is None, nome
```

- [ ] **Step 6: Rodar o teste dos samples e conferir os números**

Run: `.venv/bin/pytest tests/test_supplier_cnpj_samples.py -v`
Expected: PASS. Se `test_cobertura_medida_nao_regride` falhar com um número **abaixo** de
21, a regex está estreita demais para algum PDF — não relaxe o assert, conserte a regex.
Se falhar com número acima, ótimo: atualize o piso e o fato 14 da spec.

- [ ] **Step 7: A suíte de parsers não pode ter mexido**

Run: `.venv/bin/pytest tests/ -v -k "parser or pipeline"`
Expected: PASS — `supplier_cnpj` é campo novo com default `None`; nenhum snapshot
existente muda de forma.

- [ ] **Step 8: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/models/order.py app/routing/ app/pipeline.py tests/test_routing_documento.py tests/test_supplier_cnpj_samples.py
git commit -m "feat(routing): o pedido passa a dizer de quem ele e (supplier_cnpj)"
```

---

### Task 6: Degrau do histórico — onde este cliente já comprou

**Files:**
- Modify: `app/erp/queries.py` (nova função de SQL, no fim do arquivo)
- Create: `app/routing/historico.py`
- Test: `tests/test_routing_historico.py`

**Interfaces:**
- Consumes: `environments_repo.list_active`, `environments_repo.to_fb_config`,
  `app.erp.connection.FirebirdConnection`, `app.erp.cnpj.cnpj_digits`.
- Produces: `historico.HistoricoAmbiente` (frozen dataclass: `env_slug: str`,
  `env_nome: str`, `pedidos: int`, `ultimo_em: str | None`);
  `historico.consultar(cnpjs: Sequence[str | None], *, meses: int = 12,
  hoje: date | None = None, envs: Sequence[dict] | None = None,
  contar: Callable[[dict, list[str], str], tuple[int, str | None]] | None = None)
  -> tuple[HistoricoAmbiente, ...]`;
  `historico.resolver(hist: Sequence[HistoricoAmbiente]) -> str | None`;
  `queries.count_pedidos_cliente_desde_sql(n: int) -> str`.

**Contexto:** spec, fato 15. Na janela de 12 meses, 277 clientes: 73 só na Nasmar, 203 só
na MM, **1 nos dois**. O caso que não pode ser chutado é esse 1 — ele cai para o degrau
seguinte, não para o de maior volume. A janela de 12 meses é o que resolve a Beira Rio
(comprou da MM até 05/2025, migrou para a Nasmar).

`consultar` recebe **uma lista** de CNPJs, não um só: numa planilha de desmembramento a
identidade do comprador está nas colunas de loja (`delivery_cnpj`), não no cabeçalho.
Ver Task 12.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_historico.py
from __future__ import annotations

from datetime import date

from app.routing import historico
from app.routing.historico import HistoricoAmbiente

ENVS = [
    {"slug": "nasmar", "name": "Nasmar", "id": "1"},
    {"slug": "americanense", "name": "MM Americanense", "id": "2"},
]


def _fake_contar(mapa):
    """mapa: {env_slug: (pedidos, ultimo_em)} — substitui o Firebird."""
    def contar(env, cnpjs, desde):  # noqa: ARG001
        return mapa.get(env["slug"], (0, None))
    return contar


def test_cliente_so_num_banco_resolve():
    hist = historico.consultar(
        ["11222333000181"], envs=ENVS,
        contar=_fake_contar({"nasmar": (28, "2026-08-24")}),
    )
    assert historico.resolver(hist) == "nasmar"


def test_cliente_nos_dois_bancos_recusa():
    """1 cliente em 277. O historico se cala em vez de votar no maior volume."""
    hist = historico.consultar(
        ["11222333000181"], envs=ENVS,
        contar=_fake_contar({"nasmar": (28, "2026-08-24"), "americanense": (6, "2026-05-28")}),
    )
    assert historico.resolver(hist) is None


def test_cliente_sem_historico_devolve_none():
    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=_fake_contar({}))
    assert historico.resolver(hist) is None
    assert all(h.pedidos == 0 for h in hist)


def test_janela_de_12_meses_e_a_data_passada_ao_banco():
    """Caso Beira Rio: pedidos da MM ate 05/2025 ficam fora da janela."""
    vistos = {}

    def contar(env, cnpjs, desde):
        vistos[env["slug"]] = desde
        return (0, None)

    historico.consultar(
        ["11222333000181"], envs=ENVS, meses=12,
        hoje=date(2026, 9, 9), contar=contar,
    )
    assert vistos["nasmar"] == "2025-09-09"
    assert vistos["americanense"] == "2025-09-09"


def test_consulta_com_varios_cnpjs_soma_no_mesmo_ambiente():
    """Desmembramento: as lojas sao CNPJs diferentes do mesmo comprador."""
    recebidos = {}

    def contar(env, cnpjs, desde):  # noqa: ARG001
        recebidos[env["slug"]] = list(cnpjs)
        return (3, "2026-07-01") if env["slug"] == "nasmar" else (0, None)

    hist = historico.consultar(
        ["05055599002985", "05055599002632"], envs=ENVS, contar=contar,
    )
    assert recebidos["nasmar"] == ["05055599002985", "05055599002632"]
    assert historico.resolver(hist) == "nasmar"


def test_cnpj_vazio_nao_consulta_nada():
    """Sem CNPJ de cliente nao ha o que perguntar — nao abre conexao a toa."""
    chamou = False

    def contar(env, cnpjs, desde):  # noqa: ARG001
        nonlocal chamou
        chamou = True
        return (0, None)

    hist = historico.consultar([None, "", "  "], envs=ENVS, contar=contar)
    assert hist == ()
    assert chamou is False


def test_erro_de_conexao_num_ambiente_nao_derruba_o_outro():
    """VPN cai no meio da consulta: o ambiente que respondeu continua valendo,
    e o que falhou conta como 'nao sei', nao como 'nao tem'."""
    def contar(env, cnpjs, desde):  # noqa: ARG001
        if env["slug"] == "americanense":
            raise OSError("connection refused")
        return (5, "2026-08-01")

    hist = historico.consultar(["11222333000181"], envs=ENVS, contar=contar)
    slugs = {h.env_slug for h in hist}
    assert slugs == {"nasmar"}
    assert historico.resolver(hist) == "nasmar"


def test_resolver_ignora_ambiente_com_zero_pedidos():
    hist = (
        HistoricoAmbiente("nasmar", "Nasmar", 4, "2026-05-07"),
        HistoricoAmbiente("americanense", "MM", 0, None),
    )
    assert historico.resolver(hist) == "nasmar"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_historico.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routing.historico'`

- [ ] **Step 3: Write minimal implementation**

No fim de `app/erp/queries.py`:

```python
# ── Roteamento: histórico do cliente (degrau 2) ───────────────────────────────
# "Este cliente já comprou desta empresa nos últimos N meses?" — a resposta
# que decide o ambiente quando o documento não traz o CNPJ do fornecedor.
# CPF_CNPJ é limpo do mesmo jeito que em FIND_CLIENT_BY_CNPJ; o bind chega em
# dígitos. Aceita N CNPJs porque uma planilha de desmembramento identifica o
# comprador pelas colunas de loja, não pelo cabeçalho.
def count_pedidos_cliente_desde_sql(n: int) -> str:
    """SQL com N placeholders de CNPJ. Bind: (desde, cnpj1, ..., cnpjN)."""
    if n < 1:
        raise ValueError("count_pedidos_cliente_desde_sql exige ao menos 1 CNPJ")
    marks = ", ".join("?" for _ in range(n))
    return f"""
        SELECT COUNT(*), MAX(V.DATA_PEDIDO)
        FROM CAB_VENDAS V
        JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
        WHERE V.DATA_PEDIDO >= ?
          AND REPLACE(REPLACE(REPLACE(REPLACE(
                C.CPF_CNPJ, '.', ''), '/', ''), '-', ''), ' ', '') IN ({marks})
    """
```

```python
# app/routing/historico.py
"""Degrau 2 do roteamento: onde este cliente já comprou.

Consulta os Firebird de todos os ambientes ativos e devolve, por ambiente,
quantos pedidos aquele cliente tem na janela e qual o mais recente.

Duas regras que não se negociam:

1. **Ambíguo não responde.** Cliente com pedido em dois ambientes dentro da
   janela devolve `None` — não o de maior volume. Medido: 1 cliente em 277
   (spec, fato 15). É o caso que não pode ser chutado, não o caso comum.
2. **Erro não vira ausência.** Se o Firebird de um ambiente não responde, esse
   ambiente sai da lista em vez de entrar com zero. Contar "não sei" como
   "não tem" transformaria uma VPN caída em roteamento errado com confiança.

A janela de 12 meses é uma escolha, não um fato (spec, "Riscos"): ela é o que
resolve a Beira Rio, que comprou da MM até 05/2025 e migrou para a Nasmar.
"""
from __future__ import annotations

import calendar
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from app.erp import queries
from app.erp.cnpj import cnpj_digits
from app.utils.logger import logger


@dataclass(frozen=True)
class HistoricoAmbiente:
    env_slug: str
    env_nome: str
    pedidos: int
    ultimo_em: str | None


def _janela_desde(meses: int, hoje: date | None) -> str:
    """Data de corte da janela, em ISO. `meses` para trás a partir de hoje.

    O clamp do dia não é preciosismo: 31/03 menos 12 meses cai em 31/03, mas
    31/05 menos 3 meses cairia em 31/02 e `date()` levantaria.
    """
    ref = hoje or date.today()
    ano = ref.year - (meses // 12)
    mes = ref.month - (meses % 12)
    if mes <= 0:
        mes += 12
        ano -= 1
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(ref.day, ultimo_dia)).isoformat()


def _contar_no_fire(env: dict, cnpjs: list[str], desde: str) -> tuple[int, str | None]:
    """Consulta real ao Firebird do ambiente. Somente leitura."""
    from app.erp.connection import FirebirdConnection
    from app.persistence import environments_repo

    cfg = environments_repo.to_fb_config(env)
    if not cfg.get("path"):
        raise ValueError(f"ambiente {env['slug']} sem fb_path")
    sql = queries.count_pedidos_cliente_desde_sql(len(cnpjs))
    with FirebirdConnection().connect_with_config(cfg) as conn:
        cur = conn.cursor()
        cur.execute(sql, (desde, *cnpjs))
        row = cur.fetchone()
    if not row:
        return 0, None
    total = int(row[0] or 0)
    ultimo = row[1].isoformat() if hasattr(row[1], "isoformat") else (
        str(row[1]) if row[1] else None
    )
    return total, ultimo


def consultar(
    cnpjs: Sequence[str | None],
    *,
    meses: int = 12,
    hoje: date | None = None,
    envs: Sequence[dict] | None = None,
    contar: Callable[[dict, list[str], str], tuple[int, str | None]] | None = None,
) -> tuple[HistoricoAmbiente, ...]:
    """Histórico do cliente em cada ambiente ativo, na janela de `meses`.

    `envs` e `contar` são injetáveis para teste; em produção vêm de
    `environments_repo.list_active()` e do Firebird.
    """
    limpos = [c for c in (cnpj_digits(x) for x in cnpjs) if c]
    if not limpos:
        return ()

    if envs is None:
        from app.persistence import environments_repo

        envs = environments_repo.list_active()
    consulta = contar or _contar_no_fire
    desde = _janela_desde(meses, hoje)

    saida: list[HistoricoAmbiente] = []
    for env in envs:
        try:
            pedidos, ultimo = consulta(env, limpos, desde)
        except Exception as exc:  # noqa: BLE001 — ver regra 2 no docstring
            logger.warning(
                "routing.historico.indisponivel env={} erro={!r}", env["slug"], exc
            )
            continue
        saida.append(
            HistoricoAmbiente(
                env_slug=env["slug"],
                env_nome=env.get("name") or env["slug"],
                pedidos=int(pedidos or 0),
                ultimo_em=ultimo,
            )
        )
    return tuple(saida)


def resolver(hist: Sequence[HistoricoAmbiente]) -> str | None:
    """O ambiente do histórico, ou `None` se ele não for inequívoco."""
    com_pedido = [h for h in hist if h.pedidos > 0]
    if len(com_pedido) != 1:
        return None
    return com_pedido[0].env_slug
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_historico.py -v`
Expected: PASS

- [ ] **Step 5: Conferir a query contra a Fire viva (leitura, manual)**

Gate manual. Pelo túnel da VPN, somente leitura, com o CNPJ da Beira Rio:

```bash
.venv/bin/python -c "
from app.erp import queries
print(queries.count_pedidos_cliente_desde_sql(1))
"
```

Rodar essa SQL nos dois bancos com `desde='2025-09-09'` e conferir contra a tabela do
fato 15 da spec: Nasmar 28 pedidos / MM 0 na janela. Registrar o resultado no PR. **Se os
números não baterem, pare e investigue antes de seguir** — o degrau inteiro depende dessa
query estar certa.

- [ ] **Step 6: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/erp/queries.py app/routing/historico.py tests/test_routing_historico.py
git commit -m "feat(routing): historico do Fire vira degrau — 12 meses, ambiguo nao responde"
```

---

### Task 7: Memória das escolhas do operador

**Files:**
- Modify: `app/persistence/schema_shared.py` (TABLES_SQL, INDEXES_SQL)
- Create: `app/persistence/decisao_ambiente_repo.py`
- Test: `tests/test_decisao_ambiente_repo.py`

**Interfaces:**
- Produces: `decisao_ambiente_repo.lembrar(*, cnpj_cliente: str, env_slug: str, por: str) -> None`;
  `lembrada(cnpj_cliente: str) -> dict | None`;
  `marcar_divergencia(*, cnpj_cliente: str, de: str) -> None`;
  `listar(limit: int = 200) -> list[dict]`.

**Contexto:** esta tabela **nasce vazia e isso é um estado válido**, não um erro. Ela não
é pré-requisito de nada: o Portal funciona 100% no dia 1 sem uma linha. Ela existe para
poupar a segunda pergunta e, principalmente, para responder *"quem decidiu isso, quando"*
em uma linha quando um pedido acabar na empresa errada.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_decisao_ambiente_repo.py
from __future__ import annotations

import pytest

from app.persistence import decisao_ambiente_repo as memoria
from app.persistence import router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def test_memoria_vazia_e_estado_valido(fresh_shared):
    """Nao e erro, nao levanta, nao tem default. So nao sabe."""
    assert memoria.lembrada("11222333000181") is None
    assert memoria.listar() == []


def test_lembra_e_devolve_com_autor_e_data(fresh_shared):
    memoria.lembrar(cnpj_cliente="11.222.333/0001-81", env_slug="nasmar", por="grazi@mm")
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "nasmar"
    assert d["decidido_por"] == "grazi@mm"
    assert d["decidido_em"]
    assert d["divergiu_em"] is None


def test_lembrar_de_novo_sobrescreve_e_registra_quem(fresh_shared):
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="grazi@mm")
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="americanense", por="camila@mm")
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "americanense"
    assert d["decidido_por"] == "camila@mm"
    assert len(memoria.listar()) == 1  # UNIQUE por CNPJ, nao acumula


def test_divergencia_e_registrada_nao_silenciada(fresh_shared):
    """O documento chegou e contradisse a memoria: fica a marca."""
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="grazi@mm")
    memoria.marcar_divergencia(cnpj_cliente="11222333000181", de="americanense")
    d = memoria.lembrada("11222333000181")
    assert d["divergiu_de"] == "americanense"
    assert d["divergiu_em"]
    assert d["env_slug"] == "nasmar"  # a memoria NAO e reescrita pela divergencia


def test_marcar_divergencia_em_cnpj_desconhecido_nao_levanta(fresh_shared):
    memoria.marcar_divergencia(cnpj_cliente="99999999000199", de="mm")
    assert memoria.lembrada("99999999000199") is None


def test_cnpj_e_normalizado_na_escrita_e_na_leitura(fresh_shared):
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="x")
    assert memoria.lembrada("11.222.333/0001-81") is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_decisao_ambiente_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.persistence.decisao_ambiente_repo'`

- [ ] **Step 3: Write minimal implementation**

Em `app/persistence/schema_shared.py`, no fim de `TABLES_SQL` (antes das aspas de
fechamento):

```sql
-- Memória das escolhas de ambiente feitas pelo operador. NASCE VAZIA e isso é
-- um estado válido: o Portal funciona sem uma linha aqui. Perde para o
-- documento E para o histórico — é o degrau 3, não a verdade.
-- `divergiu_*` é preenchido quando um degrau mais forte contradiz a escolha
-- depois: erro que aparece é erro que se conserta.
CREATE TABLE IF NOT EXISTS decisao_ambiente (
    cnpj_cliente  TEXT PRIMARY KEY,
    env_slug      TEXT NOT NULL,
    decidido_por  TEXT NOT NULL,
    decidido_em   TEXT NOT NULL,
    divergiu_em   TEXT,
    divergiu_de   TEXT
);
```

E em `INDEXES_SQL`:

```sql
CREATE INDEX IF NOT EXISTS idx_decisao_ambiente_env ON decisao_ambiente(env_slug);
```

```python
# app/persistence/decisao_ambiente_repo.py
"""Degrau 3 do roteamento: a escolha que um humano já fez para este cliente.

Tabela transversal (`app_shared.db`) — a decisão de roteamento acontece **antes**
de existir ambiente ativo, então ela não pode morar em `app_state_<slug>.db`.

Três coisas que este módulo NÃO é:

- não é cadastro curado: ninguém precisa preencher, ninguém precisa revisar;
- não é fonte de verdade: perde para o documento e para o histórico, sempre;
- não é pré-requisito: vazia, o Portal funciona igual — só pergunta mais.

Ela cresce com clientes novos, uma vez cada, e vira redundante assim que o
pedido entra no Fire (aí o histórico responde). O que sobra é auditoria:
"quem decidiu isso, quando" em uma linha.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.erp.cnpj import cnpj_digits
from app.persistence import router

_FIELDS = ("cnpj_cliente", "env_slug", "decidido_por", "decidido_em",
           "divergiu_em", "divergiu_de")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def lembrar(*, cnpj_cliente: str, env_slug: str, por: str) -> None:
    """Grava (ou substitui) a decisão para este CNPJ, com autor e data."""
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO decisao_ambiente
                   (cnpj_cliente, env_slug, decidido_por, decidido_em)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(cnpj_cliente) DO UPDATE SET
                   env_slug = excluded.env_slug,
                   decidido_por = excluded.decidido_por,
                   decidido_em = excluded.decidido_em,
                   divergiu_em = NULL,
                   divergiu_de = NULL""",
            (digits, env_slug, por, _now()),
        )


def lembrada(cnpj_cliente: str) -> dict[str, Any] | None:
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return None
    with router.shared_connect() as conn:
        row = conn.execute(
            "SELECT * FROM decisao_ambiente WHERE cnpj_cliente = ?", (digits,)
        ).fetchone()
    return {k: row[k] for k in _FIELDS} if row else None


def marcar_divergencia(*, cnpj_cliente: str, de: str) -> None:
    """Um degrau mais forte contradisse a memória. Marca, não reescreve.

    Reescrever esconderia que houve conflito; o valor lembrado continua sendo o
    julgamento humano registrado, e a marca é o que faz alguém ir olhar.
    """
    digits = cnpj_digits(cnpj_cliente)
    if not digits:
        return
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE decisao_ambiente SET divergiu_em = ?, divergiu_de = ? "
            "WHERE cnpj_cliente = ?",
            (_now(), de, digits),
        )


def listar(limit: int = 200) -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM decisao_ambiente ORDER BY decidido_em DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [{k: r[k] for k in _FIELDS} for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_decisao_ambiente_repo.py -v`
Expected: PASS

- [ ] **Step 5: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/persistence/ tests/test_decisao_ambiente_repo.py
git commit -m "feat(routing): memoria das escolhas de ambiente (nasce vazia, e ok)"
```

---

### Task 8: O interruptor de três estados, a tabela sombra e a fila de pendências

Esta task vem **antes** do wiring de propósito. É ela que garante que a Fase 1 pode ser
mergeada e deployada sem mudar nada para o operador: o default é `'desligado'`, e
`'desligado'` significa que o roteador nem é chamado.

**Files:**
- Modify: `app/persistence/schema_shared.py` (TABLES_SQL, INDEXES_SQL)
- Create: `app/persistence/roteamento_repo.py`
- Test: `tests/test_roteamento_repo.py`

**Interfaces:**
- Produces: `roteamento_repo.MODOS: tuple[str, ...]` = `("desligado", "observando", "ligado")`;
  `modo() -> str`; `set_modo(valor: str, *, por: str) -> None`;
  `registrar_sombra(*, import_id: str, degrau: str, env_sugerido: str | None,
  env_escolhido: str | None) -> None`;
  `taxa(dias: int = 30) -> dict` com chaves `total`, `bateu`, `perguntar`, `divergiu`, `desde`;
  `registrar_pendencia(*, sha256: str, source_path: str, env_scan_slug: str,
  order_number: str | None, customer_cnpj: str | None, customer_name: str | None) -> None`;
  `listar_pendencias(limit: int = 200) -> list[dict]`; `contar_pendencias() -> int`;
  `limpar_pendencia(sha256: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_roteamento_repo.py
from __future__ import annotations

import pytest

from app.persistence import roteamento_repo as rot
from app.persistence import router


@pytest.fixture
def fresh_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield


def test_instalacao_nova_nasce_desligada(fresh_shared):
    """O teste que protege a adocao. Nao relaxar."""
    assert rot.modo() == "desligado"


def test_set_modo_grava_valor_autor_e_data(fresh_shared):
    rot.set_modo("observando", por="samuel@mm")
    assert rot.modo() == "observando"
    rot.set_modo("ligado", por="samuel@mm")
    assert rot.modo() == "ligado"
    rot.set_modo("desligado", por="samuel@mm")
    assert rot.modo() == "desligado"


def test_set_modo_recusa_valor_invalido(fresh_shared):
    with pytest.raises(ValueError):
        rot.set_modo("meio-ligado", por="samuel@mm")
    assert rot.modo() == "desligado"


def test_sombra_conta_acerto_divergencia_e_silencio(fresh_shared):
    rot.registrar_sombra(import_id="a", degrau="documento",
                         env_sugerido="nasmar", env_escolhido="nasmar")
    rot.registrar_sombra(import_id="b", degrau="historico",
                         env_sugerido="nasmar", env_escolhido="americanense")
    rot.registrar_sombra(import_id="c", degrau="perguntar",
                         env_sugerido=None, env_escolhido="americanense")
    t = rot.taxa(dias=30)
    assert t["total"] == 3
    assert t["bateu"] == 1
    assert t["divergiu"] == 1
    assert t["perguntar"] == 1


def test_taxa_de_periodo_vazio_nao_divide_por_zero(fresh_shared):
    t = rot.taxa(dias=30)
    assert t == {"total": 0, "bateu": 0, "divergiu": 0, "perguntar": 0, "desde": t["desde"]}


def test_pendencia_do_mesmo_arquivo_nao_duplica(fresh_shared):
    """O watcher re-varre a pasta a cada ciclo; a fila nao pode inflar."""
    for _ in range(3):
        rot.registrar_pendencia(
            sha256="abc123", source_path="/in/PEDIDO NBA 3.xlsx",
            env_scan_slug="nasmar", order_number="NBA 3",
            customer_cnpj=None, customer_name=None,
        )
    assert rot.contar_pendencias() == 1
    p = rot.listar_pendencias()[0]
    assert p["visto_vezes"] == 3


def test_limpar_pendencia_some_da_fila(fresh_shared):
    rot.registrar_pendencia(
        sha256="abc123", source_path="/in/x.xlsx", env_scan_slug="nasmar",
        order_number=None, customer_cnpj=None, customer_name=None,
    )
    rot.limpar_pendencia("abc123")
    assert rot.contar_pendencias() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_roteamento_repo.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.persistence.roteamento_repo'`

- [ ] **Step 3: Write minimal implementation**

Em `app/persistence/schema_shared.py`, no fim de `TABLES_SQL`:

```sql
-- Interruptor global do roteamento. Linha única (id=1), alterável no admin
-- sem deploy. Default 'desligado' é o que permite mergear e deployar a Fase 1
-- sem mudar nada para o operador.
--   desligado   o roteador nem é chamado; comportamento de hoje, íntegro
--   observando  calcula e grava o que TERIA feito; a escolha do operador vale
--   ligado      a decisão vale
CREATE TABLE IF NOT EXISTS roteamento_modo (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    valor        TEXT NOT NULL DEFAULT 'desligado'
                 CHECK (valor IN ('desligado', 'observando', 'ligado')),
    alterado_por TEXT,
    alterado_em  TEXT
);

-- O que o Portal TERIA decidido, versus o que aconteceu. Alimenta a taxa de
-- acerto que autoriza virar a chave, e a lista de divergências que é o
-- material de treinamento do time.
CREATE TABLE IF NOT EXISTS roteamento_sombra (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id                   TEXT NOT NULL,
    decidido_em                 TEXT NOT NULL,
    degrau                      TEXT NOT NULL,
    env_sugerido                TEXT,
    env_escolhido_pelo_operador TEXT,
    bateu                       INTEGER NOT NULL
);

-- Arquivo que o watcher não soube rotear. NÃO é importado em ambiente
-- nenhum: fica visível aqui e o arquivo continua na pasta de entrada, onde o
-- operador pode abri-lo pelo preview e responder. Chave = sha do arquivo, para
-- que a re-varredura a cada ciclo não infle a fila.
CREATE TABLE IF NOT EXISTS roteamento_pendencia (
    sha256        TEXT PRIMARY KEY,
    source_path   TEXT NOT NULL,
    env_scan_slug TEXT NOT NULL,
    order_number  TEXT,
    customer_cnpj TEXT,
    customer_name TEXT,
    visto_em      TEXT NOT NULL,
    visto_vezes   INTEGER NOT NULL DEFAULT 1
);
```

E em `INDEXES_SQL`:

```sql
CREATE INDEX IF NOT EXISTS idx_sombra_decidido_em ON roteamento_sombra(decidido_em DESC);
CREATE INDEX IF NOT EXISTS idx_sombra_import_id   ON roteamento_sombra(import_id);
```

```python
# app/persistence/roteamento_repo.py
"""Interruptor do roteamento, tabela sombra e fila de pendências (shared).

O interruptor tem TRÊS estados, não dois, porque o problema do Samuel é de
sequência: ele não pode validar o que não está rodando, e não pode ligar o que
não validou. `observando` é o estado que resolve isso — o roteador roda, grava
o que teria feito, e não age.

Regra que não se relaxa: **instalação nova nasce em `'desligado'`**, e
`'desligado'` significa que o roteador nem é chamado.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.persistence import router

MODOS: tuple[str, ...] = ("desligado", "observando", "ligado")
DESLIGADO, OBSERVANDO, LIGADO = MODOS

_PEND_FIELDS = ("sha256", "source_path", "env_scan_slug", "order_number",
                "customer_cnpj", "customer_name", "visto_em", "visto_vezes")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def modo() -> str:
    """Modo atual. Sem linha na tabela = `'desligado'` — o default é o seguro."""
    with router.shared_connect() as conn:
        row = conn.execute("SELECT valor FROM roteamento_modo WHERE id = 1").fetchone()
    return row[0] if row else DESLIGADO


def set_modo(valor: str, *, por: str) -> None:
    if valor not in MODOS:
        raise ValueError(f"modo inválido: {valor!r} — use um de {MODOS}")
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO roteamento_modo (id, valor, alterado_por, alterado_em)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   valor = excluded.valor,
                   alterado_por = excluded.alterado_por,
                   alterado_em = excluded.alterado_em""",
            (valor, por, _now()),
        )


def registrar_sombra(
    *, import_id: str, degrau: str,
    env_sugerido: str | None, env_escolhido: str | None,
) -> None:
    """Registra a decisão sombra de um pedido. Nunca levanta para o chamador.

    Em `observando` isto roda no caminho do commit. Falhar aqui não pode
    impedir a importação de um pedido — a evidência é importante, o pedido é
    mais.
    """
    bateu = 1 if (env_sugerido is not None and env_sugerido == env_escolhido) else 0
    try:
        with router.shared_connect() as conn:
            conn.execute(
                """INSERT INTO roteamento_sombra
                       (import_id, decidido_em, degrau, env_sugerido,
                        env_escolhido_pelo_operador, bateu)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (import_id, _now(), degrau, env_sugerido, env_escolhido, bateu),
            )
    except Exception:  # noqa: BLE001 — ver docstring
        from app.utils.logger import logger

        logger.warning("roteamento.sombra_falhou import_id={}", import_id)


def taxa(dias: int = 30) -> dict[str, Any]:
    """Taxa de acerto da janela: total, bateu, divergiu, não soube responder."""
    desde = (datetime.now(UTC) - timedelta(days=int(dias))).isoformat(timespec="seconds")
    with router.shared_connect() as conn:
        row = conn.execute(
            """SELECT COUNT(*),
                      COALESCE(SUM(bateu), 0),
                      COALESCE(SUM(CASE WHEN degrau = 'perguntar' THEN 1 ELSE 0 END), 0)
               FROM roteamento_sombra WHERE decidido_em >= ?""",
            (desde,),
        ).fetchone()
    total, bateu, perguntar = int(row[0]), int(row[1]), int(row[2])
    return {
        "total": total,
        "bateu": bateu,
        "perguntar": perguntar,
        "divergiu": total - bateu - perguntar,
        "desde": desde,
    }


def registrar_pendencia(
    *, sha256: str, source_path: str, env_scan_slug: str,
    order_number: str | None, customer_cnpj: str | None, customer_name: str | None,
) -> None:
    """Marca um arquivo que o watcher não soube rotear. Idempotente por sha."""
    with router.shared_connect() as conn:
        conn.execute(
            """INSERT INTO roteamento_pendencia
                   (sha256, source_path, env_scan_slug, order_number,
                    customer_cnpj, customer_name, visto_em, visto_vezes)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(sha256) DO UPDATE SET
                   visto_em = excluded.visto_em,
                   source_path = excluded.source_path,
                   visto_vezes = roteamento_pendencia.visto_vezes + 1""",
            (sha256, source_path, env_scan_slug, order_number,
             customer_cnpj, customer_name, _now()),
        )


def listar_pendencias(limit: int = 200) -> list[dict[str, Any]]:
    with router.shared_connect() as conn:
        rows = conn.execute(
            "SELECT * FROM roteamento_pendencia ORDER BY visto_em DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [{k: r[k] for k in _PEND_FIELDS} for r in rows]


def contar_pendencias() -> int:
    with router.shared_connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM roteamento_pendencia").fetchone()
    return int(row[0]) if row else 0


def limpar_pendencia(sha256: str) -> None:
    with router.shared_connect() as conn:
        conn.execute("DELETE FROM roteamento_pendencia WHERE sha256 = ?", (sha256,))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_roteamento_repo.py -v`
Expected: PASS

- [ ] **Step 5: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/persistence/ tests/test_roteamento_repo.py
git commit -m "feat(routing): interruptor de tres estados, tabela sombra e fila de pendencias"
```

---

### Task 9: A escada — `app/routing/ambiente.py`

**Files:**
- Create: `app/routing/ambiente.py`
- Test: `tests/test_routing_ambiente.py`

**Interfaces:**
- Consumes: `documento.cnpjs_de_ambientes` (Task 5), `historico.consultar`/`resolver`
  (Task 6), `decisao_ambiente_repo.lembrada`/`marcar_divergencia` (Task 7),
  `environments_repo.find_by_cnpj` (Task 4).
- Produces:
  ```python
  DEGRAUS = ("documento", "historico", "memoria", "perguntar")

  @dataclass(frozen=True)
  class Decisao:
      env_slug: str | None
      degrau: str
      explicacao: str
      divergiu_de: str | None = None
      historico: tuple[HistoricoAmbiente, ...] = ()

  @dataclass(frozen=True)
  class Deps:
      env_por_cnpj: Callable[[str], str | None]
      historico: Callable[[Sequence[str | None]], tuple[HistoricoAmbiente, ...]]
      memoria: Callable[[str], str | None]

  def cnpjs_do_pedido(order: Order) -> list[str]
  def ambiente_para(order: Order, deps: Deps) -> Decisao
  def deps_padrao() -> Deps
  ```

**A precedência é estrita e é o coração da feature:** documento > histórico > memória >
perguntar. Cada degrau perde para o de cima, sem exceção. Documento é fato sobre **este**
pedido; histórico é fato sobre o passado; memória é julgamento humano.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_ambiente.py
from __future__ import annotations

from app.models.order import Order, OrderHeader, OrderItem
from app.routing import ambiente
from app.routing.ambiente import Deps
from app.routing.historico import HistoricoAmbiente

NASMAR = "34513679000134"
MM = "35394871000111"
CLIENTE = "11222333000181"


def _pedido(*, supplier=None, cliente=CLIENTE, entregas=()):
    return Order(
        header=OrderHeader(order_number="4711", customer_cnpj=cliente,
                           customer_name="DAJU", supplier_cnpj=supplier),
        items=[OrderItem(description="Meia", quantity=1, delivery_cnpj=c)
               for c in entregas] or [OrderItem(description="Meia", quantity=1)],
    )


def _deps(*, por_cnpj=None, hist=(), memoria=None):
    return Deps(
        env_por_cnpj=lambda c: (por_cnpj or {}).get(c),
        historico=lambda cnpjs: hist,  # noqa: ARG005
        memoria=lambda c: memoria,     # noqa: ARG005
    )


def test_documento_resolve():
    d = ambiente.ambiente_para(
        _pedido(supplier=NASMAR), _deps(por_cnpj={NASMAR: "nasmar"})
    )
    assert d.env_slug == "nasmar"
    assert d.degrau == "documento"
    assert "34.513.679/0001-34" in d.explicacao or NASMAR in d.explicacao


def test_documento_vence_historico_e_memoria():
    """A regra que torna o caso Centauro impossivel por construcao."""
    d = ambiente.ambiente_para(
        _pedido(supplier=MM),
        _deps(
            por_cnpj={MM: "americanense", NASMAR: "nasmar"},
            hist=(HistoricoAmbiente("nasmar", "Nasmar", 40, "2026-08-01"),),
            memoria="nasmar",
        ),
    )
    assert d.env_slug == "americanense"
    assert d.degrau == "documento"
    assert d.divergiu_de == "nasmar"


def test_historico_vence_memoria():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),),
              memoria="americanense"),
    )
    assert d.env_slug == "nasmar"
    assert d.degrau == "historico"
    assert d.divergiu_de == "americanense"


def test_memoria_responde_quando_documento_e_historico_calam():
    d = ambiente.ambiente_para(_pedido(), _deps(memoria="nasmar"))
    assert d.env_slug == "nasmar"
    assert d.degrau == "memoria"
    assert d.divergiu_de is None


def test_sem_nada_pergunta_e_nunca_devolve_default():
    d = ambiente.ambiente_para(_pedido(), _deps())
    assert d.env_slug is None
    assert d.degrau == "perguntar"


def test_historico_ambiguo_cai_para_o_degrau_seguinte():
    """Cliente nos dois bancos: 1 em 277. Nao vota no maior volume."""
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
                    HistoricoAmbiente("americanense", "MM", 6, "2026-05-28")),
              memoria="americanense"),
    )
    assert d.degrau == "memoria"
    assert d.env_slug == "americanense"


def test_historico_ambiguo_sem_memoria_pergunta():
    d = ambiente.ambiente_para(
        _pedido(),
        _deps(hist=(HistoricoAmbiente("nasmar", "Nasmar", 28, "2026-08-24"),
                    HistoricoAmbiente("americanense", "MM", 6, "2026-05-28"))),
    )
    assert d.env_slug is None
    assert d.degrau == "perguntar"
    assert len(d.historico) == 2  # a UI mostra os dois, mesmo sem resolver


def test_supplier_cnpj_desconhecido_nao_resolve():
    """CNPJ de fornecedor que nao e de nenhum ambiente: cai, nao inventa."""
    d = ambiente.ambiente_para(
        _pedido(supplier="99999999000199"), _deps(por_cnpj={NASMAR: "nasmar"})
    )
    assert d.degrau == "perguntar"


def test_supplier_cnpj_malformado_nao_derruba():
    d = ambiente.ambiente_para(_pedido(supplier="abc"), _deps())
    assert d.degrau == "perguntar"


def test_cnpjs_do_pedido_junta_cliente_e_lojas_sem_repetir():
    order = _pedido(cliente="05055599002985",
                    entregas=("05.055.599/0026-32", "05055599002985", None))
    assert ambiente.cnpjs_do_pedido(order) == ["05055599002985", "05055599002632"]


def test_pedido_sem_cnpj_nenhum_pergunta():
    order = Order(header=OrderHeader(order_number="X"),
                  items=[OrderItem(description="Meia", quantity=1)])
    d = ambiente.ambiente_para(order, _deps())
    assert d.degrau == "perguntar"
    assert d.env_slug is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_ambiente.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routing.ambiente'`

- [ ] **Step 3: Write minimal implementation**

```python
# app/routing/ambiente.py
"""A escada do roteamento: em que empresa este pedido entra?

    1. supplier_cnpj do documento casa com environments.cnpj  -> documento
    2. histórico do cliente no Fire, 12 meses, SE inequívoco  -> historico
    3. decisão lembrada para este cliente                     -> memoria
    4. nada resolveu, ou histórico ambíguo                    -> perguntar

A precedência é **estrita**. Cada degrau perde para o de cima, sem exceção:
documento é fato sobre este pedido, histórico é fato sobre o passado, memória é
julgamento humano. Quando um degrau mais forte contradiz um mais fraco, a
divergência viaja na `Decisao` — não é sobrescrita em silêncio.

Este módulo é **puro**: recebe `Deps` com as três leituras e não abre conexão
nenhuma. É o que permite testar a escada inteira sem banco, e é o que torna o
modo `observando` barato — rodar sem agir não custa quase nada.

Ele NUNCA devolve um ambiente default. `env_slug is None` com
`degrau == 'perguntar'` é uma resposta legítima e é a que o chamador tem que
saber tratar.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from app.erp.cnpj import cnpj_digits
from app.models.order import Order
from app.routing.historico import HistoricoAmbiente, resolver as _resolver_historico

DEGRAUS: tuple[str, ...] = ("documento", "historico", "memoria", "perguntar")


@dataclass(frozen=True)
class Decisao:
    env_slug: str | None
    degrau: str
    explicacao: str
    divergiu_de: str | None = None
    historico: tuple[HistoricoAmbiente, ...] = field(default_factory=tuple)

    @property
    def resolveu(self) -> bool:
        return self.env_slug is not None


@dataclass(frozen=True)
class Deps:
    env_por_cnpj: Callable[[str], str | None]
    historico: Callable[[Sequence[str | None]], tuple[HistoricoAmbiente, ...]]
    memoria: Callable[[str], str | None]


def _fmt(cnpj: str) -> str:
    if len(cnpj) != 14:
        return cnpj
    return f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"


def cnpjs_do_pedido(order: Order) -> list[str]:
    """CNPJs que identificam o comprador: o do cabeçalho e os das lojas.

    Ordem preservada e sem repetição. As lojas entram porque numa planilha de
    desmembramento a identidade do comprador está nas colunas, não no
    cabeçalho — ver `DesmembramentoXlsParser`.
    """
    vistos: list[str] = []
    for bruto in [order.header.customer_cnpj, *(i.delivery_cnpj for i in order.items)]:
        c = cnpj_digits(bruto)
        if c and c not in vistos:
            vistos.append(c)
    return vistos


def ambiente_para(order: Order, deps: Deps) -> Decisao:
    """Resolve o ambiente do pedido. Nunca chuta."""
    cnpjs = cnpjs_do_pedido(order)
    cliente = cnpjs[0] if cnpjs else ""

    lembrado = deps.memoria(cliente) if cliente else None

    # Degrau 1 — o documento.
    fornecedor = cnpj_digits(order.header.supplier_cnpj)
    if fornecedor:
        env = deps.env_por_cnpj(fornecedor)
        if env:
            return Decisao(
                env_slug=env,
                degrau="documento",
                explicacao=(
                    f"Fornecedor {_fmt(fornecedor)} no pedido → ambiente {env}"
                ),
                divergiu_de=lembrado if lembrado and lembrado != env else None,
            )

    # Degrau 2 — o histórico, e só quando ele é inequívoco.
    hist = deps.historico(cnpjs) if cnpjs else ()
    do_historico = _resolver_historico(hist)
    if do_historico:
        h = next(x for x in hist if x.env_slug == do_historico)
        outros = [x for x in hist if x.env_slug != do_historico and x.pedidos > 0]
        extra = (
            "; atenção, este cliente também já comprou de "
            + ", ".join(f"{o.env_nome} ({o.pedidos}, até {o.ultimo_em})" for o in outros)
            if outros else ""
        )
        return Decisao(
            env_slug=do_historico,
            degrau="historico",
            explicacao=(
                f"Histórico: {h.pedidos} pedido(s) em {h.env_nome}, "
                f"último em {h.ultimo_em}{extra}"
            ),
            divergiu_de=lembrado if lembrado and lembrado != do_historico else None,
            historico=tuple(hist),
        )

    # Degrau 3 — a memória.
    if lembrado:
        return Decisao(
            env_slug=lembrado,
            degrau="memoria",
            explicacao=f"Escolha registrada para este cliente → ambiente {lembrado}",
            historico=tuple(hist),
        )

    # Degrau 4 — sem resposta. É um resultado, não uma falha.
    motivo = (
        "cliente já comprou de mais de uma empresa na janela de 12 meses"
        if len([h for h in hist if h.pedidos > 0]) > 1
        else "o pedido não traz o CNPJ do fornecedor e o cliente não tem histórico"
    )
    return Decisao(
        env_slug=None,
        degrau="perguntar",
        explicacao=f"Ambiente não resolvido: {motivo}",
        historico=tuple(hist),
    )


def deps_padrao() -> Deps:
    """As três leituras reais. Import local: mantém o módulo puro para teste."""
    from app.persistence import decisao_ambiente_repo, environments_repo
    from app.routing import historico as hist_mod


    def env_por_cnpj(cnpj: str) -> str | None:
        env = environments_repo.find_by_cnpj(cnpj)
        return env["slug"] if env else None

    def memoria(cnpj: str) -> str | None:
        d = decisao_ambiente_repo.lembrada(cnpj)
        return d["env_slug"] if d else None

    return Deps(
        env_por_cnpj=env_por_cnpj,
        historico=lambda cnpjs: hist_mod.consultar(cnpjs),
        memoria=memoria,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_ambiente.py -v`
Expected: PASS

- [ ] **Step 5: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/routing/ambiente.py tests/test_routing_ambiente.py
git commit -m "feat(routing): a escada documento > historico > memoria > perguntar"
```

---

### Task 10: Ligar o roteamento no commit do preview

O ponto mais delicado do plano inteiro. Duas armadilhas, e as duas derrubam a feature em
silêncio se você não souber que existem:

**Armadilha 1 — `entry["environment_id"]` não basta.** `imports` mora em
`app_state_<slug>.db`, e `db.connect()` resolve o arquivo pelo **contextvar**, não pelo
dict. Setar só o `environment_id` grava a linha na DB do ambiente do cookie com o `id` do
ambiente roteado: pior que errado, incoerente. O bloco de persistência inteiro
(`insert_import` + `append_audit` + `transition`) tem que rodar dentro de
`env_context.active_env(env_alvo["id"], env_alvo["slug"])`.

**Armadilha 2 — o arquivo original segue o pedido.** `_get_cfg_for_request` monta
`watch_dir`/`output_dir` a partir do ambiente do **cookie**. Com o roteamento valendo, o
`shutil.move` do fim do handler tem que usar as pastas do ambiente **roteado**, senão o
arquivo da Nasmar vai para a pasta `Pedidos importados` da MM.

**Files:**
- Modify: `app/web/server.py` (`CommitRequest`, `commit_preview`, `_build_preview_payload`, `_run_preview_routing`)
- Test: `tests/test_routing_wiring.py`

**Interfaces:**
- Consumes: `roteamento_repo.modo/registrar_sombra`, `ambiente.ambiente_para/deps_padrao`,
  `decisao_ambiente_repo.lembrar/marcar_divergencia`, `environments_repo.get_by_slug`.
- Produces: `CommitRequest.environment_slug: str | None = None`; chave `"roteamento"` no
  payload do preview (`{degrau, env_slug, env_nome, explicacao, precisa_escolher,
  opcoes: [{slug, nome}]}` ou `None` quando o modo é `desligado`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_wiring.py
"""O wiring do roteamento no /api/commit, nos tres modos.

O teste que protege a adocao e o primeiro: em 'desligado' nada muda.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_repo, router
from app.web.server import app

NASMAR = "34513679000134"
MM = "35394871000111"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    nasmar = environments_repo.create(
        slug="nasmar", name="Nasmar", cnpj=NASMAR,
        watch_dir=str(tmp_path / "n_in"), output_dir=str(tmp_path / "n_out"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    mm = environments_repo.create(
        slug="mm", name="MM Americanense", cnpj=MM,
        watch_dir=str(tmp_path / "m_in"), output_dir=str(tmp_path / "m_out"),
        fb_path=str(tmp_path / "m.fdb"),
    )
    yield {"client": TestClient(app), "nasmar": nasmar, "mm": mm}


def _order(supplier_cnpj):
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(order_number="4711", customer_cnpj="11222333000181",
                           customer_name="DAJU", supplier_cnpj=supplier_cnpj),
        items=[OrderItem(description="Meia Kit 3", quantity=10, unit_price=11.96)],
    )


def _preview_com_fornecedor(portal, supplier_cnpj):
    """Poe um Order no cache de preview e devolve o preview_id.

    `put()` exige os bytes e a extensao do arquivo original (assinatura em
    app/web/preview_cache.py:48) e devolve o PreviewEntry, nao o id.
    """
    from app.web.preview_cache import get_cache

    entry = get_cache().put(
        order=_order(supplier_cnpj),
        source_filename="PEDIDO.pdf",
        source_bytes=b"%PDF-1.4 fake",
        source_ext=".pdf",
        check=None,
    )
    return entry.preview_id


def _import_do_ambiente(slug):
    from app.persistence import context as env_context
    from app.persistence import repo

    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        return repo.list_imports(limit=50)


def test_desligado_nao_muda_nada(portal):
    """Cookie da MM, pedido com fornecedor NASMAR: entra na MM, como hoje."""
    roteamento_repo.set_modo("desligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    r = c.post("/api/commit", json={"preview_id": pid})
    assert r.status_code == 200
    assert len(_import_do_ambiente("mm")) == 1
    assert _import_do_ambiente("nasmar") == []
    assert roteamento_repo.taxa()["total"] == 0  # nem sombra grava


def test_observando_grava_sombra_e_a_escolha_do_operador_prevalece(portal):
    roteamento_repo.set_modo("observando", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    assert len(_import_do_ambiente("mm")) == 1      # o operador venceu
    assert _import_do_ambiente("nasmar") == []
    t = roteamento_repo.taxa()
    assert t["total"] == 1
    assert t["bateu"] == 0                          # e a divergencia ficou registrada
    assert t["divergiu"] == 1


def test_ligado_manda_o_pedido_para_o_ambiente_do_documento(portal):
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    nas = _import_do_ambiente("nasmar")
    assert len(nas) == 1
    assert nas[0]["order_number"] == "4711"
    assert _import_do_ambiente("mm") == []


def test_ligado_sem_resposta_pede_escolha_em_vez_de_chutar(portal):
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, None)
    r = c.post("/api/commit", json={"preview_id": pid})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["precisa_escolher"] is True
    assert {o["slug"] for o in body["opcoes"]} == {"nasmar", "mm"}
    assert _import_do_ambiente("mm") == []
    assert _import_do_ambiente("nasmar") == []


def test_escolha_do_operador_e_gravada_com_autor(portal):
    from app.persistence import decisao_ambiente_repo as memoria

    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, None)
    r = c.post("/api/commit", json={"preview_id": pid, "environment_slug": "nasmar"})
    assert r.status_code == 200
    assert len(_import_do_ambiente("nasmar")) == 1
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "nasmar"
    assert d["decidido_por"]


def test_bloco_de_roteamento_do_preview(portal):
    """O payload do preview carrega a decisao — a UI nunca roteia em silencio.

    Testado na funcao, nao numa rota: o payload so e montado dentro de
    POST /api/preview (upload) e POST /api/preview-pending (arquivo na pasta).
    Nao existe GET de preview por id, e nao e este plano que vai criar um.
    """
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("observando", por="t")
    rot = _roteamento_para_preview(_order(NASMAR))
    assert rot["degrau"] == "documento"
    assert rot["env_slug"] == "nasmar"
    assert rot["precisa_escolher"] is False
    assert "34.513.679/0001-34" in rot["explicacao"]


def test_bloco_de_roteamento_e_none_quando_desligado(portal):
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("desligado", por="t")
    assert _roteamento_para_preview(_order(NASMAR)) is None


def test_bloco_de_roteamento_pede_escolha_quando_ligado_e_mudo(portal):
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("ligado", por="t")
    rot = _roteamento_para_preview(_order(None))
    assert rot["precisa_escolher"] is True
    assert {o["slug"] for o in rot["opcoes"]} == {"nasmar", "mm"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_wiring.py -v`
Expected: FAIL — `test_desligado_nao_muda_nada` passa (é o comportamento de hoje) e os
demais falham. Se o de `desligado` falhar, pare: a fundação está errada.

- [ ] **Step 3: Write minimal implementation**

Em `app/web/server.py`, o modelo do request:

```python
class CommitRequest(BaseModel):
    preview_id: str
    # Resposta do operador quando o roteamento não soube decidir. Só é lida
    # com roteamento_modo='ligado'; nos outros modos o ambiente é o do cookie.
    environment_slug: str | None = None
```

O helper da decisão, junto dos outros helpers de preview:

```python
def _decidir_ambiente(order) -> tuple[str, object | None]:
    """(modo, Decisao|None). Em 'desligado' o roteador nem é chamado."""
    from app.persistence import roteamento_repo
    from app.routing import ambiente as routing

    modo = roteamento_repo.modo()
    if modo == roteamento_repo.DESLIGADO:
        return modo, None
    return modo, routing.ambiente_para(order, routing.deps_padrao())


def _roteamento_para_preview(order) -> dict | None:
    """Bloco `roteamento` do payload do preview. `None` em 'desligado'."""
    from app.persistence import environments_repo, roteamento_repo

    modo, decisao = _decidir_ambiente(order)
    if decisao is None:
        return None
    env = environments_repo.get_by_slug(decisao.env_slug) if decisao.env_slug else None
    return {
        "modo": modo,
        "degrau": decisao.degrau,
        "env_slug": decisao.env_slug,
        "env_nome": env["name"] if env else None,
        "explicacao": decisao.explicacao,
        "precisa_escolher": modo == roteamento_repo.LIGADO and not decisao.resolveu,
        "opcoes": [
            {"slug": e["slug"], "name": e["name"]}
            for e in environments_repo.list_active()
        ],
    }
```

Somar ao dict devolvido por `_build_preview_payload`:

```python
        "check": check,
        "roteamento": _roteamento_para_preview(order),
```

E o corpo de `commit_preview`, substituindo do `order = entry.order` até o fim do bloco
`with with_trace_id()`:

```python
    order = entry.order

    from app.persistence import context as env_context
    from app.persistence import decisao_ambiente_repo as memoria
    from app.persistence import environments_repo, roteamento_repo
    from app.routing.ambiente import cnpjs_do_pedido

    modo, decisao = _decidir_ambiente(order)
    env_alvo = _request_environment(request)

    if modo == roteamento_repo.LIGADO:
        if decisao.resolveu:
            env_alvo = environments_repo.get_by_slug(decisao.env_slug)
        elif body.environment_slug:
            env_alvo = environments_repo.get_by_slug(body.environment_slug)
        else:
            # Sem resposta NÃO vira ambiente default. Pergunta.
            raise HTTPException(
                status_code=409,
                detail={
                    "precisa_escolher": True,
                    "explicacao": decisao.explicacao,
                    "degrau": decisao.degrau,
                    "opcoes": [
                        {"slug": e["slug"], "name": e["name"]}
                        for e in environments_repo.list_active()
                    ],
                },
            )

    if env_alvo is None or not env_alvo.get("is_active"):
        raise HTTPException(status_code=412, detail="Selecione um ambiente para continuar.")

    # As pastas seguem o pedido, não o cookie — senão o arquivo original da
    # Nasmar termina em `Pedidos importados` da MM.
    cfg = dict(cfg)
    cfg["watch_dir"] = env_alvo["watch_dir"]
    cfg["output_dir"] = env_alvo["output_dir"]

    cnpj_cliente = (cnpjs_do_pedido(order) or [""])[0]

    # `imports` vive em app_state_<slug>.db: o bind do ambiente é o contextvar,
    # não o dict do entry. Todo o bloco de persistência roda aqui dentro.
    with env_context.active_env(env_alvo["id"], env_alvo["slug"]):
        with with_trace_id() as trace_id:
            log_entry = _make_log_entry(...)          # inalterado
            log_entry["portal_status"] = "parsed"
            log_entry["check"] = entry.check
            log_entry["environment_id"] = env_alvo["id"]

            from app.persistence import repo

            repo.insert_import(log_entry)
            repo.append_audit(...)                    # inalterado
            transition(...)                           # inalterado

            if decisao is not None:
                if modo == roteamento_repo.OBSERVANDO:
                    roteamento_repo.registrar_sombra(
                        import_id=log_entry["id"],
                        degrau=decisao.degrau,
                        env_sugerido=decisao.env_slug,
                        env_escolhido=env_alvo["slug"],
                    )
                if decisao.divergiu_de and cnpj_cliente:
                    memoria.marcar_divergencia(
                        cnpj_cliente=cnpj_cliente, de=decisao.divergiu_de
                    )
                if (modo == roteamento_repo.LIGADO and not decisao.resolveu
                        and cnpj_cliente):
                    memoria.lembrar(
                        cnpj_cliente=cnpj_cliente,
                        env_slug=env_alvo["slug"],
                        por=user.email,
                    )

            # ... o move do arquivo original, inalterado, usando o `cfg` acima
```

E a assinatura do handler passa a usar o usuário: troque `_user: User = Depends(require_user)`
por `user: User = Depends(require_user)` — o autor da decisão precisa ir para a memória.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_wiring.py -v`
Expected: PASS

- [ ] **Step 5: A suíte web inteira, sem regressão**

Run: `.venv/bin/pytest tests/ -v -k "web or commit or preview"`
Expected: PASS. Como o default é `'desligado'`, nenhum teste existente deve precisar de
ajuste. **Se algum precisar, leia antes de mexer**: é sinal de que o caminho `desligado`
não está mesmo idêntico ao de hoje.

- [ ] **Step 6: Mostrar a decisão na tela do preview**

Em `app/web/static/index.html` (modal de preview), uma faixa acima da lista de itens,
renderizada só quando `payload.roteamento` não é `null` — o payload vem das respostas de
`POST /api/preview` e `POST /api/preview-pending`, que são as duas únicas rotas que
montam preview hoje:

- resolvido: `Fornecedor NASMAR (34.513.679/0001-34) → este pedido entra no ambiente NASMAR`,
  com o degrau em texto menor (`pelo documento` / `pelo histórico` / `escolha registrada`).
- `precisa_escolher`: um `<select>` obrigatório com `opcoes`, e o botão de confirmar
  desabilitado até escolher. O valor vai em `environment_slug` no POST de `/api/commit`.
- `divergiu_de` presente: um aviso discreto — *"a escolha registrada para este cliente era
  outra"*. Aviso, não bloqueio.

Roteamento silencioso é como o bug de hoje nasceu; a decisão aparece **sempre**, mesmo
quando é automática.

- [ ] **Step 7: Lint, suíte completa, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/web/ tests/test_routing_wiring.py
git commit -m "feat(routing): o commit do preview roteia o pedido (atras do interruptor)"
```

---

### Task 11: Ligar o roteamento no watcher, com retenção

No watcher não há humano para perguntar. Pedido que cair em `perguntar` **não é
importado** — e, principalmente, **não vai para um ambiente default**.

A retenção aqui é deliberadamente simples: o arquivo **fica onde está**, na pasta de
entrada, e ganha uma linha em `roteamento_pendencia`. Não inventamos uma tela de
resolução: o caminho para um humano responder já existe e é o preview. O que faltava era
visibilidade, e é isso que a fila dá.

**Files:**
- Modify: `app/worker/jobs/scan_environments.py`
- Test: `tests/test_scan_environments_roteamento.py`

**Interfaces:**
- Consumes: `roteamento_repo.modo/registrar_pendencia`, `ambiente.ambiente_para/deps_padrao`,
  `environments_repo.get_by_slug`, `env_context.active_env`.
- Produces: nada novo — muda o comportamento de `_process_file`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scan_environments_roteamento.py
from __future__ import annotations

import pytest

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.worker.jobs import scan_environments

NASMAR = "34513679000134"
MM = "35394871000111"


@pytest.fixture
def dois_ambientes(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome, cnpj in (("nasmar", "Nasmar", NASMAR), ("mm", "MM", MM)):
        d = tmp_path / slug
        (d / "in").mkdir(parents=True)
        (d / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug, name=nome, cnpj=cnpj,
            watch_dir=str(d / "in"), output_dir=str(d / "out"),
            fb_path=str(d / f"{slug}.fdb"),
        )
    yield tmp_path


def _imports(slug):
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        return repo.list_imports(limit=50)


def _order(supplier):
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(order_number="4711", customer_cnpj="11222333000181",
                           customer_name="DAJU", supplier_cnpj=supplier),
        items=[OrderItem(description="Meia", quantity=5, unit_price=10.0)],
    )


def _arquivo(tmp_path, slug="mm", nome="PEDIDO.pdf"):
    p = tmp_path / slug / "in" / nome
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_desligado_importa_no_ambiente_da_pasta(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("desligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("mm")) == 1     # a pasta manda, como hoje
    assert _imports("nasmar") == []


def test_ligado_importa_no_ambiente_do_documento(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("nasmar")) == 1
    assert _imports("mm") == []


def test_ligado_sem_resposta_retem_o_arquivo_e_nao_importa(dois_ambientes, monkeypatch):
    """Nunca um ambiente default. O arquivo fica na pasta e vira pendencia."""
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(None))
    p = _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert _imports("mm") == []
    assert _imports("nasmar") == []
    assert p.exists(), "o arquivo tem que continuar na pasta de entrada"
    assert roteamento_repo.contar_pendencias() == 1


def test_pendencia_nao_infla_a_cada_varredura(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(None))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    scan_environments.run_scan()
    scan_environments.run_scan()
    assert roteamento_repo.contar_pendencias() == 1
    assert roteamento_repo.listar_pendencias()[0]["visto_vezes"] == 3


def test_observando_importa_na_pasta_e_grava_sombra(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("observando", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("mm")) == 1
    t = roteamento_repo.taxa()
    assert t["total"] == 1 and t["divergiu"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_scan_environments_roteamento.py -v`
Expected: FAIL nos três testes de `ligado`/`observando`; o de `desligado` passa.

- [ ] **Step 3: Write minimal implementation**

Em `app/worker/jobs/scan_environments.py`, dentro de `_process_file`, depois de `order`
estar pronto e **antes** de montar o `entry` de sucesso:

```python
        from app.persistence import environments_repo, roteamento_repo
        from app.routing import ambiente as routing

        env_alvo = env
        modo = roteamento_repo.modo()
        decisao = None
        if modo != roteamento_repo.DESLIGADO:
            decisao = routing.ambiente_para(order, routing.deps_padrao())

        if modo == roteamento_repo.LIGADO:
            if decisao.resolveu:
                env_alvo = environments_repo.get_by_slug(decisao.env_slug) or env
            else:
                # Sem humano para perguntar: retém. O arquivo NÃO se move e
                # NÃO é importado — fica na pasta, onde o operador pode abri-lo
                # pelo preview e responder.
                roteamento_repo.registrar_pendencia(
                    sha256=sha,
                    source_path=str(p),
                    env_scan_slug=env["slug"],
                    order_number=order.header.order_number,
                    customer_cnpj=order.header.customer_cnpj,
                    customer_name=order.header.customer_name,
                )
                logger.info(
                    "scan.retido_sem_ambiente env={} file={} motivo={}",
                    env["slug"], p.name, decisao.explicacao,
                )
                return
```

O `entry` ganha `"environment_id": env_alvo["id"]`, e a persistência passa a rodar dentro
do ambiente decidido — mesma armadilha da Task 10:

```python
        with env_context.active_env(env_alvo["id"], env_alvo["slug"]):
            try:
                repo.insert_import(entry)
                if modo == roteamento_repo.OBSERVANDO and decisao is not None:
                    roteamento_repo.registrar_sombra(
                        import_id=import_id,
                        degrau=decisao.degrau,
                        env_sugerido=decisao.env_slug,
                        env_escolhido=env["slug"],
                    )
                logger.info(
                    "scan.imported env={} file={} order={} import_id={}",
                    env_alvo["slug"], p.name, order.header.order_number, import_id,
                )
                _move_to_imported(p, watch_dir)
            except Exception as e:
                logger.error("scan.insert_failed env={} file={} {!r}", env["slug"], p.name, e)
```

O `_move_to_imported` continua usando o `watch_dir` **da pasta varrida** — o arquivo veio
de lá, e é lá que fica o histórico de entrada. Só a linha em `imports` muda de ambiente.

E o check de duplicidade passa a olhar **todos** os ambientes, senão o mesmo arquivo
roteado para a Nasmar seria reimportado a cada varredura da pasta da MM. A função perde o
parâmetro `slug`, então **atualize também a chamada** no topo de `_process_file`
(`if _already_imported(env["slug"], sha):` → `if _already_imported(sha):`):

```python
def _already_imported(sha: str) -> bool:
    """O sha é único no Portal inteiro, não por ambiente: com roteamento, o
    arquivo pode ter entrado numa empresa diferente da pasta em que está."""
    for slug in router.list_env_slugs():
        with router.env_connect(slug) as conn:
            if conn.execute(
                "SELECT 1 FROM imports WHERE file_sha256 = ? LIMIT 1", (sha,)
            ).fetchone():
                return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_scan_environments_roteamento.py -v`
Expected: PASS

- [ ] **Step 5: A suíte do worker, sem regressão**

Run: `.venv/bin/pytest tests/ -v -k "scan or worker"`
Expected: PASS

- [ ] **Step 6: Lint, suíte completa, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/worker/ tests/test_scan_environments_roteamento.py
git commit -m "feat(routing): watcher roteia e RETEM o que nao sabe (nunca ambiente default)"
```

---

# FASE 1a — o CNPJ do cliente nas planilhas de desmembramento

**Correção à spec, encontrada ao escrever este plano.** O fato 15 diz que os 3 samples que
sobram falham por "lacuna de parser". Medido nos arquivos, a lacuna é de **um**:

| sample | tem CNPJ no arquivo? |
|---|---|
| `Desmembramento Magic Feet.xlsx` | **sim** — 8 CNPJs de loja na linha acima do cabeçalho |
| `Desmembramento Authentic feet (1).xlsx` | **não** — só nomes de shopping e códigos `AF011`, `AF013`, … |
| `PEDIDO NBA 3.xlsx` | **não** — só nomes de loja com prefixo numérico |

Verificado varrendo todas as abas dos três arquivos com a regex de CNPJ: zero ocorrências
nos dois últimos. Então esta fase entrega **Magic Feet**, e Authentic Feet e NBA continuam
caindo em `perguntar` — uma vez cada, porque a resposta vira memória e, depois do primeiro
pedido no Fire, vira histórico. O desenho já cobre isso; o que muda é que a spec deve
dizer 1 de 3, não 3 de 3.

---

### Task 12: `customer_cnpj` derivado das colunas de loja

**Files:**
- Modify: `app/parsers/desmembramento_xls_parser.py`
- Test: `tests/test_desmembramento_customer_cnpj.py`

**Interfaces:**
- Produces: `DesmembramentoXlsParser._derive_customer_cnpj(store_cols) -> str | None`,
  e `OrderHeader.customer_cnpj` preenchido quando as colunas de loja trazem CNPJ.

**A regra:** numa planilha de desmembramento não existe *"o CNPJ do cliente"* — existe uma
lista de lojas. Quando a maioria compartilha a mesma raiz de 8 dígitos, elas são filiais da
mesma empresa, e essa empresa é o comprador. O representante é a filial de **menor sufixo**:
determinístico, e estável entre planilhas do mesmo cliente. Sem maioria clara, `None` — não
se inventa comprador.

O roteamento não depende só disso: `cnpjs_do_pedido` (Task 9) já manda **todos** os
`delivery_cnpj` para o histórico. Esta task melhora a identificação do pedido na UI e no
`imports.customer_cnpj`, e dá ao degrau da memória uma chave estável.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_desmembramento_customer_cnpj.py
from __future__ import annotations

from pathlib import Path

import pytest

from app.extractors.xls_extractor import XLSExtractor
from app.ingestion.file_loader import LoadedFile
from app.parsers.desmembramento_xls_parser import DesmembramentoXlsParser

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _parse(nome):
    p = SAMPLES / nome
    raw = p.read_bytes()
    extracted = XLSExtractor().extract(
        LoadedFile(path=p, extension=p.suffix.lower(), raw=raw)
    )
    return DesmembramentoXlsParser().parse(extracted)


def test_deriva_por_raiz_majoritaria_e_menor_sufixo():
    p = DesmembramentoXlsParser()
    cols = [
        (10, "Loja A", "05.055.599/0029-85"),
        (11, "Loja B", "05.055.599/0008-50"),
        (12, "Loja C", "05.055.599/0026-32"),
        (13, "Outra",  "10.389.941/0001-12"),
    ]
    assert p._derive_customer_cnpj(cols) == "05055599000850"


def test_sem_cnpj_nenhum_devolve_none():
    p = DesmembramentoXlsParser()
    assert p._derive_customer_cnpj([(10, "SHOPPING CENTER NORTE", None)]) is None
    assert p._derive_customer_cnpj([]) is None


def test_empate_de_raizes_nao_inventa_comprador():
    p = DesmembramentoXlsParser()
    cols = [
        (10, "A", "05.055.599/0029-85"),
        (11, "B", "10.389.941/0001-12"),
    ]
    assert p._derive_customer_cnpj(cols) is None


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_magic_feet_passa_a_ter_cnpj_de_cliente():
    order = _parse("Desmembramento Magic Feet.xlsx")
    assert order is not None
    assert order.header.customer_cnpj == "05055599000850"


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_authentic_feet_e_nba_seguem_sem_cnpj_e_isso_esta_certo():
    """Os arquivos nao trazem CNPJ nenhum. Cair em 'perguntar' e o desenho,
    nao um bug — a resposta do operador vira memoria e depois historico."""
    for nome in ("Desmembramento Authentic feet (1).xlsx", "PEDIDO NBA 3.xlsx"):
        order = _parse(nome)
        assert order is not None, nome
        assert order.header.customer_cnpj is None, nome


@pytest.mark.skipif(not SAMPLES.is_dir(), reason="samples/ não está no checkout")
def test_itens_nao_mudam():
    """Diff minimo: a derivacao do cabecalho nao pode mexer nos itens."""
    order = _parse("Desmembramento Magic Feet.xlsx")
    assert len(order.items) > 0
    assert all(i.quantity and i.quantity > 0 for i in order.items)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_desmembramento_customer_cnpj.py -v`
Expected: FAIL — `AttributeError: 'DesmembramentoXlsParser' object has no attribute '_derive_customer_cnpj'`

- [ ] **Step 3: Write minimal implementation**

Em `app/parsers/desmembramento_xls_parser.py`, o método novo:

```python
    def _derive_customer_cnpj(self, store_cols: list) -> str | None:
        """CNPJ do comprador a partir das colunas de loja.

        Numa planilha de desmembramento não existe "o CNPJ do cliente" — existe
        uma lista de lojas. Quando a maioria compartilha a mesma raiz de 8
        dígitos, são filiais da mesma empresa e essa empresa é o comprador.
        Representante = a filial de menor sufixo: determinístico e estável entre
        planilhas do mesmo cliente.

        Sem maioria, `None`. Inventar comprador aqui roteia pedido errado lá na
        frente.
        """
        from collections import Counter

        from app.erp.cnpj import cnpj_digits

        cnpjs = [c for c in (cnpj_digits(x[2]) for x in store_cols) if len(c) == 14]
        if not cnpjs:
            return None
        raizes = Counter(c[:8] for c in cnpjs)
        (raiz, n), *resto = raizes.most_common()
        if resto and resto[0][1] == n:
            return None
        return min(c for c in cnpjs if c.startswith(raiz))
```

E em `parse`, trocar a construção do header:

```python
        order_header = OrderHeader(
            order_number=order_number,
            customer_cnpj=self._derive_customer_cnpj(store_cols),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_desmembramento_customer_cnpj.py -v`
Expected: PASS

- [ ] **Step 5: Parsers sem regressão**

Run: `.venv/bin/pytest tests/ -v -k "desmembramento or parser"`
Expected: PASS

- [ ] **Step 6: Corrigir o fato 15 da spec**

Em `docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`, na tabela
do fato 15, trocar as três linhas `o parser não extrai CNPJ do cliente` por:

| sample | Nasmar | MM | veredito |
|---|---|---|---|
| `Desmembramento Magic Feet` | — | — | resolvido na Fase 1a (raiz `05055599`) |
| `Desmembramento Authentic feet` | — | — | **o arquivo não tem CNPJ nenhum** — vai para `perguntar` |
| `PEDIDO NBA 3.xlsx` | — | — | **o arquivo não tem CNPJ nenhum** — vai para `perguntar` |

E ajustar a frase seguinte: não é lacuna de parser nos três, é lacuna de parser em **um**;
nos outros dois o documento é mudo, e o desenho já responde a isso.

- [ ] **Step 7: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/parsers/desmembramento_xls_parser.py tests/test_desmembramento_customer_cnpj.py docs/superpowers/specs/
git commit -m "feat(parsers): cliente do desmembramento pela raiz das lojas (Magic Feet)"
```

---

# FASE 1c — a tela que autoriza virar a chave

Vem antes da Fase 1b de propósito: é a taxa de acerto medida aqui que decide se o
roteamento pode ir para `ligado`, e só faz sentido tirar a seleção de ambiente do login
depois disso.

---

### Task 13: `/admin/roteamento` — interruptor, taxa de acerto e pendências

**Files:**
- Create: `app/web/routes_roteamento.py`, `app/web/static/admin-roteamento.html`
- Modify: `app/web/server.py` (`include_router`, rota da página), `app/web/static/js/shell.js`
- Test: `tests/test_routing_modo.py`

**Interfaces:**
- Consumes: `roteamento_repo` inteiro, `decisao_ambiente_repo.listar`, `require_admin`.
- Produces: `GET /api/roteamento/modo` → `{"modo": ...}`;
  `PUT /api/roteamento/modo` body `{"modo": "..."}` (admin);
  `GET /api/roteamento/taxa?dias=30` → `taxa()` + `divergencias` (últimas 50 linhas de
  sombra com `bateu = 0`) + `pendencias`; página `GET /admin/roteamento`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_modo.py
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import roteamento_repo, router
from app.web.server import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    yield TestClient(app)


def test_get_modo_de_instalacao_nova(client):
    assert client.get("/api/roteamento/modo").json()["modo"] == "desligado"


def test_put_modo_muda_sem_deploy(client):
    r = client.put("/api/roteamento/modo", json={"modo": "observando"})
    assert r.status_code == 200
    assert client.get("/api/roteamento/modo").json()["modo"] == "observando"
    assert roteamento_repo.modo() == "observando"


def test_put_modo_invalido_e_400(client):
    r = client.put("/api/roteamento/modo", json={"modo": "talvez"})
    assert r.status_code == 400
    assert roteamento_repo.modo() == "desligado"


def test_taxa_devolve_numeros_e_divergencias(client):
    roteamento_repo.registrar_sombra(import_id="a", degrau="documento",
                                     env_sugerido="nasmar", env_escolhido="nasmar")
    roteamento_repo.registrar_sombra(import_id="b", degrau="documento",
                                     env_sugerido="nasmar", env_escolhido="mm")
    body = client.get("/api/roteamento/taxa?dias=30").json()
    assert body["total"] == 2
    assert body["bateu"] == 1
    assert body["divergiu"] == 1
    assert [d["import_id"] for d in body["divergencias"]] == ["b"]


def test_taxa_lista_pendencias_do_watcher(client):
    roteamento_repo.registrar_pendencia(
        sha256="s1", source_path="/in/NBA.xlsx", env_scan_slug="mm",
        order_number="NBA 3", customer_cnpj=None, customer_name=None,
    )
    body = client.get("/api/roteamento/taxa").json()
    assert body["pendencias"][0]["order_number"] == "NBA 3"


def test_pagina_do_admin_responde(client):
    assert client.get("/admin/roteamento").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_modo.py -v`
Expected: FAIL — 404 em todas as rotas.

- [ ] **Step 3: Write minimal implementation**

```python
# app/web/routes_roteamento.py
"""Interruptor do roteamento e a evidência que autoriza virar a chave.

Três estados porque o problema é de sequência: não dá para validar o que não
está rodando, nem ligar o que não foi validado. `observando` roda o roteador e
grava o que ele teria feito, sem agir — e as **divergências** que saem daí são
o material de treinamento do time.

Quem vira a chave é admin. Não é configuração de operador.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.persistence import roteamento_repo
from app.persistence import router as db_router
from app.web.auth import User, require_admin, require_user

router = APIRouter()


class ModoRequest(BaseModel):
    modo: str


@router.get("/api/roteamento/modo")
def get_modo(_=Depends(require_user)):
    return {"modo": roteamento_repo.modo(), "modos": list(roteamento_repo.MODOS)}


@router.put("/api/roteamento/modo")
def put_modo(payload: ModoRequest, user: User = Depends(require_admin)):
    try:
        roteamento_repo.set_modo(payload.modo, por=user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"modo": roteamento_repo.modo()}


@router.get("/api/roteamento/taxa")
def get_taxa(dias: int = 30, _=Depends(require_user)):
    t = roteamento_repo.taxa(dias=dias)
    with db_router.shared_connect() as conn:
        rows = conn.execute(
            """SELECT import_id, decidido_em, degrau, env_sugerido,
                      env_escolhido_pelo_operador
               FROM roteamento_sombra
               WHERE bateu = 0 AND degrau <> 'perguntar' AND decidido_em >= ?
               ORDER BY decidido_em DESC LIMIT 50""",
            (t["desde"],),
        ).fetchall()
    t["divergencias"] = [
        {
            "import_id": r[0], "decidido_em": r[1], "degrau": r[2],
            "env_sugerido": r[3], "env_escolhido": r[4],
        }
        for r in rows
    ]
    t["pendencias"] = roteamento_repo.listar_pendencias(limit=50)
    return t
```

Em `app/web/server.py`, ao lado dos outros `include_router`:

```python
from app.web.routes_roteamento import router as roteamento_router

app.include_router(roteamento_router)
```

E a rota da página, junto das outras de `/admin`:

```python
@app.get("/admin/roteamento")
def admin_roteamento_page(request: Request):
    """Interruptor do roteamento + taxa de acerto (admin-only). API enforce o role."""
    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    return FileResponse(str(STATIC_DIR / "admin-roteamento.html"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_modo.py -v`
Expected: PASS

- [ ] **Step 5: A tela**

`app/web/static/admin-roteamento.html`, seguindo o padrão visual de
`admin-ambientes.html` (mesma shell, mesma tipografia, dark-first). Três blocos:

1. **O interruptor** — três opções em linha, com o estado atual destacado e uma frase
   por estado, exatamente estas:
   - `desligado` — *"nada muda. O operador escolhe a empresa no login, como sempre."*
   - `observando` — *"o Portal calcula a decisão e registra o que teria feito. A escolha do operador continua valendo."*
   - `ligado` — *"a decisão do Portal vale. A seleção de ambiente some do login."*

   Trocar para `ligado` pede confirmação; voltar para `observando` ou `desligado`, não —
   recuar tem que ser barato.

2. **A taxa** — uma frase, não um dashboard: *"nos últimos 30 dias, 214 pedidos: 209
   bateram, 3 o Portal não soube responder, 2 divergiram"*. Abaixo, a tabela das
   divergências (pedido, degrau, sugerido, escolhido) — **é ela o material de
   treinamento**, então é ela que ocupa o espaço.

3. **Pendências do watcher** — arquivos retidos, com pasta de origem e há quanto tempo.
   Vazio é o estado normal e a tela deve dizer isso em vez de mostrar tabela vazia.

Em `app/web/static/js/shell.js`, item de menu `Roteamento` sob Configurações, visível só
para admin.

- [ ] **Step 6: Validação de design**

Rodar a skill `hm-designer` contra a tela nova antes de fechar. Nenhuma UI fecha sem
passar.

- [ ] **Step 7: Lint, suíte completa, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/web/ tests/test_routing_modo.py
git commit -m "feat(routing): tela do interruptor e da taxa de acerto"
```

---

# FASE 1b — o ambiente deixa de ser um modo e vira propriedade do pedido

Só entra depois que `ligado` estiver valendo em produção. As duas tasks são gated por
`roteamento_modo == 'ligado'`: com o roteamento desligado, o login continua exatamente
como hoje.

---

### Task 14: Listagem que enxerga todos os ambientes

**Files:**
- Modify: `app/persistence/repo.py`
- Test: `tests/test_imports_cross_env.py`

**Interfaces:**
- Produces: `repo.list_imports_all_envs(**kwargs) -> list[dict]` — mesmos filtros de
  `list_imports`, cada linha com `env_slug` e `env_name` a mais;
  `repo.count_imports_all_envs(**kwargs) -> int`;
  `repo.count_by_portal_status_all_envs(**kwargs) -> dict[str, int]`.

**Os três andam juntos, sempre.** Os contadores dos chips vão na mesma resposta da
listagem justamente para nunca divergirem dela (`repo.count_by_portal_status`, docstring).
Somar a lista e não somar os chips reintroduziria o bug que aquele comentário descreve —
lista com 12 e chip com 308.

**Por que em Python e não em SQL:** são bancos SQLite **separados**, um por empresa. Não
há JOIN possível sem `ATTACH`, e `ATTACH` amarraria o roteador ao layout de arquivos. O
volume torna isso um não-problema: a produção tem ~108 pedidos (auditoria de 24/08).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_imports_cross_env.py
from __future__ import annotations

import pytest

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, router


@pytest.fixture
def dois_ambientes_com_pedidos(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("nasmar", "Nasmar"), ("mm", "MM Americanense")):
        environments_repo.create(
            slug=slug, name=nome, watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug), fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    def _inserir(slug, ident, quando, cliente):
        env = environments_repo.get_by_slug(slug)
        with env_context.active_env(env["id"], env["slug"]):
            repo.insert_import({
                "id": ident, "source_filename": f"{ident}.pdf",
                "imported_at": quando, "order_number": ident,
                "customer_name": cliente, "status": "success",
                "portal_status": "parsed",
            })
    _inserir("nasmar", "N1", "2026-09-01T10:00:00", "DAJU")
    _inserir("mm", "M1", "2026-09-02T10:00:00", "CENTAURO")
    _inserir("nasmar", "N2", "2026-09-03T10:00:00", "STUDIO Z")
    yield


def test_lista_junta_os_ambientes_em_ordem_de_data(dois_ambientes_com_pedidos):
    linhas = repo.list_imports_all_envs(limit=10)
    assert [r["id"] for r in linhas] == ["N2", "M1", "N1"]


def test_cada_linha_carrega_o_selo_da_empresa(dois_ambientes_com_pedidos):
    por_id = {r["id"]: r for r in repo.list_imports_all_envs(limit=10)}
    assert por_id["N1"]["env_slug"] == "nasmar"
    assert por_id["N1"]["env_name"] == "Nasmar"
    assert por_id["M1"]["env_name"] == "MM Americanense"


def test_paginacao_atravessa_a_fronteira_dos_ambientes(dois_ambientes_com_pedidos):
    assert [r["id"] for r in repo.list_imports_all_envs(limit=2)] == ["N2", "M1"]
    assert [r["id"] for r in repo.list_imports_all_envs(limit=2, offset=2)] == ["N1"]


def test_filtro_vale_para_todos_os_ambientes(dois_ambientes_com_pedidos):
    linhas = repo.list_imports_all_envs(limit=10, customer_search="DAJU")
    assert [r["id"] for r in linhas] == ["N1"]


def test_contagem_soma_os_ambientes(dois_ambientes_com_pedidos):
    assert repo.count_imports_all_envs() == 3
    assert repo.count_imports_all_envs(customer_search="DAJU") == 1


def test_chips_somam_os_ambientes_junto_com_a_lista(dois_ambientes_com_pedidos):
    """Chip que discorda da lista e pior que chip nenhum."""
    assert repo.count_by_portal_status_all_envs() == {"parsed": 3}


def test_sem_ambiente_nenhum_devolve_lista_vazia(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    assert repo.list_imports_all_envs() == []
    assert repo.count_imports_all_envs() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_imports_cross_env.py -v`
Expected: FAIL — `AttributeError: module 'app.persistence.repo' has no attribute 'list_imports_all_envs'`

- [ ] **Step 3: Write minimal implementation**

No fim de `app/persistence/repo.py`:

```python
def _envs_para_listagem() -> list[dict]:
    from app.persistence import environments_repo

    return environments_repo.list_active()


def list_imports_all_envs(limit: int = 100, offset: int = 0, **filtros) -> list[dict]:
    """`list_imports` somando todos os ambientes ativos, com selo da empresa.

    Merge em Python, não em SQL: são bancos SQLite separados (um arquivo por
    empresa) e não há JOIN sem `ATTACH`. Cada ambiente é consultado com
    `limit + offset` linhas — o suficiente para a página pedida, sem varrer
    tudo. Volume real: ~108 pedidos em produção.
    """
    from app.persistence import context as env_context

    teto = max(1, min(int(limit) + int(offset), _MAX_PAGE_SIZE))
    juntas: list[dict] = []
    for env in _envs_para_listagem():
        with env_context.active_env(env["id"], env["slug"]):
            for linha in list_imports(limit=teto, offset=0, **filtros):
                linha["env_slug"] = env["slug"]
                linha["env_name"] = env["name"]
                juntas.append(linha)
    juntas.sort(key=lambda r: (r.get("imported_at") or "", r.get("id") or ""), reverse=True)
    return juntas[int(offset): int(offset) + int(limit)]


def count_imports_all_envs(**filtros) -> int:
    from app.persistence import context as env_context

    total = 0
    for env in _envs_para_listagem():
        with env_context.active_env(env["id"], env["slug"]):
            total += count_imports(**filtros)
    return total


def count_by_portal_status_all_envs(**filtros) -> dict[str, int]:
    """Contadores dos chips somando os ambientes. Anda junto da listagem."""
    from app.persistence import context as env_context

    total: dict[str, int] = {}
    for env in _envs_para_listagem():
        with env_context.active_env(env["id"], env["slug"]):
            for estado, n in count_by_portal_status(**filtros).items():
                total[estado] = total.get(estado, 0) + n
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_imports_cross_env.py -v`
Expected: PASS

- [ ] **Step 5: Lint e commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
git add app/persistence/repo.py tests/test_imports_cross_env.py
git commit -m "feat(imports): listagem que soma os ambientes, com selo da empresa"
```

---

### Task 15: Login sem seleção de ambiente

**Files:**
- Modify: `app/web/server.py` (`index`, `/api/imported`), `app/web/middleware/environment.py`,
  `app/web/dependencies/environment.py`, `app/web/static/index.html`, `app/web/static/js/shell.js`
- Test: `tests/test_routing_modo.py` (estender)

**O que muda e o que não muda:**

| | `desligado` / `observando` | `ligado` |
|---|---|---|
| login | → escolher empresa → trabalhar | → trabalhar |
| caixa de entrada | pedidos de uma empresa | todos, com selo da empresa em cada linha |
| cookie `portal_env` | **gate** de toda navegação | **filtro** opcional da listagem |
| ambiente do pedido | o que estava selecionado no commit | derivado do documento |

**Não muda:** `environment_id` continua bind imutável; cada empresa continua com seu
SQLite e seu Firebird; `active_env()` continua envolvendo todo caminho de escrita — só que
o `env` vem **do pedido**. Isolamento entre empresas é o mesmo. `/admin/ambientes` continua
por empresa.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_routing_modo.py

def test_ligado_a_home_nao_manda_escolher_ambiente(client):
    roteamento_repo.set_modo("ligado", por="t")
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 200


def test_desligado_a_home_continua_mandando_escolher(client):
    roteamento_repo.set_modo("desligado", por="t")
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (200, 307)
    if r.status_code == 307:
        assert r.headers["location"] == "/selecionar-ambiente"


def test_ligado_a_caixa_de_entrada_soma_os_ambientes(client, tmp_path):
    """Sem empresa escolhida e com o roteamento valendo, a lista traz as duas."""
    from app.persistence import context as env_context
    from app.persistence import environments_repo, repo

    for slug, nome in (("nasmar", "Nasmar"), ("mm", "MM Americanense")):
        environments_repo.create(
            slug=slug, name=nome, watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug), fb_path=str(tmp_path / f"{slug}.fdb"),
        )
        env = environments_repo.get_by_slug(slug)
        with env_context.active_env(env["id"], env["slug"]):
            repo.insert_import({
                "id": f"{slug}-1", "source_filename": "p.pdf",
                "imported_at": "2026-09-05T10:00:00", "order_number": slug.upper(),
                "customer_name": nome, "status": "success", "portal_status": "parsed",
            })

    roteamento_repo.set_modo("ligado", por="t")
    body = client.get("/api/imported").json()
    assert {e["env_name"] for e in body["entries"]} == {"Nasmar", "MM Americanense"}
    assert body["total"] == 2
    assert body["counts"]["parsed"] == 2
```

> `entries`, `total` e `counts` são as chaves que `/api/imported` já devolve
> (`server.py:1268`). O que muda é o conteúdo, não a forma da resposta.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_modo.py -v -k ligado`
Expected: FAIL — a home redireciona para `/selecionar-ambiente` mesmo com `ligado`.

- [ ] **Step 3: Write minimal implementation**

Em `app/web/server.py`, a home deixa de exigir ambiente quando o roteamento vale:

```python
@app.get("/")
def index(request: Request):
    """Dashboard. Redireciona para login se não autenticado.

    Com `roteamento_modo='ligado'` o ambiente é propriedade do pedido, não da
    sessão: não há o que escolher, e o passo de seleção some. Nos outros modos
    o comportamento é o de sempre.
    """
    from app.persistence import roteamento_repo

    if not request.cookies.get(COOKIE_NAME) and not _is_test_bypass():
        return RedirectResponse(url="/login")
    if roteamento_repo.modo() != roteamento_repo.LIGADO:
        if getattr(request.state, "environment", None) is None and not _is_test_bypass():
            return RedirectResponse(url="/selecionar-ambiente")
    return FileResponse(str(STATIC_DIR / "index.html"))
```

Em `/api/imported`, a listagem passa a somar os ambientes quando não há empresa escolhida:

```python
    from app.persistence import repo, roteamento_repo

    # Sem empresa no cookie E com o roteamento valendo, a caixa de entrada é
    # de todas as empresas. As três funções trocam JUNTAS: lista, total e
    # chips têm que contar o mesmo conjunto.
    cross = (
        roteamento_repo.modo() == roteamento_repo.LIGADO
        and getattr(request.state, "environment", None) is None
    )
    listar = repo.list_imports_all_envs if cross else repo.list_imports
    contar = repo.count_imports_all_envs if cross else repo.count_imports
    contar_chips = (
        repo.count_by_portal_status_all_envs if cross else repo.count_by_portal_status
    )
```

`list_imported` passa a receber `request: Request` (hoje não recebe), e as três chamadas
existentes passam a usar `listar`, `contar` e `contar_chips` em vez dos nomes diretos.

Em `app/web/dependencies/environment.py`, `current_environment` ganha a mensagem certa
para o modo novo — continua devolvendo 412, porque as rotas que escrevem no Fire **de
fato** precisam de um ambiente:

```python
        raise HTTPException(
            status_code=412,
            detail="Esta ação é de uma empresa específica — abra o pedido para agir nele.",
        )
```

Em `app/web/static/index.html`, cada linha da listagem ganha o selo `env_name` quando ele
vem no payload; em `shell.js`, o seletor de empresa do topo vira **filtro** (com a opção
"Todas as empresas") em vez de gate.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_modo.py -v`
Expected: PASS

- [ ] **Step 5: Validação de design**

`hm-designer` na caixa de entrada com selo de empresa e no seletor que virou filtro.

- [ ] **Step 6: Lint, suíte completa, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/web/ tests/test_routing_modo.py
git commit -m "feat(web): com o roteamento ligado, o ambiente e propriedade do pedido"
```

---

## Validação final antes do merge

- [ ] **Suíte completa verde**

```bash
ruff check app/ tests/ && ruff format --check app/ tests/
.venv/bin/pytest tests/ -v
```

- [ ] **Os testes que não podem ser relaxados** — rode-os nomeadamente e confira que
  passam pelo motivo certo, não por acidente:

| teste | o que protege |
|---|---|
| `test_desligado_nao_muda_nada` | a adoção: enquanto a chave não virar, o Portal é o de hoje |
| `test_nenhum_sample_traz_os_dois_cnpjs` | a propriedade medida que torna o degrau do documento seguro |
| `test_documento_vence_historico_e_memoria` | o caso Centauro impossível por construção |
| `test_cliente_nos_dois_bancos_recusa` | ambíguo não vota no maior volume |
| `test_ligado_sem_resposta_pede_escolha_em_vez_de_chutar` | sem resposta nunca vira default |
| `test_ligado_sem_resposta_retem_o_arquivo_e_nao_importa` | o mesmo, no watcher, onde não há humano |

- [ ] **Doc incremental** — só as seções afetadas:
  - `docs/ai/00-index.md`: domínio `routing` novo, apontando para os arquivos.
  - `docs/ai/modules/environments.md`: `environments.cnpj`, e o interruptor mudando o
    papel do cookie `portal_env`.
  - `docs/ai/modules/models.md`: `OrderHeader.supplier_cnpj`.
  - `docs/ai/modules/pipeline.md`: a varredura do fornecedor entre parser e normalizer.
  - `docs/ai/modules/web.md`: rotas novas (`/admin/roteamento`, `/api/roteamento/*`) e a
    mudança de `/api/commit`.
  - `docs/ai/modules/persistence.md`: as quatro tabelas novas no shared.
  - `CLAUDE.md`: contagem de testes e de rotas, se mudaram.

- [ ] **Gate manual do Firebird (Fase 0)** — cópia do `.fdb`, nunca produção. Diff
  coluna a coluna registrado no PR.

- [ ] **Gate manual do histórico (Task 6)** — números conferidos contra o fato 15 da spec,
  na Fire viva, somente leitura.

- [ ] **Deploy entra em `desligado`.** O primeiro passo em produção é ligar `observando` e
  deixar acumular. Ninguém vira para `ligado` porque o código ficou pronto — vira quando a
  taxa de acerto convencer o Samuel.
