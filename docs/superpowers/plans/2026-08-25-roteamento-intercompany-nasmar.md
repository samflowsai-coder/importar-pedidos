# Roteamento intercompany Nasmar → MM — Implementation Plan

> ⚠️ **SUBSTITUÍDO em 2026-09-09.** Este plano foi escrito contra a **Revisão 2** da spec.
> A Revisão 4 mudou o eixo do roteamento de *cadastro de cliente* para *fornecedor no
> documento*, e com isso as Tasks 4, 5 e 6 (a tabela `rota_intercompany`) deixaram de
> existir. O plano em vigor é
> [`2026-09-09-roteamento-intercompany-fases-0-1c.md`](2026-09-09-roteamento-intercompany-fases-0-1c.md),
> que traz as Tasks 1 a 3 (Fase 0) daqui inalteradas.
>
> **O que ainda vale neste arquivo:** as Tasks 7 a 12 — lote, consolidador, janela, perna
> espelho e `/lotes`. Elas não dependem do eixo do roteamento e serão a base do plano da
> Fase 2, quando a pergunta comercial em aberto for respondida.


> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pedido de cliente que compra da Nasmar nasce no ambiente Nasmar com o cliente
final, e vira um pedido consolidado por janela no ambiente MM com a Nasmar como cliente,
preço ajustado por parâmetro e rastro auditável das pernas de origem.

**Architecture:** Três camadas independentes. (1) O mapper do Firebird passa a escrever
todas as colunas que um pedido faturável precisa — hoje escreve 9 de 98 e nunca rodou em
produção. (2) Uma tabela de rota por CNPJ decide o ambiente de destino antes da linha em
`imports` nascer, porque `environment_id` é bind imutável. (3) Um consolidador puro monta
o pedido da perna espelho a partir das pernas de origem da janela, e um job fecha o lote.

**Tech Stack:** Python 3.11+, pydantic v2, SQLite (`app_shared.db` + `app_state_<slug>.db`),
firebird-driver, FastAPI, APScheduler, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`](../specs/2026-08-24-roteamento-intercompany-nasmar-design.md) (Revisão 2)

**Escopo deste plano:** Fases 0, 1 e 2 da spec. A **Fase 3** (perna de volta no
FlowPCP / `poll_decisoes`) ganha plano próprio depois que a Fase 2 estiver em produção —
ela depende do `pcp-app`, que é outro repositório.

## Global Constraints

- **Python 3.11+** — `X | Y` e `match` liberados (`pyproject.toml: requires-python = ">=3.11"`).
- **Toda mutação de `portal_status`/`production_status` passa por `app.state.transition()`.** Nunca atribuir direto (`state.md`, "Armadilhas").
- **Tabelas transversais usam `db.connect_shared()`**; tabelas operacionais de pedido usam `db.connect()` (`environments.md`, "connect() vs connect_shared()").
- **`environment_id` em `imports` é bind imutável** — populado no INSERT, jamais em UPDATE.
- **Cálculo de preço em `Decimal`, nunca `float`.** `OrderItem.unit_price` é `float` (`app/models/order.py:20`); converter na borda, quantizar explicitamente antes de gravar.
- **Charset do Firebird é `WIN1252`.** Flags booleanas são strings `'Sim'`/`'Nao'`. `STATUS` inicial é `'PEDIDO'`.
- **Nunca rodar script de escrita contra `.fdb` de produção.** Validação de Firebird usa cópia.
- **Lint antes de dar por pronto:** `ruff check app/ tests/` e `ruff format app/ tests/`.
- **Suíte completa antes do commit final:** `.venv/bin/pytest tests/ -v` (1021 testes em 91 arquivos, conferido 2026-08-24).

---

## File Structure

**Criados:**

| Arquivo | Responsabilidade |
|---|---|
| `app/erp/fiscal.py` | Perfil fiscal por ambiente — as constantes que o Fire exige e o mapper não escrevia |
| `app/routing/__init__.py` | Pacote novo |
| `app/routing/intercompany.py` | Política: este pedido vai pra qual ambiente? Puro, sem I/O de Firebird |
| `app/persistence/rotas_repo.py` | CRUD de `rota_intercompany` (shared) |
| `app/persistence/lotes_repo.py` | CRUD de `lote_config`, `intercompany_lote`, `intercompany_lote_item` (shared) |
| `app/consolidators/janela.py` | Puro: data → janela → chave de lote |
| `app/consolidators/lote.py` | Puro: N `Order` de origem → 1 `Order` consolidado |
| `app/web/routes_intercompany.py` | `/api/admin/rotas/*` e `/api/lotes/*` |
| `app/web/static/admin-rotas.html` | Cadastro de rota por CNPJ |
| `app/web/static/lotes.html` | Lote aberto, conteúdo, fechar, informar fator |
| `app/worker/jobs/fechar_lotes.py` | Job que fecha a janela vencida |

**Modificados:**

| Arquivo | O quê |
|---|---|
| `app/persistence/schema_shared.py` | 3 tabelas novas + colunas fiscais em `environments` |
| `app/erp/queries.py` | `INSERT_CAB_VENDAS` e `INSERT_CORPO_VENDAS` completos |
| `app/erp/mapper.py` | Escrever as colunas que faltam; `UNID` do cadastro |
| `app/exporters/firebird_exporter.py` | Passar perfil fiscal; devolver o `CODIGO` gerado |
| `app/web/server.py` | Roteamento no commit; anexar perna ao lote no send-to-fire |
| `app/worker/jobs/scan_environments.py` | Roteamento no watcher |
| `app/worker/__init__.py` | Registrar `fechar_lotes` |
| `app/web/static/js/shell.js` | Itens de menu novos |
| `docs/ai/modules/erp.md`, `environments.md` | Seções afetadas |

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

# FASE 1 — Roteamento da perna de origem

Entrega valor sozinha: acaba o pedido Nasmar caindo no ambiente da MM.

---

### Task 4: Tabela e repo de `rota_intercompany`

**Files:**
- Modify: `app/persistence/schema_shared.py` (TABLES_SQL)
- Create: `app/persistence/rotas_repo.py`
- Test: `tests/test_rotas_repo.py`

**Interfaces:**
- Produces: `rotas_repo.upsert(conn, *, cnpj, env_origem_slug, rotulo) -> dict`;
  `rotas_repo.lookup(conn, cnpj) -> dict | None`; `rotas_repo.listar(conn) -> list[dict]`;
  `rotas_repo.desativar(conn, cnpj) -> bool`. Todas recebem `sqlite3.Connection` explícito,
  como `produto_depara_repo`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotas_repo.py
from __future__ import annotations

import pytest

from app.persistence import db, rotas_repo


@pytest.fixture()
def conn(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    with db.connect_shared() as c:
        yield c
    db.set_db_path(None)


def test_upsert_e_lookup_normalizam_cnpj(conn):
    """CNPJ entra formatado e sai casando com a versao so-digitos."""
    rotas_repo.upsert(conn, cnpj="76.917.624/0004-82",
                      env_origem_slug="nasmar", rotulo="DAJU")
    achado = rotas_repo.lookup(conn, "76917624000482")
    assert achado is not None
    assert achado["env_origem_slug"] == "nasmar"
    assert achado["rotulo"] == "DAJU"


def test_lookup_de_cnpj_nao_cadastrado_devolve_none(conn):
    assert rotas_repo.lookup(conn, "11111111000191") is None


def test_upsert_e_idempotente_no_mesmo_cnpj(conn):
    """Recadastrar o mesmo CNPJ atualiza, nao duplica — a coluna e UNIQUE."""
    rotas_repo.upsert(conn, cnpj="76917624000482",
                      env_origem_slug="nasmar", rotulo="DAJU")
    rotas_repo.upsert(conn, cnpj="76.917.624/0004-82",
                      env_origem_slug="nasmar", rotulo="DAJU LTDA")
    assert len(rotas_repo.listar(conn)) == 1
    assert rotas_repo.lookup(conn, "76917624000482")["rotulo"] == "DAJU LTDA"


def test_desativar_esconde_do_lookup_mas_preserva_a_linha(conn):
    """Desativar nao apaga: o historico de qual rota valia importa em auditoria."""
    rotas_repo.upsert(conn, cnpj="76917624000482",
                      env_origem_slug="nasmar", rotulo="DAJU")
    assert rotas_repo.desativar(conn, "76917624000482") is True
    assert rotas_repo.lookup(conn, "76917624000482") is None
    assert len(rotas_repo.listar(conn, incluir_inativas=True)) == 1


def test_cnpj_vazio_e_rejeitado(conn):
    with pytest.raises(ValueError):
        rotas_repo.upsert(conn, cnpj="", env_origem_slug="nasmar", rotulo="X")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_rotas_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'rotas_repo'`

- [ ] **Step 3: Add the table**

Append to `TABLES_SQL` in `app/persistence/schema_shared.py`:

```sql
-- Quem compra da revenda. CNPJ do cliente FINAL -> ambiente onde o pedido
-- dele deve nascer. Transversal de proposito: a decisao de roteamento
-- acontece ANTES de existir ambiente ativo, e `imports.environment_id` e
-- bind imutavel — nao da pra corrigir depois do INSERT.
CREATE TABLE IF NOT EXISTS rota_intercompany (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    cnpj_cliente     TEXT NOT NULL UNIQUE,
    env_origem_slug  TEXT NOT NULL,
    rotulo           TEXT,
    ativo            INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
```

- [ ] **Step 4: Write the repo**

```python
# app/persistence/rotas_repo.py
"""Rota intercompany: CNPJ do cliente final -> ambiente onde o pedido nasce.

`cnpj_cliente` e guardado SO COM DIGITOS. Toda entrada passa por
`app.erp.cnpj.cnpj_digits` na gravacao E na leitura — chave divergente entre
as duas pontas e vinculo fantasma, mesmo problema que `produto_depara_repo`
documenta em `_norm_key`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.erp.cnpj import cnpj_digits

if TYPE_CHECKING:
    import sqlite3

_COLS = ("id", "cnpj_cliente", "env_origem_slug", "rotulo", "ativo",
         "created_at", "updated_at")


def _agora() -> str:
    return datetime.now(UTC).isoformat()


def _row(r: Any) -> dict:
    return dict(zip(_COLS, r))


def upsert(conn: sqlite3.Connection, *, cnpj: str,
           env_origem_slug: str, rotulo: str | None = None) -> dict:
    """Cadastra ou atualiza a rota. Reativa se estava inativa."""
    d = cnpj_digits(cnpj)
    if not d:
        raise ValueError("cnpj vazio ou sem digitos")
    if not (env_origem_slug or "").strip():
        raise ValueError("env_origem_slug obrigatorio")
    agora = _agora()
    conn.execute(
        """INSERT INTO rota_intercompany
               (cnpj_cliente, env_origem_slug, rotulo, ativo, created_at, updated_at)
           VALUES (?, ?, ?, 1, ?, ?)
           ON CONFLICT(cnpj_cliente) DO UPDATE SET
               env_origem_slug = excluded.env_origem_slug,
               rotulo          = excluded.rotulo,
               ativo           = 1,
               updated_at      = excluded.updated_at""",
        (d, env_origem_slug.strip(), rotulo, agora, agora),
    )
    conn.commit()
    return lookup(conn, d)  # type: ignore[return-value]


def lookup(conn: sqlite3.Connection, cnpj: str | None) -> dict | None:
    """Rota ATIVA para o CNPJ, ou None. None = comportamento de hoje."""
    d = cnpj_digits(cnpj)
    if not d:
        return None
    cur = conn.execute(
        f"SELECT {','.join(_COLS)} FROM rota_intercompany "
        "WHERE cnpj_cliente = ? AND ativo = 1",
        (d,),
    )
    r = cur.fetchone()
    return _row(r) if r else None


def listar(conn: sqlite3.Connection, *, incluir_inativas: bool = False) -> list[dict]:
    sql = f"SELECT {','.join(_COLS)} FROM rota_intercompany"
    if not incluir_inativas:
        sql += " WHERE ativo = 1"
    sql += " ORDER BY rotulo, cnpj_cliente"
    return [_row(r) for r in conn.execute(sql).fetchall()]


def desativar(conn: sqlite3.Connection, cnpj: str) -> bool:
    d = cnpj_digits(cnpj)
    cur = conn.execute(
        "UPDATE rota_intercompany SET ativo = 0, updated_at = ? "
        "WHERE cnpj_cliente = ? AND ativo = 1",
        (_agora(), d),
    )
    conn.commit()
    return cur.rowcount > 0
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_rotas_repo.py -v`
Expected: 5 passed

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/persistence/rotas_repo.py tests/test_rotas_repo.py
ruff format app/persistence/rotas_repo.py tests/test_rotas_repo.py
git add app/persistence/rotas_repo.py app/persistence/schema_shared.py tests/test_rotas_repo.py
git commit -m "feat(persistence): tabela e repo de rota intercompany por CNPJ"
```

---

### Task 5: A política de roteamento

**Files:**
- Create: `app/routing/__init__.py`, `app/routing/intercompany.py`
- Test: `tests/test_routing_intercompany.py`

**Interfaces:**
- Consumes: `rotas_repo.lookup` (Task 4).
- Produces: `Rota` (frozen dataclass: `env_origem_slug: str`, `cnpj_cliente: str`,
  `rotulo: str | None`); `rota_para(order: Order) -> Rota | None`.

**Contexto:** mesmo contrato defensivo de
`app/integrations/flowpcp/intercompany.py::resolucao_para` — **nunca levanta**. Uma falha
de leitura do cadastro não pode derrubar a importação de um pedido; ela degrada para o
comportamento de hoje (ambiente selecionado).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_intercompany.py
from __future__ import annotations

from app.models.order import Order, OrderHeader, OrderItem
from app.routing.intercompany import rota_para


def _pedido(cnpj):
    return Order(
        header=OrderHeader(order_number="OC-1", customer_name="X", customer_cnpj=cnpj),
        items=[OrderItem(description="d", quantity=1.0)],
    )


def test_cnpj_cadastrado_roteia_para_o_ambiente_de_origem(conn_com_rota):
    r = rota_para(_pedido("76.917.624/0004-82"))
    assert r is not None
    assert r.env_origem_slug == "nasmar"
    assert r.cnpj_cliente == "76917624000482"


def test_cnpj_nao_cadastrado_nao_roteia(conn_com_rota):
    """None = segue o comportamento de hoje, ambiente selecionado."""
    assert rota_para(_pedido("11.111.111/0001-91")) is None


def test_pedido_sem_cnpj_nao_roteia(conn_com_rota):
    """Riachuelo nao traz CNPJ no header (mercado_eletronico_parser.py:56).
    Sem CNPJ nao ha como decidir — nao inventa."""
    assert rota_para(_pedido(None)) is None
    assert rota_para(_pedido("")) is None
    assert rota_para(_pedido("ISENTO")) is None


def test_falha_de_leitura_nao_levanta(monkeypatch, conn_com_rota):
    """Cadastro ilegivel degrada pro comportamento de hoje, nao derruba o import."""
    import app.routing.intercompany as ri

    def explode(*a, **k):
        raise RuntimeError("db fora do ar")

    monkeypatch.setattr(ri.rotas_repo, "lookup", explode)
    assert rota_para(_pedido("76917624000482")) is None
```

Add the fixture to the same file:

```python
import pytest

from app.persistence import db, rotas_repo


@pytest.fixture()
def conn_com_rota(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    with db.connect_shared() as c:
        rotas_repo.upsert(c, cnpj="76917624000482",
                          env_origem_slug="nasmar", rotulo="DAJU")
    yield
    db.set_db_path(None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_intercompany.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.routing'`

- [ ] **Step 3: Write the implementation**

```python
# app/routing/__init__.py
"""Politica de roteamento de pedido para ambiente."""
```

```python
# app/routing/intercompany.py
"""Decide em QUAL ambiente um pedido deve nascer.

Alguns varejistas compram da Nasmar, nao da MM: a Nasmar fatura pra eles e
compra da MM. O pedido desses clientes precisa nascer no ambiente da Nasmar
com o cliente final, e nao no ambiente que o operador selecionou.

Este modulo so decide. Nao le Firebird, nao cria lote, nao mexe em `imports`.

NUNCA LEVANTA. Uma falha aqui degrada para `None` — o comportamento de hoje
(ambiente selecionado). Derrubar a importacao de um pedido porque o cadastro
de rota esta ilegivel seria trocar um bug de roteamento por um de perda.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.erp.cnpj import cnpj_digits
from app.models.order import Order
from app.persistence import db, rotas_repo
from app.utils.logger import logger


@dataclass(frozen=True)
class Rota:
    env_origem_slug: str
    cnpj_cliente: str
    rotulo: str | None = None


def rota_para(order: Order) -> Rota | None:
    """Ambiente de origem do pedido, ou None quando nao ha rota cadastrada."""
    try:
        d = cnpj_digits(order.header.customer_cnpj)
        # CADASTRO legado guarda 'ISENTO' e '0'; cnpj_digits devolve '' ou
        # lixo curto. So 11 (CPF) ou 14 (CNPJ) digitos sao decidiveis.
        if len(d) not in (11, 14):
            return None
        with db.connect_shared() as conn:
            achado = rotas_repo.lookup(conn, d)
        if achado is None:
            return None
        return Rota(
            env_origem_slug=achado["env_origem_slug"],
            cnpj_cliente=achado["cnpj_cliente"],
            rotulo=achado.get("rotulo"),
        )
    except Exception as exc:  # noqa: BLE001 — degradar, nunca derrubar o import
        logger.warning(f"routing: rota_para falhou, seguindo sem rotear: {exc}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_intercompany.py -v`
Expected: 5 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check app/routing/ tests/test_routing_intercompany.py
ruff format app/routing/ tests/test_routing_intercompany.py
git add app/routing/ tests/test_routing_intercompany.py
git commit -m "feat(routing): politica de roteamento intercompany por CNPJ"
```

---

### Task 6: Ligar o roteamento nos dois pontos de entrada

**Files:**
- Modify: `app/web/server.py` (o commit do preview, antes de `repo.insert_import`)
- Modify: `app/worker/jobs/scan_environments.py` (idem, no watcher)
- Create: `app/web/static/admin-rotas.html`
- Modify: `app/web/routes_intercompany.py` (novo — rotas de admin)
- Modify: `app/web/static/js/shell.js` (item de menu)
- Test: `tests/test_routing_wiring.py`

**Interfaces:**
- Consumes: `rota_para` (Task 5), `rotas_repo` (Task 4).
- Produces: rota HTTP `GET/POST/DELETE /api/admin/rotas`; a linha em `imports` nasce com
  `environment_id` do ambiente de origem quando há rota.

**Contexto crítico:** `environment_id` é bind imutável (`environments.md`). O roteamento
tem que acontecer **antes** do `repo.insert_import`, dentro de
`with active_env(env_id, slug):` do ambiente de destino — não do selecionado.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_wiring.py
from __future__ import annotations

import pytest

from app.persistence import db, rotas_repo


@pytest.fixture()
def rota_daju(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    with db.connect_shared() as c:
        rotas_repo.upsert(c, cnpj="76917624000482",
                          env_origem_slug="nasmar", rotulo="DAJU")
    yield
    db.set_db_path(None)


def test_commit_de_pedido_roteado_nasce_no_ambiente_de_origem(client, rota_daju, env_mm, env_nasmar):
    """Operador logado na MM commita pedido da DAJU -> a linha em imports
    nasce com environment_id da NASMAR, nao da MM."""
    resp = client.post("/api/process", files=_arquivo_daju())
    entry_id = resp.json()["entry_id"]

    from app.persistence import router
    with router.env_connect("nasmar") as c:
        achado = c.execute("SELECT id FROM imports WHERE id = ?", (entry_id,)).fetchone()
    assert achado is not None, "pedido deveria ter nascido no ambiente nasmar"

    with router.env_connect("mm") as c:
        assert c.execute("SELECT id FROM imports WHERE id = ?", (entry_id,)).fetchone() is None


def test_commit_de_pedido_sem_rota_nasce_no_ambiente_selecionado(client, rota_daju, env_mm):
    """Sem rota cadastrada, nada muda em relacao a hoje."""
    resp = client.post("/api/process", files=_arquivo_sem_rota())
    entry_id = resp.json()["entry_id"]

    from app.persistence import router
    with router.env_connect("mm") as c:
        assert c.execute("SELECT id FROM imports WHERE id = ?", (entry_id,)).fetchone()


def test_preview_avisa_que_o_pedido_vai_para_outro_ambiente(client, rota_daju, env_mm, env_nasmar):
    """Roteamento silencioso e como o bug de hoje nasceu. A resposta do
    preview carrega o destino para a UI mostrar."""
    resp = client.post("/api/process", files=_arquivo_daju())
    body = resp.json()
    assert body["rota_intercompany"]["env_origem_slug"] == "nasmar"
    assert body["rota_intercompany"]["rotulo"] == "DAJU"
```

Reuse the app/client fixtures already used by `tests/test_web_server.py`; add
`_arquivo_daju()` / `_arquivo_sem_rota()` helpers building a minimal XLSX from
`samples/`, and `env_mm` / `env_nasmar` fixtures creating two environments via
`environments_repo`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_routing_wiring.py -v`
Expected: FAIL — `KeyError: 'rota_intercompany'` e o pedido nasce em `mm`.

- [ ] **Step 3: Wire the web commit**

In `app/web/server.py`, at the commit boundary, before `repo.insert_import`:

```python
    from app.persistence.context import active_env
    from app.routing.intercompany import rota_para

    rota = rota_para(order)
    destino = environments_repo.get_by_slug(rota.env_origem_slug) if rota else None
    if destino is None:
        destino = getattr(request.state, "environment", None)
        rota = None

    with active_env(destino["id"], destino["slug"]):
        repo.insert_import(entry)
        # ... resto do commit, ja dentro do ambiente de destino
```

Include the routing decision in the preview response payload:

```python
    payload["rota_intercompany"] = (
        {"env_origem_slug": rota.env_origem_slug, "rotulo": rota.rotulo}
        if rota else None
    )
```

- [ ] **Step 4: Wire the watcher**

In `app/worker/jobs/scan_environments.py`, the same decision inside the per-file loop:
compute `rota_para(order)` after the pipeline parses, and open
`with active_env(destino_id, destino_slug):` for the insert instead of the scanned
environment.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_routing_wiring.py tests/test_web_server.py tests/test_scan_environments.py -v`
Expected: PASS

- [ ] **Step 6: Build the admin screen**

Create `app/web/routes_intercompany.py` with `GET /api/admin/rotas`,
`POST /api/admin/rotas` (`require_admin`), `DELETE /api/admin/rotas/{cnpj}`
(`require_admin`). Create `app/web/static/admin-rotas.html` following
`admin-usuarios.html` — same app-shell includes (`tokens.css`, `shell.css`, `shell.js`,
`<div id="app-shell">`), table of CNPJ / rótulo / ambiente, add form, remove button.
Register the page route `GET /configuracoes/rotas` and add the sidebar item in
`shell.js` under the admin-only Configurações group.

Show the routing badge in the preview UI: when `rota_intercompany` is non-null, render
*"Este pedido vai para o ambiente NASMAR"* above the commit button.

Bump the asset cache-buster (`?v=`) on `shell.js` — assets have no hash
(`web.md`, "Armadilhas").

- [ ] **Step 7: Lint, full suite, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/web/ app/worker/jobs/scan_environments.py tests/test_routing_wiring.py
git commit -m "feat(web): roteia pedido para o ambiente de origem e avisa no preview"
```

- [ ] **Step 8: Update the module docs**

Add a "Roteamento intercompany" section to `docs/ai/modules/environments.md` and list the
new routes in `docs/ai/modules/web.md`. Only the affected sections — não reescrever o
módulo (`CLAUDE.md`, "Doc incremental").

```bash
git add docs/ai/modules/
git commit -m "docs(ai): roteamento intercompany em environments.md e web.md"
```

---

# FASE 2 — Lote e perna espelho

---

### Task 7: Tabelas e repo do lote

**Files:**
- Modify: `app/persistence/schema_shared.py` (TABLES_SQL)
- Create: `app/persistence/lotes_repo.py`
- Test: `tests/test_lotes_repo.py`

**Interfaces:**
- Produces — esta é a API completa que as Tasks 10, 11 e 12 consomem. Toda função recebe
  `conn: sqlite3.Connection` explícito, como `produto_depara_repo`:

```python
# config
get_config(conn, env_origem_slug: str) -> dict | None
save_config(conn, *, env_origem_slug: str, **campos) -> dict
listar_configs_ativas(conn) -> list[dict]

# lote
lote_aberto(conn, env_origem_slug: str, janela_inicio: date, janela_fim: date) -> dict
    Cria o lote se nao existir, COPIANDO fator_preco e modo_preco da config para
    fator_preco_aplicado/modo_preco_aplicado do lote. Rafael respondeu em 09/09 que
    percentual novo "so vale da segunda seguinte em diante": congelar no fechamento
    faria uma mudanca de quarta pegar a semana ja aberta. Congela na abertura.
    Config sem fator ainda deixa o lote sem fator -> aguardando_fator no fechamento.
buscar_lote(conn, env_origem_slug: str, janela_inicio: date, janela_fim: date) -> dict | None
get(conn, lote_id: int) -> dict | None
marcar_aguardando_fator(conn, lote_id: int) -> None
informar_fator(conn, *, lote_id: int, fator: Decimal, modo: str, usuario: str) -> dict
fechar(conn, *, lote_id: int, fator_preco_aplicado: Decimal, modo_preco_aplicado: str,
       closed_by: str, import_id_espelho: str | None = None) -> dict

# pernas
anexar_perna(conn, *, lote_id: int, import_id: str, env_origem_slug: str,
             fire_codigo_origem: int | None, pedido_cliente: str | None,
             cnpj_cliente_final: str | None, razao_cliente_final: str | None) -> None
pernas_do_lote(conn, lote_id: int) -> list[dict]

# erro
class LoteFechadoError(Exception): ...
```

`lote_aberto` cria se não existe; `buscar_lote` só lê (é o que o job usa, porque fechar
uma janela vencida não pode criar lote fantasma numa semana sem pedido).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lotes_repo.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.persistence import db, lotes_repo


@pytest.fixture()
def conn(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    with db.connect_shared() as c:
        yield c
    db.set_db_path(None)


def test_lote_aberto_e_criado_uma_vez_por_janela(conn):
    a = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    b = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    assert a["id"] == b["id"]
    assert a["status"] == "aberto"


def test_chave_do_lote_e_unica(conn):
    a = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    b = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 24), date(2026, 8, 30))
    assert a["chave_lote"] != b["chave_lote"]


def test_anexar_perna_e_listar(conn):
    lote = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    lotes_repo.anexar_perna(
        conn, lote_id=lote["id"], import_id="imp-1", env_origem_slug="nasmar",
        fire_codigo_origem=1157, pedido_cliente="AF179",
        cnpj_cliente_final="05055599002802", razao_cliente_final="H 2 S 4",
    )
    pernas = lotes_repo.pernas_do_lote(conn, lote["id"])
    assert len(pernas) == 1
    assert pernas[0]["fire_codigo_origem"] == 1157


def test_sem_fator_o_lote_vai_para_aguardando_fator(conn):
    """O teste que trava a regressao mais cara: preco inferido em documento
    fiscal e imposto errado. Sem fator cadastrado o lote NAO fecha."""
    lote = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    lotes_repo.marcar_aguardando_fator(conn, lote["id"])
    atual = lotes_repo.get(conn, lote["id"])
    assert atual["status"] == "aguardando_fator"
    assert atual["fator_preco_aplicado"] is None


def test_informar_fator_grava_quem_e_quando(conn):
    lote = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    lotes_repo.marcar_aguardando_fator(conn, lote["id"])
    r = lotes_repo.informar_fator(conn, lote_id=lote["id"], fator=Decimal("0.07"),
                                  modo="divisor", usuario="samuel@x.com")
    assert r["fator_preco_aplicado"] == "0.07"
    assert r["fator_informado_por"] == "samuel@x.com"
    assert r["fator_informado_em"] is not None
    assert r["status"] == "aberto"


def test_lote_fechado_nao_aceita_perna_nova(conn):
    """Pedido atrasado vai pro proximo lote. Lote fechado nunca reabre."""
    lote = lotes_repo.lote_aberto(conn, "nasmar", date(2026, 8, 17), date(2026, 8, 23))
    lotes_repo.fechar(conn, lote_id=lote["id"],
                      fator_preco_aplicado=Decimal("0.07"), closed_by="job")
    with pytest.raises(lotes_repo.LoteFechadoError):
        lotes_repo.anexar_perna(
            conn, lote_id=lote["id"], import_id="imp-2", env_origem_slug="nasmar",
            fire_codigo_origem=1189, pedido_cliente="MF072",
            cnpj_cliente_final="1", razao_cliente_final="FKSHOES",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_lotes_repo.py -v`
Expected: FAIL — `ImportError: cannot import name 'lotes_repo'`

- [ ] **Step 3: Add the tables**

Append to `TABLES_SQL` in `app/persistence/schema_shared.py`:

```sql
-- Parametros do lote intercompany, um por par origem -> espelho.
-- fator_preco e NULLABLE DE PROPOSITO: sem fator o lote nao fecha, para e
-- pergunta ao operador. Preco inferido em documento fiscal e imposto errado.
-- modo_preco existe porque "nota -7%" e ambiguo: 'divisor' faz preco/1.07
-- (o que o Fire tem hoje: 16,12 -> 15,0654) e 'desconto' faz preco*0.93
-- (16,12 -> 14,9916). Diferenca medida: ~R$ 15 mil/ano.
-- Rafael escolheu 'desconto' em 08/09/2026, entao e o default. 'divisor'
-- fica porque lote fechado guarda o modo que usou e nao pode ser reescrito.
-- hora_fechamento: ele pediu segunda de manha; 8h e a hora acordada.
CREATE TABLE IF NOT EXISTS lote_config (
    env_origem_slug   TEXT PRIMARY KEY,
    env_espelho_slug  TEXT NOT NULL,
    cnpj_revenda      TEXT NOT NULL,
    nome_revenda      TEXT NOT NULL,
    janela            TEXT NOT NULL DEFAULT 'semanal',
    dia_fechamento    INTEGER NOT NULL DEFAULT 0,
    hora_fechamento   INTEGER NOT NULL DEFAULT 8,
    modo_preco        TEXT NOT NULL DEFAULT 'desconto',
    fator_preco       TEXT,
    ativo             INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intercompany_lote (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chave_lote           TEXT NOT NULL UNIQUE,
    env_origem_slug      TEXT NOT NULL,
    env_espelho_slug     TEXT NOT NULL,
    janela_inicio        TEXT NOT NULL,
    janela_fim           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'aberto',
    modo_preco_aplicado  TEXT,
    fator_preco_aplicado TEXT,
    fator_informado_por  TEXT,
    fator_informado_em   TEXT,
    import_id_espelho    TEXT,
    fire_codigo_espelho  INTEGER,
    created_at           TEXT NOT NULL,
    closed_at            TEXT,
    closed_by            TEXT
);

CREATE TABLE IF NOT EXISTS intercompany_lote_item (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id             INTEGER NOT NULL REFERENCES intercompany_lote(id) ON DELETE CASCADE,
    import_id           TEXT NOT NULL,
    env_origem_slug     TEXT NOT NULL,
    fire_codigo_origem  INTEGER,
    pedido_cliente      TEXT,
    cnpj_cliente_final  TEXT,
    razao_cliente_final TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (lote_id, import_id)
);
```

- [ ] **Step 4: Write the repo**

Implement `app/persistence/lotes_repo.py` with the interface listed above. Key rules:

- `LoteFechadoError(Exception)` — raised by `anexar_perna` when the lot's `status` is not
  `aberto` or `aguardando_fator`.
- `fator_preco` and `fator_preco_aplicado` are stored as **TEXT**, converted with
  `Decimal(str(...))` on read. SQLite has no decimal type and `REAL` would reintroduce
  the float problem the Global Constraints forbid.
- `lote_aberto` uses `INSERT ... ON CONFLICT(chave_lote) DO NOTHING` then `SELECT`, so
  two workers racing on the same window produce one lot.
- `chave_lote` comes from `app.consolidators.janela.chave_para` (Task 8) — import it here.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_lotes_repo.py -v`
Expected: 6 passed

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/persistence/lotes_repo.py tests/test_lotes_repo.py
ruff format app/persistence/lotes_repo.py tests/test_lotes_repo.py
git add app/persistence/ tests/test_lotes_repo.py
git commit -m "feat(persistence): tabelas e repo do lote intercompany"
```

---

### Task 8: Janela e chave de lote

**Files:**
- Create: `app/consolidators/janela.py`
- Test: `tests/test_consolidador_janela.py`

**Interfaces:**
- Produces: `janela_de(d: date, *, modo: str, dia_fechamento: int) -> tuple[date, date]`;
  `chave_para(env_origem_slug: str, inicio: date, modo: str) -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consolidador_janela.py
from __future__ import annotations

from datetime import date

import pytest

from app.consolidators.janela import chave_para, janela_de


def test_janela_semanal_vai_de_segunda_a_domingo():
    ini, fim = janela_de(date(2026, 8, 20), modo="semanal", dia_fechamento=0)
    assert ini == date(2026, 8, 17)  # segunda
    assert fim == date(2026, 8, 23)  # domingo


def test_janela_semanal_no_proprio_domingo_nao_vaza_pra_semana_seguinte():
    ini, fim = janela_de(date(2026, 8, 23), modo="semanal", dia_fechamento=0)
    assert (ini, fim) == (date(2026, 8, 17), date(2026, 8, 23))


def test_quinzena_primeira_metade_do_mes():
    ini, fim = janela_de(date(2026, 8, 7), modo="quinzenal", dia_fechamento=0)
    assert (ini, fim) == (date(2026, 8, 1), date(2026, 8, 15))


def test_quinzena_segunda_metade_termina_no_ultimo_dia():
    ini, fim = janela_de(date(2026, 8, 20), modo="quinzenal", dia_fechamento=0)
    assert (ini, fim) == (date(2026, 8, 16), date(2026, 8, 31))


def test_quinzena_de_fevereiro_respeita_o_mes_curto():
    ini, fim = janela_de(date(2026, 2, 20), modo="quinzenal", dia_fechamento=0)
    assert fim == date(2026, 2, 28)


def test_chave_semanal_usa_semana_iso():
    assert chave_para("nasmar", date(2026, 8, 17), "semanal") == "NAS-2026-S34"


def test_chave_quinzenal():
    assert chave_para("nasmar", date(2026, 8, 16), "quinzenal") == "NAS-2026-Q16"


def test_chave_cabe_no_pedido_cliente():
    """PEDIDO_CLIENTE trunca em 20 (mapper.py:64). Truncar chave de lote
    silenciosamente colidiria dois lotes."""
    c = chave_para("nasmar", date(2026, 8, 17), "semanal")
    assert len(c) <= 20


def test_modo_invalido_levanta():
    with pytest.raises(ValueError):
        janela_de(date(2026, 8, 20), modo="mensal", dia_fechamento=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_consolidador_janela.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.consolidators.janela'`

- [ ] **Step 3: Write the implementation**

```python
# app/consolidators/janela.py
"""Janela do lote intercompany e a chave que a identifica.

Puro: so datas e strings. Sem I/O.

A chave entra em CAB_VENDAS.PEDIDO_CLIENTE, que o mapper trunca em 20
caracteres (mapper.py:64). `NAS-2026-S34` tem 12 — nunca trunca. Isso
importa porque a idempotencia do exporter e PEDIDO_CLIENTE + CLIENTE
(queries.py:91): chave truncada colide dois lotes e o segundo e recusado
como duplicata.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

MODOS = ("semanal", "quinzenal")


def janela_de(d: date, *, modo: str, dia_fechamento: int) -> tuple[date, date]:
    """Inicio e fim (inclusivos) da janela que contem `d`."""
    if modo == "semanal":
        inicio = d - timedelta(days=(d.weekday() - dia_fechamento) % 7)
        return inicio, inicio + timedelta(days=6)
    if modo == "quinzenal":
        if d.day <= 15:
            return date(d.year, d.month, 1), date(d.year, d.month, 15)
        ultimo = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 16), date(d.year, d.month, ultimo)
    raise ValueError(f"modo de janela invalido: {modo!r} (use {MODOS})")


def chave_para(env_origem_slug: str, inicio: date, modo: str) -> str:
    """Chave do lote: <PREFIXO>-<ano>-S<semana ISO> ou -Q<quinzena>.

    O prefixo sao as 3 primeiras letras do slug em maiusculo, para que um
    segundo par origem->espelho no futuro nao colida.
    """
    prefixo = (env_origem_slug or "x")[:3].upper()
    if modo == "semanal":
        ano, semana, _ = inicio.isocalendar()
        return f"{prefixo}-{ano}-S{semana:02d}"
    if modo == "quinzenal":
        n = (inicio.month - 1) * 2 + (1 if inicio.day <= 15 else 2)
        return f"{prefixo}-{inicio.year}-Q{n:02d}"
    raise ValueError(f"modo de janela invalido: {modo!r} (use {MODOS})")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_consolidador_janela.py -v`
Expected: 9 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check app/consolidators/ tests/test_consolidador_janela.py
ruff format app/consolidators/ tests/test_consolidador_janela.py
git add app/consolidators/janela.py tests/test_consolidador_janela.py
git commit -m "feat(consolidators): janela semanal/quinzenal e chave de lote"
```

---

### Task 9: O consolidador

**Files:**
- Create: `app/consolidators/lote.py`
- Test: `tests/test_consolidador_lote.py`

**Interfaces:**
- Consumes: `Order`, `OrderHeader`, `OrderItem` (`app/models/order.py`).
- Produces: `PernaDoLote` (frozen dataclass: `order: Order`, `fire_codigo_origem: int | None`);
  `consolidar(pernas: list[PernaDoLote], *, cnpj_revenda: str, nome_revenda: str, chave_lote: str, fator_preco: Decimal, modo_preco: str) -> Order`;
  `preco_espelho(preco: Decimal, *, fator: Decimal, modo: str) -> Decimal`.

**Contexto medido:** os itens são somados por produto — 168 linhas viraram 65 no lote
`#4619`, com quantidade preservada exata (1.739 → 1.739). **Mas** o mesmo produto aparece
com preços diferentes para clientes diferentes na mesma janela (rede Nacional Lojas,
R$ 32,00 num CD e R$ 36,00 em seis). Por isso o preço entra na chave de agrupamento.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_consolidador_lote.py
from __future__ import annotations

from decimal import Decimal

import pytest

from app.consolidators.lote import PernaDoLote, consolidar, preco_espelho
from app.models.order import Order, OrderHeader, OrderItem


def _perna(codigo, itens, numero="X"):
    return PernaDoLote(
        order=Order(
            header=OrderHeader(order_number=numero, customer_name="C",
                               customer_cnpj="1", issue_date="24/08/2026"),
            items=itens,
        ),
        fire_codigo_origem=codigo,
    )


def _item(desc, qtd, preco, entrega="21/08/2026", code="P1"):
    return OrderItem(description=desc, product_code=code, quantity=qtd,
                     unit_price=preco, delivery_date=entrega)


# ── preco ────────────────────────────────────────────────────────────────

def test_modo_divisor_reproduz_o_que_esta_no_fire():
    """Pedido 4676 real: 16,12 na Nasmar virou 15,0654205607477 na MM."""
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="divisor") \
        == Decimal("15.0654")


def test_modo_desconto_e_uma_conta_diferente():
    """'Nota -7%' lido ao pe da letra da outro numero. ~R$ 15 mil/ano de
    diferenca sobre o volume de 2026 — por isso o modo e explicito."""
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="desconto") \
        == Decimal("14.9916")


def test_fator_zero_espelha_o_preco():
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0"), modo="divisor") \
        == Decimal("16.12")


def test_modo_invalido_levanta():
    with pytest.raises(ValueError):
        preco_espelho(Decimal("1"), fator=Decimal("0.07"), modo="markup")


# ── consolidacao ─────────────────────────────────────────────────────────

def _consolidado(pernas, fator="0"):
    return consolidar(pernas, cnpj_revenda="34513679000134",
                      nome_revenda="NASMAR COMERCIO DE ROUPAS LTDA",
                      chave_lote="NAS-2026-S34", fator_preco=Decimal(fator),
                      modo_preco="divisor")


def test_soma_quantidade_do_mesmo_produto_mesmo_preco_mesma_data():
    """168 linhas viraram 65 no lote #4619 real, com qtd preservada."""
    o = _consolidado([
        _perna(1157, [_item("MEIA", 100.0, 8.19)]),
        _perna(1158, [_item("MEIA", 200.0, 8.19)]),
    ])
    assert len(o.items) == 1
    assert o.items[0].quantity == 300.0


def test_precos_diferentes_do_mesmo_produto_NAO_se_fundem():
    """Caso Nacional Lojas: um CD a 32,00 e seis a 36,00, mesmo produto,
    mesma semana. Fundir obrigaria a escolher um preco — e escolher errado
    e base de calculo errada na nota."""
    o = _consolidado([
        _perna(1, [_item("KIT", 10.0, 32.00)]),
        _perna(2, [_item("KIT", 20.0, 36.00)]),
    ])
    assert len(o.items) == 2
    assert sorted(i.quantity for i in o.items) == [10.0, 20.0]
    assert sorted(i.unit_price for i in o.items) == [32.00, 36.00]


def test_datas_de_entrega_diferentes_nao_se_fundem():
    o = _consolidado([
        _perna(1, [_item("MEIA", 10.0, 8.19, entrega="19/08/2026")]),
        _perna(2, [_item("MEIA", 20.0, 8.19, entrega="27/08/2026")]),
    ])
    assert len(o.items) == 2
    assert {i.delivery_date for i in o.items} == {"19/08/2026", "27/08/2026"}


def test_cliente_do_lote_e_a_revenda():
    o = _consolidado([_perna(1157, [_item("MEIA", 10.0, 8.19)])])
    assert o.header.customer_cnpj == "34513679000134"
    assert o.header.customer_name == "NASMAR COMERCIO DE ROUPAS LTDA"


def test_pedido_cliente_do_lote_e_a_chave():
    o = _consolidado([_perna(1157, [_item("MEIA", 10.0, 8.19)])])
    assert o.header.order_number == "NAS-2026-S34"


def test_fator_aplicado_a_todos_os_itens():
    o = _consolidado([_perna(1, [_item("KIT", 300.0, 16.12)])], fator="0.07")
    assert o.items[0].unit_price == pytest.approx(15.0654)


def test_lote_vazio_levanta():
    """Fechar lote sem perna nenhuma e bug, nao caso valido."""
    with pytest.raises(ValueError):
        _consolidado([])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_consolidador_lote.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.consolidators.lote'`

- [ ] **Step 3: Write the implementation**

```python
# app/consolidators/lote.py
"""Consolida N pedidos de origem em 1 pedido da perna espelho.

Puro: sem Firebird, sem SQLite, sem HTTP. Recebe `Order` e devolve `Order`.

Duas regras nao-obvias, as duas medidas na Fire viva:

1. Itens sao SOMADOS por produto — 168 linhas viraram 65 no lote #4619 com
   quantidade preservada exata (1.739 -> 1.739).

2. Mas o preco entra na chave de agrupamento. O mesmo produto aparece com
   precos diferentes para clientes diferentes na mesma janela (rede Nacional
   Lojas: R$ 32,00 num CD e R$ 36,00 em seis outros, `KIT C/3 PARES DE MEIA
   CANO MEDIO BRANCA`). Fundir obrigaria a escolher um preco, e escolher
   errado e base de calculo errada na nota fiscal. Nos 99,4% dos casos sem
   divergencia o resultado e identico.

O calculo e todo em Decimal. `OrderItem.unit_price` e float
(app/models/order.py:20); converte na borda e quantiza antes de devolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.models.order import Order, OrderHeader, OrderItem

_QUANT = Decimal("0.0001")  # 4 casas, como o Fire guarda
MODOS_PRECO = ("divisor", "desconto")


@dataclass(frozen=True)
class PernaDoLote:
    order: Order
    fire_codigo_origem: int | None = None


def preco_espelho(preco: Decimal, *, fator: Decimal, modo: str) -> Decimal:
    """Preco da perna espelho.

    'divisor'  -> preco / (1 + fator). E o que esta gravado no Fire hoje:
                  16,12 na Nasmar virou 15,0654205607477 na MM.
    'desconto' -> preco * (1 - fator). Leitura literal de "nota menos 7%".

    Sao contas diferentes (~0,46% do valor) e a escolha e comercial, nao
    tecnica. Por isso o modo e explicito e nao tem default aqui.
    """
    if modo == "divisor":
        return (preco / (Decimal("1") + fator)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    if modo == "desconto":
        return (preco * (Decimal("1") - fator)).quantize(_QUANT, rounding=ROUND_HALF_UP)
    raise ValueError(f"modo_preco invalido: {modo!r} (use {MODOS_PRECO})")


def consolidar(
    pernas: list[PernaDoLote],
    *,
    cnpj_revenda: str,
    nome_revenda: str,
    chave_lote: str,
    fator_preco: Decimal,
    modo_preco: str,
) -> Order:
    """Um Order no nome da revenda, com os itens de todas as pernas."""
    if not pernas:
        raise ValueError("lote sem pernas — nao ha o que consolidar")

    # chave: (produto, descricao, data de entrega, preco JA convertido)
    agrupado: dict[tuple, OrderItem] = {}
    for perna in pernas:
        for item in perna.order.items:
            bruto = Decimal(str(item.unit_price or 0))
            preco = preco_espelho(bruto, fator=fator_preco, modo=modo_preco)
            chave = (
                item.product_code,
                item.description,
                item.delivery_date,
                str(preco),
            )
            existente = agrupado.get(chave)
            if existente is None:
                agrupado[chave] = OrderItem(
                    description=item.description,
                    product_code=item.product_code,
                    ean=item.ean,
                    quantity=item.quantity or 0.0,
                    unit_price=float(preco),
                    delivery_date=item.delivery_date,
                )
            else:
                existente.quantity = (existente.quantity or 0.0) + (item.quantity or 0.0)

    itens = list(agrupado.values())
    for i in itens:
        i.total_price = float(
            (Decimal(str(i.unit_price)) * Decimal(str(i.quantity))).quantize(
                _QUANT, rounding=ROUND_HALF_UP
            )
        )

    return Order(
        header=OrderHeader(
            order_number=chave_lote,
            customer_name=nome_revenda,
            customer_cnpj=cnpj_revenda,
        ),
        items=itens,
        source_file=f"lote:{chave_lote}",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_consolidador_lote.py -v`
Expected: 11 passed

- [ ] **Step 5: Lint and commit**

```bash
ruff check app/consolidators/ tests/test_consolidador_lote.py
ruff format app/consolidators/ tests/test_consolidador_lote.py
git add app/consolidators/lote.py tests/test_consolidador_lote.py
git commit -m "feat(consolidators): consolida pernas em pedido espelho (preco na chave)"
```

---

### Task 10: Anexar a perna de origem ao lote

**Files:**
- Modify: `app/web/server.py` (`_send_one_to_fire`, após `SEND_TO_FIRE_SUCCEEDED`)
- Modify: `app/exporters/firebird_exporter.py` (devolver o `CODIGO` gerado)
- Test: `tests/test_lote_anexo.py`

**Interfaces:**
- Consumes: `lotes_repo` (Task 7), `janela_de` (Task 8).
- Produces: `FirebirdExporter.export(...)` passa a devolver o `CAB_VENDAS.CODIGO` gerado
  no resultado (campo `fire_codigo`), consumido por `_send_one_to_fire`.

**Contexto:** `_send_one_to_fire` (`app/web/server.py:1677`) já grava
`repo.update_fire_metadata(...)` antes do `transition(SEND_TO_FIRE_SUCCEEDED)`. O anexo ao
lote entra logo depois, dentro do mesmo `with with_trace_id(...)`, e é **best-effort**:
falhar aqui não pode desfazer um INSERT que já aconteceu no Firebird.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lote_anexo.py
from __future__ import annotations

from datetime import date

import pytest

from app.persistence import db, lotes_repo


def test_perna_enviada_ao_fire_e_anexada_ao_lote_da_janela(client, env_nasmar, lote_cfg):
    """Depois de SEND_TO_FIRE_SUCCEEDED a perna aparece no lote aberto."""
    entry_id = _importa_pedido_daju(client)
    client.post(f"/api/imported/{entry_id}/send-to-fire")

    with db.connect_shared() as c:
        ini, fim = date(2026, 8, 17), date(2026, 8, 23)
        lote = lotes_repo.lote_aberto(c, "nasmar", ini, fim)
        pernas = lotes_repo.pernas_do_lote(c, lote["id"])
    assert [p["import_id"] for p in pernas] == [entry_id]
    assert pernas[0]["fire_codigo_origem"] is not None


def test_falha_ao_anexar_nao_derruba_o_send_to_fire(client, env_nasmar, lote_cfg, monkeypatch):
    """O INSERT no Firebird ja aconteceu. Perder o anexo e ruim; desfazer o
    envio seria pior — e impossivel."""
    import app.web.server as srv

    def explode(*a, **k):
        raise RuntimeError("shared db fora do ar")

    monkeypatch.setattr(srv.lotes_repo, "anexar_perna", explode)
    entry_id = _importa_pedido_daju(client)
    resp = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert resp.status_code == 200


def test_pedido_de_ambiente_sem_lote_config_nao_anexa(client, env_mm):
    """Ambiente sem lote_config e o caso comum. Nao pode explodir nem criar
    lote fantasma."""
    entry_id = _importa_pedido_sem_rota(client)
    resp = client.post(f"/api/imported/{entry_id}/send-to-fire")
    assert resp.status_code == 200
    with db.connect_shared() as c:
        assert c.execute("SELECT COUNT(*) FROM intercompany_lote").fetchone()[0] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_lote_anexo.py -v`
Expected: FAIL — nenhuma linha em `intercompany_lote_item`.

- [ ] **Step 3: Return the generated CODIGO from the exporter**

In `app/exporters/firebird_exporter.py`, the header PK is already computed before
`INSERT_CAB_VENDAS`. Include it in the returned result dict as `fire_codigo`.

- [ ] **Step 4: Anexar no `_send_one_to_fire`**

In `app/web/server.py`, inside `with with_trace_id(entry.get("trace_id")):`, right after
the `transition(..., SEND_TO_FIRE_SUCCEEDED, ...)`:

```python
        # Anexa a perna ao lote da janela. Best-effort de proposito: o INSERT
        # no Firebird ja aconteceu e nao da pra desfazer. Perder o anexo faz
        # a perna cair no proximo lote; derrubar aqui perderia o pedido.
        try:
            _anexar_ao_lote(entry_id, order, fire_codigo, request_env)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"lote: anexo da perna {entry_id} falhou: {exc}")
```

And the helper:

```python
def _anexar_ao_lote(import_id, order, fire_codigo, request_env) -> None:
    from datetime import date

    from app.consolidators.janela import janela_de
    from app.persistence import lotes_repo

    slug = (request_env or {}).get("slug")
    if not slug:
        return
    with db.connect_shared() as conn:
        cfg = lotes_repo.get_config(conn, slug)
        if cfg is None or not cfg["ativo"]:
            return
        ini, fim = janela_de(date.today(), modo=cfg["janela"],
                             dia_fechamento=cfg["dia_fechamento"])
        lote = lotes_repo.lote_aberto(conn, slug, ini, fim)
        lotes_repo.anexar_perna(
            conn,
            lote_id=lote["id"],
            import_id=import_id,
            env_origem_slug=slug,
            fire_codigo_origem=fire_codigo,
            pedido_cliente=order.header.order_number,
            cnpj_cliente_final=order.header.customer_cnpj,
            razao_cliente_final=order.header.customer_name,
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_lote_anexo.py tests/test_web_server.py -v`
Expected: PASS

- [ ] **Step 6: Lint and commit**

```bash
ruff check app/ tests/test_lote_anexo.py && ruff format app/ tests/test_lote_anexo.py
git add app/web/server.py app/exporters/firebird_exporter.py tests/test_lote_anexo.py
git commit -m "feat(web): anexa perna de origem ao lote apos envio ao Fire"
```

---

### Task 11: O job que fecha o lote

**Files:**
- Create: `app/worker/jobs/fechar_lotes.py`
- Modify: `app/worker/__init__.py` (registrar o job)
- Test: `tests/test_fechar_lotes.py`

**Interfaces:**
- Consumes: `lotes_repo` (7), `janela_de` (8), `consolidar` / `PernaDoLote` (9),
  `repo.insert_import`, `app.state.transition`.
- Produces: `fechar_lotes.run(hoje: date | None = None) -> dict` — contagem por resultado.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fechar_lotes.py
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.persistence import db, lotes_repo
from app.worker.jobs import fechar_lotes


def test_lote_da_janela_vencida_fecha(conn_cfg_com_fator, perna_em_lote):
    r = fechar_lotes.run(hoje=date(2026, 8, 24))  # segunda seguinte
    assert r["fechados"] == 1
    with db.connect_shared() as c:
        lote = lotes_repo.get(c, perna_em_lote["lote_id"])
    assert lote["status"] in ("fechado", "enviado")
    assert lote["import_id_espelho"] is not None


def test_lote_da_janela_corrente_nao_fecha(conn_cfg_com_fator, perna_em_lote):
    """So fecha janela vencida. Fechar a corrente perderia pedidos do dia."""
    r = fechar_lotes.run(hoje=date(2026, 8, 21))  # ainda dentro da janela
    assert r["fechados"] == 0


def test_sem_fator_nao_fecha_e_marca_aguardando(conn_cfg_sem_fator, perna_em_lote):
    """O teste que trava a regressao mais cara do projeto. Preco inferido em
    documento fiscal e imposto errado — sem fator, nao escreve nada."""
    r = fechar_lotes.run(hoje=date(2026, 8, 24))
    assert r["fechados"] == 0
    assert r["aguardando_fator"] == 1
    with db.connect_shared() as c:
        lote = lotes_repo.get(c, perna_em_lote["lote_id"])
    assert lote["status"] == "aguardando_fator"
    assert lote["import_id_espelho"] is None


def test_lote_vazio_e_ignorado_sem_erro(conn_cfg_com_fator):
    """Semana sem pedido nenhum: nada a fazer, nao explode, nao cria pedido."""
    r = fechar_lotes.run(hoje=date(2026, 8, 24))
    assert r["fechados"] == 0
    assert r["erros"] == 0


def test_pedido_atrasado_vai_para_o_proximo_lote(conn_cfg_com_fator, perna_em_lote):
    """#1189 de 11/08 entrou no lote de 14/08 junto com pedidos de 27/07.
    Lote fechado nunca reabre."""
    fechar_lotes.run(hoje=date(2026, 8, 24))
    with db.connect_shared() as c:
        ini, fim = date(2026, 8, 24), date(2026, 8, 30)
        novo = lotes_repo.lote_aberto(c, "nasmar", ini, fim)
        assert novo["id"] != perna_em_lote["lote_id"]
        assert novo["status"] == "aberto"


def test_fator_congelado_no_fechamento(conn_cfg_com_fator, perna_em_lote):
    """Mudar o parametro depois nao reescreve lote fechado."""
    fechar_lotes.run(hoje=date(2026, 8, 24))
    with db.connect_shared() as c:
        lotes_repo.save_config(c, env_origem_slug="nasmar",
                               fator_preco=Decimal("0.12"))
        lote = lotes_repo.get(c, perna_em_lote["lote_id"])
    assert Decimal(lote["fator_preco_aplicado"]) == Decimal("0.07")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_fechar_lotes.py -v`
Expected: FAIL — `ImportError: cannot import name 'fechar_lotes'`

- [ ] **Step 3: Write the job**

```python
# app/worker/jobs/fechar_lotes.py
"""Fecha o lote intercompany da janela vencida.

Para cada `lote_config` ativa: acha o lote da janela ANTERIOR a de hoje, junta
as pernas, consolida e cria a linha de `imports` da perna espelho no ambiente
espelho, deixando-a em `parsed` para a revisao humana de sempre.

NAO envia ao Fire sozinho. O lote espelho passa pelo mesmo preview -> commit
que qualquer pedido, porque um lote de uma semana cheia tem ~170 linhas e
concentra o risco de um jeito que o 1:1 de hoje nao concentra.

Sem `fator_preco` na config, o lote vai para `aguardando_fator` e NADA e
escrito. Preco inferido em documento fiscal e imposto errado.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.consolidators.janela import janela_de
from app.consolidators.lote import PernaDoLote, consolidar
from app.persistence import db, lotes_repo, repo, router
from app.persistence.context import active_env
from app.utils.logger import logger


def run(hoje: date | None = None) -> dict:
    hoje = hoje or date.today()
    resultado = {"fechados": 0, "aguardando_fator": 0, "vazios": 0, "erros": 0}

    with db.connect_shared() as conn:
        configs = lotes_repo.listar_configs_ativas(conn)

    for cfg in configs:
        try:
            _fechar_um(cfg, hoje, resultado)
        except Exception as exc:  # noqa: BLE001 — uma empresa nao derruba as outras
            resultado["erros"] += 1
            logger.exception(f"fechar_lotes: {cfg['env_origem_slug']} falhou: {exc}")

    return resultado


def _fechar_um(cfg: dict, hoje: date, resultado: dict) -> None:
    slug = cfg["env_origem_slug"]
    ini_atual, _ = janela_de(hoje, modo=cfg["janela"],
                             dia_fechamento=cfg["dia_fechamento"])
    ini, fim = janela_de(ini_atual - timedelta(days=1), modo=cfg["janela"],
                         dia_fechamento=cfg["dia_fechamento"])

    with db.connect_shared() as conn:
        lote = lotes_repo.buscar_lote(conn, slug, ini, fim)
        if lote is None or lote["status"] not in ("aberto", "aguardando_fator"):
            return
        pernas_meta = lotes_repo.pernas_do_lote(conn, lote["id"])
        if not pernas_meta:
            resultado["vazios"] += 1
            return

        fator_txt = lote["fator_preco_aplicado"] or cfg["fator_preco"]
        if fator_txt in (None, ""):
            lotes_repo.marcar_aguardando_fator(conn, lote["id"])
            resultado["aguardando_fator"] += 1
            logger.warning(
                f"lote {lote['chave_lote']}: sem fator de preco, nao fechado"
            )
            return
        fator = Decimal(str(fator_txt))
        modo = lote["modo_preco_aplicado"] or cfg["modo_preco"]

    pernas = [
        PernaDoLote(order=o, fire_codigo_origem=m["fire_codigo_origem"])
        for m, o in _carregar_orders(pernas_meta)
    ]
    espelho = consolidar(
        pernas,
        cnpj_revenda=cfg["cnpj_revenda"],
        nome_revenda=cfg["nome_revenda"],
        chave_lote=lote["chave_lote"],
        fator_preco=fator,
        modo_preco=modo,
    )

    codigos = [str(m["fire_codigo_origem"]) for m in pernas_meta
               if m["fire_codigo_origem"]]
    obs = f"LOTE {lote['chave_lote']} | PEDIDOS {slug.upper()}: {', '.join(codigos)}"

    espelho_env = cfg["env_espelho_slug"]
    espelho_id = _env_id(espelho_env)
    import_id = f"lote-{lote['chave_lote']}"
    with active_env(espelho_id, espelho_env):
        repo.insert_import({
            "id": import_id,
            "snapshot": espelho.model_dump(),
            "customer_name": cfg["nome_revenda"],
            "customer_cnpj": cfg["cnpj_revenda"],
            "order_number": lote["chave_lote"],
            "obs_intercompany": obs,
        })

    with db.connect_shared() as conn:
        lotes_repo.fechar(conn, lote_id=lote["id"], fator_preco_aplicado=fator,
                          modo_preco_aplicado=modo, closed_by="job",
                          import_id_espelho=import_id)
    resultado["fechados"] += 1
    logger.info(
        f"lote {lote['chave_lote']} fechado: {len(pernas_meta)} pernas, "
        f"{len(espelho.items)} linhas, fator {fator} modo {modo}"
    )
```

Implement `_carregar_orders` (reads each perna's `imports.snapshot` from its own
environment DB via `router.env_connect`) and `_env_id` (looks up `environments` by slug).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_fechar_lotes.py -v`
Expected: 6 passed

- [ ] **Step 5: Register in the scheduler**

In `app/worker/scheduler.py`, register `fechar_lotes.run` alongside `drain_outbox`,
`poll_fire`, `retention` and `scan_environments`, as
`scheduler.add_job(run_fechar_lotes, "cron", day_of_week="mon", hour=8, ...)` — local
server time, same convention as `retention` at 03:00.

Rafael pediu **segunda de manha** e o Samuel fechou em **8h**: o pedido da MM fica
esperando conferencia na tela quando a operacao chega. De hora em hora fecharia as
00:xx de segunda, de madrugada, sem ninguem para olhar.

A hora **nao** muda quem entra na semana — isso e decidido pela data da importacao
(`date.today()` no `_anexar_ao_lote`) — so decide quando o lote aparece na tela.

- [ ] **Step 6: Lint, full suite, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/worker/ tests/test_fechar_lotes.py
git commit -m "feat(worker): job que fecha o lote intercompany da janela vencida"
```

---

### Task 12: Tela de lotes e informe de fator

**Files:**
- Modify: `app/web/routes_intercompany.py` (rotas de lote)
- Create: `app/web/static/lotes.html`
- Modify: `app/web/static/js/shell.js` (item de menu)
- Test: `tests/test_web_lotes.py`

**Interfaces:**
- Consumes: `lotes_repo` (7).
- Produces: `GET /api/lotes` (lote aberto + últimos fechados); `GET /api/lotes/{id}`
  (pernas + prévia consolidada); `POST /api/lotes/{id}/fator` body
  `{fator, modo}` (`require_user`); `POST /api/lotes/{id}/fechar` (`require_user`);
  `GET /api/lotes/cnpjs-sem-rota` (radar).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_web_lotes.py
from __future__ import annotations

from decimal import Decimal


def test_get_lotes_mostra_o_aberto_com_contagem(client, lote_com_pernas):
    body = client.get("/api/lotes").json()
    assert body["aberto"]["chave_lote"] == "NAS-2026-S34"
    assert body["aberto"]["pernas"] == 2
    assert body["aberto"]["clientes"] == 2


def test_informar_fator_exige_modo_explicito(client, lote_aguardando_fator):
    """'Nota -7%' e ambiguo: divisor da 15,07 e desconto da 14,99 sobre
    16,12. Sem modo, nao grava."""
    lote_id = lote_aguardando_fator["id"]
    assert client.post(f"/api/lotes/{lote_id}/fator", json={"fator": 0.07}).status_code == 422
    ok = client.post(f"/api/lotes/{lote_id}/fator",
                     json={"fator": 0.07, "modo": "divisor"})
    assert ok.status_code == 200
    assert ok.json()["exemplo"]["de"] == 16.12
    assert ok.json()["exemplo"]["para"] == 15.0654


def test_fator_fora_de_faixa_e_rejeitado(client, lote_aguardando_fator):
    """Fator > 1 ou negativo e erro de digitacao, nao politica comercial."""
    lote_id = lote_aguardando_fator["id"]
    for ruim in (-0.07, 1.5, 7):
        r = client.post(f"/api/lotes/{lote_id}/fator",
                        json={"fator": ruim, "modo": "divisor"})
        assert r.status_code == 422, f"fator {ruim} deveria ser rejeitado"


def test_radar_lista_cnpjs_da_janela_sem_rota(client, lote_com_pernas, pedido_cnpj_novo):
    """Cliente novo que ninguem cadastrou volta a cair no ambiente
    selecionado — o bug de hoje. O radar torna isso visivel."""
    body = client.get("/api/lotes/cnpjs-sem-rota").json()
    assert "11222333000181" in [c["cnpj"] for c in body["cnpjs"]]


def test_fechar_manual_exige_fator(client, lote_aguardando_fator):
    lote_id = lote_aguardando_fator["id"]
    r = client.post(f"/api/lotes/{lote_id}/fechar")
    assert r.status_code == 409
    assert "fator" in r.json()["detail"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_web_lotes.py -v`
Expected: FAIL — 404 em `/api/lotes`

- [ ] **Step 3: Write the routes**

Add to `app/web/routes_intercompany.py`. The `POST /api/lotes/{id}/fator` handler:

- validates `modo in ("divisor", "desconto")` → 422 otherwise
- validates `0 <= fator < 1` → 422 otherwise
- persists via `lotes_repo.informar_fator`
- returns a **worked example** computed with `preco_espelho(Decimal("16.12"), ...)` so the
  UI can show *"R$ 16,12 na nota → R$ 15,07 para a MM"* before the operator confirms

`GET /api/lotes/cnpjs-sem-rota` reads the open lot's window, lists distinct
`customer_cnpj` from `imports` in the origin environment in that range, and subtracts the
CNPJs present in `rota_intercompany`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_web_lotes.py -v`
Expected: 5 passed

- [ ] **Step 5: Build the page**

Create `app/web/static/lotes.html` following `admin-usuarios.html` (same app-shell
includes). Sections: open lot card (chave, janela, nº de pernas, nº de clientes, total,
fator vigente); table of pernas (pedido, cliente final, `CODIGO` no Fire, valor); the
fator form when `aguardando_fator`, **showing the worked example live as the operator
types**; "fechar agora" button; and the "CNPJs sem rota" radar list with a shortcut to
`/configuracoes/rotas`.

Register `GET /lotes` and add the sidebar item in `shell.js`. Bump `?v=` on the assets.

- [ ] **Step 6: Lint, full suite, commit**

```bash
ruff check app/ tests/ && ruff format app/ tests/
.venv/bin/pytest tests/ -v
git add app/web/ tests/test_web_lotes.py
git commit -m "feat(web): tela de lotes com informe de fator e radar de CNPJ sem rota"
```

- [ ] **Step 7: Update the module docs**

New section "Lote intercompany" in `docs/ai/modules/environments.md`; new routes in
`docs/ai/modules/web.md`; new job in `docs/ai/modules/worker.md`; the mapper columns in
`docs/ai/modules/erp.md`. Update the test count in `docs/ai/00-index.md`.

```bash
git add docs/ai/
git commit -m "docs(ai): lote intercompany em environments, web, worker e erp"
```

---

## Validação final antes do merge

- [ ] `.venv/bin/pytest tests/ -v` — suíte completa verde
- [ ] `ruff check app/ tests/` — limpo
- [ ] **Validação manual em `.fdb` de cópia** (Task 3, Step 7): pedido inserido pelo
      Portal comparado coluna a coluna contra um digitado pela operação. Registrar o
      diff no PR.
- [ ] **Primeiro lote real conferido a olho antes de faturar.** Um lote de semana cheia
      tem ~170 linhas e concentra o risco de um jeito que o 1:1 de hoje não concentra.
- [ ] Respostas do Rafael às 3 questões abertas da spec refletidas em `lote_config`
      (`modo_preco`, `fator_preco`, `dia_fechamento`) antes de ligar o job. `janela`
      já entra `'semanal'` por decisão de 25/08.
