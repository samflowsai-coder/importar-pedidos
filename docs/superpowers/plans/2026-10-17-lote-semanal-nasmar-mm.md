# Lote semanal Nasmar → MM (Fase 2 do roteamento intercompany) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Toda perna de origem que a Nasmar grava no Fire `.4` entra no lote da sua semana;
na segunda às 8h o lote vira **um** pedido consolidado no ambiente MM (cliente Nasmar,
preço × 0,93), que espera conferência humana e, ao ser cadastrado no `.7`, passa pela
mesma trava de preço de qualquer pedido e deixa o rastro item a item para o Flow
(`lote_fiscal` do SP1).

**Architecture:** Três peças puras em `app/consolidators/` (janela, consolidador,
regras de fechamento) + um repo no `app_shared.db` + dois ganchos finos em
`_send_one_to_fire` (anexar a perna de origem; tratar a perna espelho) que delegam para
`app/consolidators/integracao.py`, para o diff em `server.py` ficar de poucas linhas.
O fechamento roda numa thread do processo web (o worker não roda no Windows da MM), com
recuperação se o servidor estava fora na segunda 8h.

**Tech Stack:** Python 3.11+, pydantic v2, SQLite (`app_shared.db` + `app_state_<slug>.db`),
firebird-driver, FastAPI, pytest, ruff.

**Spec:** [`docs/superpowers/specs/2026-08-24-roteamento-intercompany-nasmar-design.md`](../specs/2026-08-24-roteamento-intercompany-nasmar-design.md)
(Revisão 4) — seções "Perna de origem", "Consolidador", "Chave de lote", "Fator de preço",
"Fechamento do lote", diretrizes 1–11. Base: Tasks 7–12 de
[`2026-08-25-roteamento-intercompany-nasmar.md`](2026-08-25-roteamento-intercompany-nasmar.md),
corrigidas abaixo.
Trava de preço (outra sessão, **não editar**): `docs/superpowers/specs/2026-10-17-tabela-preco-por-cliente-design.md`
na branch `feat/tabela-preco-por-rede`, chamada 8 (§11).

## Global Constraints

- Worktree `../importar pedidos-lote`, branch `feat/lote-semanal-nasmar-mm`, a partir de `origin/main` (630e048).
- **Não editar** `app/erp/product_check.py` nem a lógica de `is_blocking`. A perna espelho chama `check_order` + `is_blocking` exatamente onde qualquer pedido chama hoje (`_send_one_to_fire`).
- **Rebase em `main`** assim que a correção do BACKLOG §2.1 (`depara_apply` dentro de `_send_one_to_fire`) entrar — antes de começar a Task 6.
- Preço em `Decimal`, nunca `float`; `OrderItem.unit_price` é `float` (`app/models/order.py`) → converter na borda com `Decimal(str(x))`, quantizar em `0.0001` (`ROUND_HALF_UP`) antes de devolver.
- Fator e modo guardados como **TEXT** no SQLite; leitura com `Decimal(...)`.
- `modo_preco` default `'desconto'` (`preço × (1 − fator)`); `'divisor'` existe só porque lote fechado guarda o modo que usou.
- `fator` **sem default em lugar nenhum**. Sem fator vigente → lote `aguardando_fator`, nada é escrito.
- Janela default `'semanal'`, fecha na **segunda às 08:00 hora local do servidor**; `'quinzenal'` configurável na tela, sem código.
- Fator e modo **congelam na abertura** do lote (diretriz 10): mudança vale a partir da próxima janela.
- Chave de lote `<3 primeiras letras do slug de origem em maiúsculo>-<ano>-S<semana ISO 2 dígitos>` ou `-Q<quinzena 2 dígitos>`; ≤ 20 caracteres (`PEDIDO_CLIENTE[:20]`).
- `OBS` do espelho: `LOTE <chave> | PEDIDOS <SLUG_ORIGEM>: <CODIGO>, <CODIGO>, ...` (CODIGO = `CAB_VENDAS.CODIGO` da perna no Fire de origem, em ordem crescente).
- Agrupamento de linhas por `(product_code, ean, description, delivery_date, preço já convertido)`. Preços diferentes **não** se fundem.
- Perna espelho **não** vai para o Flow por `/recebimento` (`push_new_order`): no Aponta continua tudo por pedido (diretriz 11). O vínculo vai pelo `lote_fiscal` do SP1.
- O fechamento **não** grava no Fire. Grava no Fire só o botão "Cadastrar no Fire" de sempre.
- Lote fechado nunca reabre. Perna que chega depois vai para o próximo lote aberto.
- Tudo novo nasce desligado: `lote_config.ativo = 0`; sem linha de config, nada acontece. `PORTAL_LOTES=0` desliga a thread de fechamento.
- **Perguntar ao Samuel antes de:** aplicar o schema novo num banco que não seja de teste, qualquer escrita em Fire de produção, push, deploy, gerar cópia nova do `.fdb` (o backup lê o servidor de produção).
- Teste contra Fire só em **cópia** (`tools/fire_copia_local.sh`, no checkout principal; banco em `data/fire_copia/`).
- Lint e teste por task: `ruff check` + `ruff format` nos arquivos tocados; teste direcionado; suíte completa só na Task 9.

## Decisões que este plano toma além da spec

1. **Vigência do fator por janela, não "na primeira perna".** A spec copia o fator para o lote na primeira perna anexada. Se o fator muda na terça e a primeira perna da semana chega na quarta, essa semana pegaria o valor novo — contra a resposta do Rafael ("só vale da segunda seguinte"). Tabela `lote_fator` versionada: salvar um fator cria uma linha com `vigente_desde` = início da **próxima** janela; o lote copia o fator vigente na data de **início da sua janela**.
2. **Janela pela data de importação da perna** (`imports.imported_at`), como a spec diz, com recuo: se o lote dessa janela já fechou, vai para a próxima janela com lote aberto.
3. **Fechamento por thread no processo web, com recuperação**, não cron: o worker/APScheduler não roda no Windows da MM (memória SP1). A cada 5 min a thread fecha todo lote cuja janela venceu (`agora ≥ fim + 1 dia às hora_fechamento`). Servidor fora na segunda 8h → fecha quando voltar.
4. **Perna espelho travada depois do fechamento.** O rastro do `lote_fiscal` é por linha; se alguém mexer em quantidade/preço/número de linhas do espelho antes de cadastrar, o envio bloqueia ("lote alterado depois do fechamento"). Trocar o produto (de-para) continua permitido.
5. **Produto do espelho tem que existir no Fire MM.** `is_blocking` não bloqueia `no_product_match` e o INSERT gravaria `CODPRODUTO NULL`. Para o espelho, item sem produto no `.7` bloqueia o envio — regra do lote, fora de `product_check.py`. É também a rede de segurança da questão aberta da spec ("a Nasmar revende algo que não compra da MM?").
6. **`linhaLote` = `CORPO_VENDAS.CODIGO` da linha do espelho no `.7`** (o vetor de teste do SP1 usa `900011`, formato de CODIGO). `produtoProdutor` = `CODPRODUTO` dessa linha.
7. **Trocar a janela (semanal↔quinzenal) com lote aberto contendo pernas → 409.** Evita dois lotes com intervalos sobrepostos.

## Review Focus

1. **Perna cadastrada no Fire depois do fechamento da sua semana** → entra no próximo lote aberto, nunca reabre o fechado. Teste: `test_perna_de_janela_fechada_vai_para_a_proxima` (Task 4).
2. **Operador mexe nas linhas do espelho antes de cadastrar** → envio bloqueado com motivo claro, nada no Fire. Teste: `test_espelho_alterado_depois_do_fechamento_bloqueia` (Task 6).
3. **Item do espelho sem produto no Fire MM** → envio bloqueado listando os itens, nunca `CODPRODUTO NULL`. Teste: `test_espelho_com_item_sem_produto_no_fire_bloqueia` (Task 6).
4. **Fechamento concorrente** (thread + botão "fechar agora", ou dois ciclos) → um espelho só. Teste: `test_fechar_duas_vezes_cria_um_espelho` (Task 5).
5. **INSERT do espelho deu certo e o registro do `lote_fiscal` falhou** → o pedido no Fire é mantido, o lote fica `enviado` com `lote_fiscal_ok = 0` visível na tela, auditoria gravada. Teste: `test_falha_no_lote_fiscal_nao_desfaz_o_envio` (Task 6).

---

## File Structure

**Criados**

| Arquivo | Responsabilidade |
|---|---|
| `app/consolidators/janela.py` | Puro: data → janela, chave, instante de fechamento, próxima janela |
| `app/consolidators/lote.py` | Puro: N pedidos de origem → 1 pedido espelho + composição por linha |
| `app/persistence/lotes_repo.py` | `lote_config`, `lote_fator`, `intercompany_lote`, `intercompany_lote_item` (shared) |
| `app/consolidators/integracao.py` | Ganchos chamados por `server.py`: anexar perna, preparar/concluir envio do espelho |
| `app/consolidators/fechamento.py` | Fecha lotes vencidos, cria o `imports` do espelho; loop da thread |
| `app/web/routes_lotes.py` | `/lotes`, `/api/lotes*` |
| `app/web/static/lotes.html` | Tela de lotes |
| `docs/ai/modules/consolidators.md` | Módulo novo |
| `tests/test_consolidador_janela.py`, `tests/test_consolidador_lote.py`, `tests/test_lotes_repo.py`, `tests/test_firebird_exporter_lote.py`, `tests/test_lote_anexo.py`, `tests/test_fechar_lotes.py`, `tests/test_lote_espelho_envio.py`, `tests/test_web_lotes.py` | Testes |

**Modificados**

| Arquivo | O quê |
|---|---|
| `app/persistence/schema_shared.py` | 4 tabelas novas (`TABLES_SQL`) |
| `app/exporters/firebird_exporter.py` | `export(..., obs=None)`; `FirebirdExportResult.produtos` |
| `app/web/server.py` | 2 ganchos em `_send_one_to_fire`; startup da thread; `include_router` |
| `app/web/static/js/shell.js` | Item de menu "Lotes" |
| `docs/ai/00-index.md`, `modules/routing.md`, `modules/erp_eventos.md`, `modules/web.md`, `CLAUDE.md` | Roteamento do domínio novo |

---

### Task 1: Janela e chave de lote

**Files:**
- Create: `app/consolidators/janela.py`
- Test: `tests/test_consolidador_janela.py`

**Interfaces:**
- Produces:
  - `MODOS = ("semanal", "quinzenal")`
  - `janela_de(d: date, *, modo: str, dia_inicio: int = 0) -> tuple[date, date]` — início e fim inclusivos; `dia_inicio` 0=segunda.
  - `proxima_janela(fim: date, *, modo: str, dia_inicio: int = 0) -> tuple[date, date]` — a janela que começa em `fim + 1 dia`.
  - `chave_para(env_origem_slug: str, inicio: date, modo: str) -> str`
  - `instante_fechamento(fim: date, *, hora: int) -> datetime` — `datetime.combine(fim + 1 dia, time(hora))`, naive (hora local do servidor).
  - Modo inválido → `ValueError` em todas.

- [ ] **Step 1: Write the failing test** — transcrever os 9 testes da Task 8 do plano de 25/08 (`tests/test_consolidador_janela.py`, linhas 1464–1518), trocando o parâmetro `dia_fechamento` por `dia_inicio`, e somar:

```python
def test_proxima_janela_semanal():
    assert proxima_janela(date(2026, 8, 23), modo="semanal") == (date(2026, 8, 24), date(2026, 8, 30))

def test_proxima_janela_quinzenal_vira_o_mes():
    assert proxima_janela(date(2026, 8, 31), modo="quinzenal") == (date(2026, 9, 1), date(2026, 9, 15))

def test_fecha_na_segunda_8h_depois_do_domingo():
    assert instante_fechamento(date(2026, 8, 23), hora=8) == datetime(2026, 8, 24, 8, 0)

def test_chave_na_virada_de_ano_usa_ano_iso():
    # 2026-12-28 é segunda da semana ISO 53 de 2026; 2027-01-04 é S01 de 2027
    assert chave_para("nasmar", date(2026, 12, 28), "semanal") == "NAS-2026-S53"
    assert chave_para("nasmar", date(2027, 1, 4), "semanal") == "NAS-2027-S01"
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_consolidador_janela.py -v` — Expected: FAIL `ModuleNotFoundError: app.consolidators.janela`.
- [ ] **Step 3: Implement** — o corpo da Task 8 de 25/08 (linhas 1527–1575) serve para `janela_de` e `chave_para`; `chave_para` semanal usa o **ano ISO** (`inicio.isocalendar()[0]`). Docstring do módulo explica o limite de 20 caracteres e a idempotência `PEDIDO_CLIENTE + CLIENTE`.
- [ ] **Step 4: Run** o mesmo comando — Expected: 13 passed.
- [ ] **Step 5: Commit**

```bash
ruff check app/consolidators tests/test_consolidador_janela.py && ruff format app/consolidators tests/test_consolidador_janela.py
git add app/consolidators/janela.py tests/test_consolidador_janela.py
git commit -m "feat(consolidators): janela semanal/quinzenal, chave e instante de fechamento"
```

---

### Task 2: O consolidador

**Files:**
- Create: `app/consolidators/lote.py`
- Test: `tests/test_consolidador_lote.py`

**Interfaces:**
- Consumes: `Order`, `OrderHeader`, `OrderItem` (`app/models/order.py`).
- Produces:
  - `MODOS_PRECO = ("desconto", "divisor")`
  - `preco_espelho(preco: Decimal, *, fator: Decimal, modo: str) -> Decimal` — quantizado em `0.0001`.
  - `@dataclass(frozen=True) PernaDoLote(import_id: str, order: Order, fire_codigo_origem: int)`
  - `@dataclass(frozen=True) Origem(import_id: str, indice: int, quantidade: Decimal)` — um item de origem que alimentou uma linha.
  - `@dataclass(frozen=True) Consolidado(order: Order, composicao: list[list[Origem]], obs: str)` — `composicao[i]` = as origens da linha `order.items[i]`.
  - `consolidar(pernas: list[PernaDoLote], *, env_origem_slug: str, cnpj_revenda: str, nome_revenda: str, chave_lote: str, fator_preco: Decimal, modo_preco: str) -> Consolidado`

Regras: cliente = revenda; `order_number = chave_lote`; `issue_date` = hoje `dd/mm/aaaa`; `source_file = f"lote:{chave}"`; itens **sem** `delivery_cnpj/delivery_name/delivery_ean/obs` (senão o exportador divide por loja); quantidade somada em `Decimal`; `total_price = preço × quantidade` quantizado; ordem das linhas = ordem da primeira aparição (perna por `fire_codigo_origem` crescente, item por posição); `obs` no formato das Global Constraints. Pernas vazias → `ValueError`. Item com `unit_price` `None` ou `quantity` ≤ 0 → `ValueError` com o `import_id` e a posição (nunca chuta preço).

- [ ] **Step 1: Write the failing test** — transcrever os testes da Task 9 de 25/08 (linhas 1612–1726) ajustando à interface nova (`PernaDoLote(import_id=..., order=..., fire_codigo_origem=...)`, `consolidar(...).order`, `env_origem_slug="nasmar"`, `modo_preco="desconto"` no helper `_consolidado`), e somar:

```python
def test_desconto_e_o_default_escolhido_pelo_rafael():
    assert preco_espelho(Decimal("16.12"), fator=Decimal("0.07"), modo="desconto") == Decimal("14.9916")

def test_fator_aplicado_em_desconto():
    c = _consolidado([_perna(1, [_item("KIT", 300.0, 16.12)])], fator="0.07")
    assert c.order.items[0].unit_price == pytest.approx(14.9916)

def test_composicao_aponta_cada_linha_para_as_origens():
    c = _consolidado([
        _perna(1158, [_item("MEIA", 200.0, 8.19)], imp="b"),
        _perna(1157, [_item("MEIA", 100.0, 8.19), _item("KIT", 5.0, 30.0, code="P2")], imp="a"),
    ])
    assert len(c.order.items) == 2
    meia = c.composicao[0]
    assert {(o.import_id, o.indice, o.quantidade) for o in meia} == {
        ("a", 0, Decimal("100")), ("b", 0, Decimal("200"))}

def test_obs_lista_os_codigos_de_origem_em_ordem():
    c = _consolidado([_perna(1161, [_item("A", 1.0, 1.0)]), _perna(1157, [_item("A", 1.0, 1.0)])])
    assert c.obs == "LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157, 1161"

def test_espelho_nao_carrega_local_de_entrega():
    it = _item("MEIA", 10.0, 8.19)
    it.delivery_cnpj = "05055599002802"
    c = _consolidado([_perna(1, [it])])
    assert c.order.items[0].delivery_cnpj is None

def test_item_sem_preco_levanta_em_vez_de_chutar():
    with pytest.raises(ValueError, match="sem preço"):
        _consolidado([_perna(1, [_item("MEIA", 10.0, None)])])

def test_quantidade_soma_em_decimal_sem_ruido_de_float():
    c = _consolidado([_perna(1, [_item("M", 0.1, 1.0)]), _perna(2, [_item("M", 0.2, 1.0)])])
    assert c.order.items[0].quantity == 0.3
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_consolidador_lote.py -v` — Expected: FAIL `ModuleNotFoundError`.
- [ ] **Step 3: Implement** em `app/consolidators/lote.py`. Docstring do módulo carrega os dois fatos medidos (168→65 linhas no `#4619`; Nacional Lojas R$ 32/36) e o porquê do `Decimal`.
- [ ] **Step 4: Run** — Expected: todos passam.
- [ ] **Step 5: Commit** — `feat(consolidators): consolida pernas no pedido espelho com composição por linha`.

---

### Task 3: Tabelas e repo do lote

> ⚠️ **Schema novo em `app_shared.db`.** Aprovar este plano aprova estas 4 tabelas em banco de teste. Aplicar no banco do cliente só via deploy, que é pergunta à parte.

**Files:**
- Modify: `app/persistence/schema_shared.py` (`TABLES_SQL`)
- Create: `app/persistence/lotes_repo.py`
- Test: `tests/test_lotes_repo.py`

**Interfaces:**
- Consumes: `janela.chave_para` (Task 1).
- Produces (toda função recebe `conn` e **não** dá commit, como `erp_vinculo_repo`):

```python
class LoteFechadoError(Exception): ...
class JanelaComLoteAbertoError(Exception): ...

get_config(conn, env_origem_slug: str) -> dict | None
listar_configs(conn, *, so_ativas: bool) -> list[dict]
save_config(conn, *, env_origem_slug: str, env_espelho_slug: str, cnpj_revenda: str,
            nome_revenda: str, janela: str, dia_inicio: int, hora_fechamento: int,
            ativo: bool, por: str, agora: datetime) -> dict
    # trocar `janela` com lote aberto que tem pernas -> JanelaComLoteAbertoError

registrar_fator(conn, *, env_origem_slug: str, fator: Decimal, modo: str,
                vigente_desde: date, por: str, agora: datetime) -> dict
fator_vigente(conn, env_origem_slug: str, em: date) -> tuple[Decimal, str] | None

lote_para(conn, *, env_origem_slug: str, env_espelho_slug: str, inicio: date, fim: date,
          modo_janela: str, agora: datetime) -> dict
    # cria se não existe (INSERT ... ON CONFLICT(chave_lote) DO NOTHING + SELECT),
    # copiando fator_vigente(inicio) para fator_preco_aplicado/modo_preco_aplicado
buscar_lote(conn, env_origem_slug: str, inicio: date) -> dict | None
get(conn, lote_id: int) -> dict | None
lote_do_espelho(conn, import_id: str) -> dict | None
lotes_vencidos(conn, agora: datetime) -> list[dict]   # status aberto|aguardando_fator e instante_fechamento(fim) <= agora
listar(conn, *, limite: int = 20) -> list[dict]

anexar_perna(conn, *, lote_id: int, import_id: str, env_origem_slug: str,
             fire_codigo_origem: int, itens_fire: list[tuple[int, int]],
             pedido_cliente: str | None, cnpj_cliente_final: str | None,
             razao_cliente_final: str | None, agora: datetime) -> None
    # status fora de (aberto, aguardando_fator) -> LoteFechadoError; mesma perna 2x = no-op
pernas_do_lote(conn, lote_id: int) -> list[dict]   # itens_fire volta como list[tuple[int,int]]

marcar_aguardando_fator(conn, lote_id: int) -> None
informar_fator(conn, *, lote_id: int, fator: Decimal, modo: str, por: str, agora: datetime) -> dict
fechar(conn, *, lote_id: int, import_id_espelho: str, composicao: list[list[dict]],
       por: str, agora: datetime) -> bool
    # UPDATE ... WHERE status IN ('aberto','aguardando_fator') AND fator_preco_aplicado IS NOT NULL;
    # devolve rowcount == 1 (False = outro fechamento chegou antes)
marcar_erro(conn, *, lote_id: int, motivo: str, agora: datetime) -> None
marcar_enviado(conn, *, lote_id: int, fire_codigo_espelho: int, lote_fiscal_ok: bool,
               agora: datetime) -> None
```

DDL (append em `TABLES_SQL`; comentário SQL curto em cada tabela com o porquê):

```sql
CREATE TABLE IF NOT EXISTS lote_config (
    env_origem_slug  TEXT PRIMARY KEY,
    env_espelho_slug TEXT NOT NULL,
    cnpj_revenda     TEXT NOT NULL,
    nome_revenda     TEXT NOT NULL,
    janela           TEXT NOT NULL DEFAULT 'semanal',
    dia_inicio       INTEGER NOT NULL DEFAULT 0,
    hora_fechamento  INTEGER NOT NULL DEFAULT 8,
    ativo            INTEGER NOT NULL DEFAULT 0,
    alterado_por     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lote_fator (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    env_origem_slug  TEXT NOT NULL,
    modo_preco       TEXT NOT NULL CHECK (modo_preco IN ('desconto','divisor')),
    fator_preco      TEXT NOT NULL,
    vigente_desde    TEXT NOT NULL,
    informado_por    TEXT NOT NULL,
    informado_em     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lote_fator_vig ON lote_fator(env_origem_slug, vigente_desde);
CREATE TABLE IF NOT EXISTS intercompany_lote (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    chave_lote           TEXT NOT NULL UNIQUE,
    env_origem_slug      TEXT NOT NULL,
    env_espelho_slug     TEXT NOT NULL,
    janela               TEXT NOT NULL,
    janela_inicio        TEXT NOT NULL,
    janela_fim           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'aberto'
                         CHECK (status IN ('aberto','aguardando_fator','fechado','enviado','erro')),
    modo_preco_aplicado  TEXT,
    fator_preco_aplicado TEXT,
    fator_informado_por  TEXT,
    fator_informado_em   TEXT,
    import_id_espelho    TEXT,
    composicao_json      TEXT,
    fire_codigo_espelho  INTEGER,
    lote_fiscal_ok       INTEGER,
    erro_motivo          TEXT,
    created_at           TEXT NOT NULL,
    closed_at            TEXT,
    closed_by            TEXT,
    sent_at              TEXT
);
CREATE TABLE IF NOT EXISTS intercompany_lote_item (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    lote_id             INTEGER NOT NULL REFERENCES intercompany_lote(id),
    import_id           TEXT NOT NULL,
    env_origem_slug     TEXT NOT NULL,
    fire_codigo_origem  INTEGER NOT NULL,
    itens_fire_json     TEXT NOT NULL,
    pedido_cliente      TEXT,
    cnpj_cliente_final  TEXT,
    razao_cliente_final TEXT,
    created_at          TEXT NOT NULL,
    UNIQUE (env_origem_slug, import_id)
);
```

`UNIQUE (env_origem_slug, import_id)` garante que uma perna vive num lote só.

- [ ] **Step 1: Write the failing test** — fixture `conn` igual à de 25/08 (linhas 1287–1293). Transcrever os 6 testes de 25/08 (linhas 1296–1352) na interface nova e somar:

```python
def test_fator_vale_so_a_partir_da_janela_seguinte(conn):
    # salvo na quarta 19/08, com vigência a partir de 24/08
    lotes_repo.registrar_fator(conn, env_origem_slug="nasmar", fator=Decimal("0.07"),
        modo="desconto", vigente_desde=date(2026, 8, 24), por="s", agora=AGORA)
    a = _lote(conn, date(2026, 8, 17))          # semana já em curso
    b = _lote(conn, date(2026, 8, 24))
    assert a["fator_preco_aplicado"] is None
    assert b["fator_preco_aplicado"] == "0.07" and b["modo_preco_aplicado"] == "desconto"

def test_mudar_o_fator_nao_reescreve_lote_ja_aberto(conn): ...
    # registrar 0.07 vigente 17/08, abrir lote de 17/08, registrar 0.12 vigente 17/08
    # -> o lote continua "0.07"

def test_mesma_perna_em_dois_lotes_e_recusada(conn): ...
    # anexar import "imp-1" no lote A e depois no lote B -> sqlite3.IntegrityError

def test_fechar_duas_vezes_so_vale_uma(conn): ...
    # fechar(...) True na primeira, False na segunda

def test_fechar_sem_fator_nao_fecha(conn): ...
    # lote sem fator -> fechar(...) devolve False, status continua 'aberto'

def test_trocar_janela_com_lote_aberto_com_perna_levanta(conn): ...

def test_lotes_vencidos_respeita_a_hora(conn): ...
    # lote 17–23/08: não vence em 24/08 07:59, vence em 24/08 08:00
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_lotes_repo.py -v` — Expected: FAIL `ImportError: lotes_repo`.
- [ ] **Step 3: Add the DDL** acima.
- [ ] **Step 4: Implement `lotes_repo.py`** com a interface acima.
- [ ] **Step 5: Run** — Expected: todos passam. Rodar também `.venv/bin/pytest tests/test_roteamento_repo.py tests/test_decisao_ambiente_repo.py -v` (schema compartilhado).
- [ ] **Step 6: Commit** — `feat(persistence): lote intercompany — config, fator com vigência, lote e pernas`.

---

### Task 4: Exportador devolve produto por linha e grava OBS; perna de origem entra no lote

**Files:**
- Modify: `app/exporters/firebird_exporter.py`
- Create: `app/consolidators/integracao.py` (parte 1)
- Modify: `app/web/server.py` (`_send_one_to_fire`, gancho 1)
- Test: `tests/test_firebird_exporter_lote.py`, `tests/test_lote_anexo.py`

**Interfaces:**
- Consumes: `lotes_repo` (3), `janela` (1).
- Produces:
  - `FirebirdExporter.export(order, *, override_client_id: int | None = None, produtos_por_item: dict[int, int] | None = None, obs: str | None = None)` — `obs` vai para `order_to_cabvendas(..., obs=obs)` (`produtos_por_item` veio do PR #87, BACKLOG §2.1).
  - `FirebirdExportResult.produtos: dict[int, int]` — posição em `order.items` → `CODPRODUTO` gravado (o `product_seq` do `_find_product`; desde o PR #87 item sem produto recusa o pedido inteiro com `PRODUCT_NOT_FOUND`, então nunca há `None`).
  - `integracao.anexar_perna_de_origem(import_id: str, entry: dict, order: Order, result: FirebirdExportResult, request_env: dict | None, *, agora: datetime | None = None) -> int | None` — devolve o `lote_id` ou `None`. Nunca levanta (log `lote: ...` e `None`).

Regra do anexo: ambiente atual é origem de uma `lote_config` **ativa**; data = `imports.imported_at` (`entry["imported_at"]`); janela pela config; lote dessa janela; se `LoteFechadoError` → `proxima_janela` até achar uma aberta (máx. 8 saltos, depois log de erro). `itens_fire` = `result.itens`.

No `server.py`, uma linha logo depois de `transition(..., SEND_TO_FIRE_SUCCEEDED, ...)`, antes do vínculo SP1:

```python
        integracao.anexar_perna_de_origem(import_id, entry, order, result, request_env)
```

- [ ] **Step 1: Write the failing tests**

`tests/test_firebird_exporter_lote.py` — reaproveitar o cursor falso de `tests/test_firebird_exporter_override.py`:

```python
def test_obs_chega_no_insert_do_cabecalho(fake_fb): ...
    # export(order, obs="LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157")
    # -> o parâmetro OBS do INSERT_CAB_VENDAS é exatamente essa string

def test_sem_obs_continua_none(fake_fb): ...

def test_resultado_traz_o_produto_de_cada_posicao(fake_fb): ...
    # 2 itens, _find_product devolve (501,'KIT') e (502,'UN') -> produtos == {0: 501, 1: 502}
```

`tests/test_lote_anexo.py` — fixture `portal_lote`: montar como o `portal` de `tests/test_routing_wiring.py:28-65` (ambientes `nasmar` e `mm`), `lote_config` ativa `nasmar→mm`, `FirebirdExporter.export` trocado por um dublê que devolve `FirebirdExportResult(fire_codigo=1157, itens=[(9001, 0)], produtos={0: 501}, ...)`, `check_order` trocado para `{"available": False}`.

```python
def test_perna_enviada_ao_fire_entra_no_lote_da_semana_da_importacao(portal_lote): ...
    # pedido importado em 2026-08-20 (quinta), enviado em 2026-08-25
    # -> lote NAS-2026-S34 tem a perna com fire_codigo_origem 1157 e itens [(9001, 0)]

def test_perna_de_janela_fechada_vai_para_a_proxima(portal_lote): ...
    # lote S34 fechado; perna importada em 20/08 -> entra em S35

def test_falha_ao_anexar_nao_derruba_o_envio(portal_lote, monkeypatch): ...
    # lotes_repo.anexar_perna levanta -> POST send-to-fire continua 200

def test_ambiente_sem_config_ou_config_inativa_nao_anexa(portal_lote): ...
    # envio no ambiente mm, e com nasmar ativo=0 -> zero linhas em intercompany_lote
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_firebird_exporter_lote.py tests/test_lote_anexo.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** o exportador (o comentário "OBS fica None aqui" em `firebird_exporter.py:218` vira a explicação do parâmetro) e `integracao.anexar_perna_de_origem`; gancho no `server.py`.
- [ ] **Step 4: Run** os dois arquivos + `tests/test_web_server.py tests/test_firebird_exporter_override.py tests/test_erp_eventos_*.py` — Expected: PASS (o `to_dict` não muda; `registrar_insert` segue com `itens`).
- [ ] **Step 5: Commit** — `feat(lote): perna de origem entra no lote; exportador grava OBS e devolve produto por linha`.

---

### Task 5: Fechamento — o espelho nasce no ambiente MM

**Files:**
- Create: `app/consolidators/fechamento.py`
- Modify: `app/web/server.py` (startup da thread, ao lado de `_iniciar_envio_flowpcp`)
- Test: `tests/test_fechar_lotes.py`

**Interfaces:**
- Consumes: `lotes_repo` (3), `consolidar`/`PernaDoLote` (2), `instante_fechamento` (1), `repo.insert_import`, `repo.append_audit`, `transition(..., LifecycleEvent.IMPORTED, ...)`, `env_context.active_env`, `environments_repo.get_by_slug`.
- Produces:
  - `fechar_vencidos(agora: datetime | None = None) -> dict` — `{"fechados", "aguardando_fator", "vazios", "erros"}`.
  - `fechar_lote(lote_id: int, *, por: str, agora: datetime | None = None) -> dict` — `{"resultado": "fechado"|"aguardando_fator"|"vazio"|"ja_fechado"|"erro", "import_id_espelho": str | None, "motivo": str | None}`. Usado pela thread e pelo botão "fechar agora" (Task 8).
  - `habilitado() -> bool` — `os.environ.get("PORTAL_LOTES") != "0"`.
  - `loop() -> None` — a cada 300 s, `fechar_vencidos()` sob `threading.Lock` de processo; exceção vira log, nunca mata a thread.

Sequência de `fechar_lote` (a ordem importa):
1. Lote sem pernas → `vazio` (não cria pedido; status não muda).
2. Sem `fator_preco_aplicado` → `marcar_aguardando_fator`, `aguardando_fator`. Nada mais.
3. Carrega cada perna: `env_context.active_env(env_origem)` + `repo.get_import(import_id)` → `Order.model_validate(snapshot)`. Falha → `marcar_erro` com o motivo, `erro`.
4. `consolidar(...)`. `ValueError` → `marcar_erro`, `erro`.
5. `import_id_espelho = f"lote-{chave}"`. Sob `active_env(env_espelho)`: `repo.insert_import` com `portal_status="parsed"`, `source_filename=f"Lote {chave}.lote"`, `order_number=chave`, `customer_cnpj`/`customer` = revenda, `snapshot=order.model_dump()`, `imported_at` = agora; `append_audit(..., "imported_to_portal", {"source": "lote_intercompany", "chave_lote", "pernas", "linhas", "fator", "modo"})`; `transition(IMPORTED)` só se a linha era nova (re-execução não transiciona de novo).
6. `lotes_repo.fechar(...)` com a `composicao` serializada (`[[{"import_id","indice","quantidade"}...]...]`). Devolveu `False` → `ja_fechado` (o `insert_import` é upsert com id determinístico: não duplica).

Thread em `server.py`:

```python
@app.on_event("startup")
def _iniciar_fechamento_lotes() -> None:
    from app.consolidators import fechamento
    if fechamento.habilitado():
        threading.Thread(target=fechamento.loop, name="lotes-fechamento", daemon=True).start()
```

Testes desligam com `PORTAL_LOTES=0` no `tests/conftest.py` (`pytest_configure`, ao lado de `PORTAL_ERP_VIGIA`).

- [ ] **Step 1: Write the failing test** — transcrever os 6 testes da Task 11 de 25/08 (linhas 2029–2081) trocando `run(hoje=date)` por `fechar_vencidos(agora=datetime(2026, 8, 24, 8, 0))` e fator por `lotes_repo.registrar_fator`, e somar:

```python
def test_nao_fecha_antes_das_8h(portal_lote_com_perna):
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 7, 59))["fechados"] == 0

def test_fecha_atrasado_se_o_servidor_estava_fora(portal_lote_com_perna):
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 26, 10, 0))["fechados"] == 1

def test_espelho_nasce_no_ambiente_mm_em_revisao(portal_lote_com_perna):
    # imports do ambiente mm tem "lote-NAS-2026-S34", portal_status 'parsed',
    # customer_cnpj da Nasmar, preço 16,12 -> 14,9916; nada no ambiente nasmar

def test_fechar_duas_vezes_cria_um_espelho(portal_lote_com_perna):
    # fechar_vencidos 2x -> 1 linha em imports do mm, 1 evento 'imported' no audit

def test_snapshot_ilegivel_marca_erro_e_nao_cria_espelho(portal_lote_com_perna): ...

def test_fechamento_nao_chama_o_fire(portal_lote_com_perna, monkeypatch):
    # FirebirdExporter.export trocado por um que levanta -> fechamento ok
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_fechar_lotes.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** `fechamento.py` e o startup.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(lote): fechamento cria o pedido espelho no ambiente MM, com recuperação`.

---

> **Checkpoint antes da Task 6:** `git fetch origin && git rebase origin/main` se o fix do BACKLOG §2.1 já entrou. Se ainda não entrou, seguir e rebasear no fim — o gancho 2 fica **depois** do `is_blocking` e não toca a linha do `depara_apply`.

### Task 6: Envio da perna espelho — mesma trava, rastro para o Flow

**Files:**
- Modify: `app/consolidators/integracao.py` (parte 2)
- Modify: `app/web/server.py` (`_send_one_to_fire`, gancho 2)
- Test: `tests/test_lote_espelho_envio.py`

**Interfaces:**
- Consumes: `lotes_repo.lote_do_espelho/marcar_enviado/pernas_do_lote` (3), `FirebirdExportResult.produtos` (4), `lote_fiscal.reservar_lote/registrar_lote_fiscal/cancelar_reserva/PernaLote`, `erp_bases_repo.obter`, `router.env_connect`.
- Produces:
  - `@dataclass EnvioEspelho(lote: dict, obs: str, reservado: bool)`
  - `integracao.preparar_envio_espelho(import_id: str, order: Order, check: dict, request_env: dict | None) -> EnvioEspelho | BloqueioEspelho | None` — `None` = não é espelho (caminho de sempre). `@dataclass BloqueioEspelho(motivo: str, detalhe: dict)` = não envia.
  - `integracao.concluir_envio_espelho(envio: EnvioEspelho, result: FirebirdExportResult, request_env: dict | None) -> None` — nunca levanta.
  - `integracao.abortar_envio_espelho(envio: EnvioEspelho, request_env: dict | None) -> None` — cancela a reserva.

Ordem em `_send_one_to_fire` (o `check_order` + `is_blocking` existentes **não mudam e rodam antes**):

```
check_order -> is_blocking (inalterado; bloqueou -> 409 como hoje)
espelho = integracao.preparar_envio_espelho(import_id, order, check, request_env)
  bloqueio -> append_audit("send_to_fire_blocked", {...}) + _FireSendOutcome(False, reason=motivo, 409)
export(order, override_client_id=override, obs=espelho.obs if espelho else None)
  falhou -> abortar_envio_espelho; caminho de falha de sempre
  ok     -> update_fire_metadata, audit, SEND_TO_FIRE_SUCCEEDED (iguais)
            concluir_envio_espelho(...)
            push_new_order SÓ se não for espelho
```

`preparar_envio_espelho`, para o espelho:
1. (Removido após o PR #87: o exportador já recusa o pedido inteiro com `PRODUCT_NOT_FOUND` quando falta produto; o espelho herda isso. Ver Ruling no ledger.)
2. (Idem.)
3. Linhas divergem da composição gravada (contagem, ou `quantity`/`unit_price` de alguma linha) → bloqueio `lote_alterado`.
4. Lote com status ≠ `fechado` → bloqueio `lote_status_<status>`.
5. Se o ambiente espelho tem base SP1 (`erp_bases_repo.obter(env_id)`): `reservar_lote(conn, chave_lote, base_espelho=cfg.base, agora)` no banco do ambiente espelho; `reservado=True`.

`concluir_envio_espelho`: monta `PernaLote` para cada `Origem` de cada linha `i` do espelho — `base_origem` = base SP1 do ambiente de origem; `erp_codigo_origem` = `fire_codigo_origem` da perna; `erp_item_codigo_origem` = o `CORPO_VENDAS.CODIGO` cujo índice é `origem.indice` em `itens_fire`; `produto_produtor` = `result.produtos[i]`; `quantidade` = `origem.quantidade`; `linha_lote` = o `CORPO_VENDAS.CODIGO` da linha `i` em `result.itens`. Chama `registrar_lote_fiscal(conn, tenant=cfg.tenant_id, ...)` e `marcar_enviado(lote_fiscal_ok=True)`. Qualquer falha (base de origem sem SP1, item sem par, exceção) → `marcar_enviado(lote_fiscal_ok=False)` + `append_audit(import_id, "lote_fiscal_falhou", {"motivo": ...})` + log. Sem reserva (MM sem SP1) → `marcar_enviado(lote_fiscal_ok=False)` com motivo `sp1_desligado`.

- [ ] **Step 1: Write the failing test** — fixture `espelho_pronto`: `portal_lote` + perna anexada + fator + `fechar_lote`; dublê de `check_order` que devolve `{"available": True, "items": [...]}` com `price_status` controlável; dublê do exportador.

```python
def test_espelho_passa_pelo_mesmo_is_blocking(espelho_pronto, monkeypatch):
    chamadas = []
    real = product_check.is_blocking
    monkeypatch.setattr(product_check, "is_blocking", lambda *a, **k: chamadas.append(1) or real(*a, **k))
    _enviar(espelho_pronto)
    assert chamadas == [1]

def test_trava_de_preco_bloqueia_o_espelho_como_qualquer_pedido(espelho_pronto): ...
    # item com price_status 'mismatch' -> 409 "preço divergente", exportador não chamado

def test_espelho_com_item_sem_produto_no_fire_bloqueia(espelho_pronto_sp1): ...
    # exportador devolve skipped PRODUCT_NOT_FOUND -> 409, reserva cancelada, lote continua 'fechado'

def test_espelho_alterado_depois_do_fechamento_bloqueia(espelho_pronto): ...
    # snapshot do espelho com quantity da linha 0 mudada -> 409 lote_alterado

def test_espelho_grava_obs_e_nao_vai_para_o_recebimento(espelho_pronto, monkeypatch): ...
    # export recebeu obs "LOTE NAS-2026-S34 | PEDIDOS NASMAR: 1157"; push_new_order não chamado

def test_lote_fiscal_registrado_com_o_rastro_por_item(espelho_pronto_sp1): ...
    # erp_lote_perna no banco do mm: base_origem 'nasmar', erp_item_codigo_origem 9001,
    # produto_produtor 501, linha_lote = CODIGO da linha no .7; reserva removida;
    # lote 'enviado', lote_fiscal_ok 1, fire_codigo_espelho preenchido

def test_insert_falhou_cancela_a_reserva(espelho_pronto_sp1): ...

def test_falha_no_lote_fiscal_nao_desfaz_o_envio(espelho_pronto_sp1, monkeypatch): ...
    # registrar_lote_fiscal levanta -> 200, lote 'enviado' com lote_fiscal_ok 0, audit 'lote_fiscal_falhou'

def test_pedido_comum_nao_muda(portal_lote): ...
    # pedido que não é espelho: export chamado com obs=None, push_new_order chamado
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_lote_espelho_envio.py -v` — Expected: FAIL.
- [ ] **Step 3: Implement** parte 2 de `integracao.py` e o gancho 2 (diff no `server.py`: ~12 linhas).
- [ ] **Step 4: Run** o arquivo + `tests/test_web_server.py tests/test_routing_wiring.py tests/test_erp_eventos_*.py tests/test_lote_anexo.py` — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(lote): espelho passa pela trava de sempre e registra o lote_fiscal do SP1`.

---

### Task 7: API de lotes

**Files:**
- Create: `app/web/routes_lotes.py`
- Modify: `app/web/server.py` (`app.include_router`)
- Test: `tests/test_web_lotes.py`

**Interfaces:**
- Consumes: `lotes_repo` (3), `consolidar`/`preco_espelho` (2), `fechamento.fechar_lote` (5), `janela` (1).
- Produces:

| Rota | Auth | Corpo / resposta |
|---|---|---|
| `GET /api/lotes` | `require_user` | `{"config": {...} \| null, "fator_vigente": {...} \| null, "proximo_fator": {...} \| null, "aberto": {chave, janela_inicio, janela_fim, fecha_em, status, pernas, clientes, linhas_previstas, total_previsto, fator, modo} \| null, "recentes": [...]}` |
| `GET /api/lotes/{lote_id}` | `require_user` | lote + `pernas` (pedido, cliente final, CODIGO no Fire, valor) + `previa` (linhas consolidadas com o fator do lote; `null` se sem fator) |
| `PUT /api/lotes/config` | `require_admin` | `{env_origem_slug, env_espelho_slug, nome_revenda, janela, dia_inicio, hora_fechamento, ativo}` → 409 `JanelaComLoteAbertoError`; `cnpj_revenda` vem de `environments.cnpj` do ambiente de origem; 400 se o `intercompany_cnpj` do ambiente espelho existir e for diferente |
| `POST /api/lotes/fator` | `require_admin` | `{env_origem_slug, fator, modo}` → grava com `vigente_desde` = início da próxima janela; devolve `{"vigente_desde", "exemplo": {"de": 16.12, "para": 14.99}}` |
| `POST /api/lotes/{lote_id}/fator` | `require_user` | só em `aguardando_fator`; `{fator, modo}` → `informar_fator` com o e-mail do usuário; devolve o exemplo |
| `POST /api/lotes/{lote_id}/fechar` | `require_user` | `fechamento.fechar_lote(lote_id, por=user.email)`; `aguardando_fator` → 409 "informe o fator"; `vazio` → 409 |

Validação de corpo com pydantic: `modo in ("desconto","divisor")` obrigatório; `0 <= fator < 1` (`Decimal`); `janela in MODOS`; `0 <= dia_inicio <= 6`; `0 <= hora_fechamento <= 23`. O exemplo usa `preco_espelho(Decimal("16.12"), ...)` arredondado a 2 casas para a tela.

- [ ] **Step 1: Write the failing test** — transcrever `test_informar_fator_exige_modo_explicito`, `test_fator_fora_de_faixa_e_rejeitado`, `test_fechar_manual_exige_fator` da Task 12 de 25/08 (linhas 2268–2300; exemplo agora `14.99` em `desconto`). **Não** transcrever o radar de CNPJ sem rota (morreu na Revisão 4). Somar:

```python
def test_get_lotes_mostra_o_aberto_com_contagem_e_previa(client, lote_com_pernas): ...
def test_config_e_fator_global_exigem_admin(client_operador): ...      # 403
def test_fator_novo_vale_da_proxima_janela(client_admin, lote_com_pernas): ...
def test_trocar_janela_com_lote_aberto_da_409(client_admin, lote_com_pernas): ...
def test_config_recusa_revenda_diferente_do_intercompany_do_espelho(client_admin): ...
def test_fechar_agora_cria_o_espelho(client, lote_com_pernas_e_fator): ...
```

- [ ] **Step 2: Run** `.venv/bin/pytest tests/test_web_lotes.py -v` — Expected: FAIL (404).
- [ ] **Step 3: Implement** `routes_lotes.py`, `include_router`.
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `feat(web): API de lotes — config, fator com vigência, fechar agora`.

---

### Task 8: Tela `/lotes`

**Files:**
- Create: `app/web/static/lotes.html`
- Modify: `app/web/routes_lotes.py` (`GET /lotes` → HTML, `require_user`), `app/web/static/js/shell.js` (item "Lotes", bump `?v=`)
- Test: `tests/test_web_lotes.py` (somar `test_pagina_lotes_responde_200_autenticado`)

Conteúdo, seguindo o app shell de `admin-roteamento.html`:
- **Lote da semana**: chave, janela, "fecha segunda 24/08 às 8h", pernas, clientes, linhas previstas, total previsto, fator e modo **com a conta**: "R$ 16,12 na nota → R$ 14,99 para a MM".
- **Pernas**: pedido do cliente, cliente final, CODIGO no Fire Nasmar, valor.
- **Prévia consolidada** (colapsável): linhas do espelho como vão para o `.7`.
- **Aguardando fator**: campo de fator + modo, exemplo ao vivo enquanto digita, confirmar.
- **Recentes**: chave, status, link para o pedido espelho, `lote_fiscal_ok` ("rastro enviado ao Flow" / "rastro NÃO enviado — ver auditoria").
- **Configuração** (só admin): janela, dia, hora, ativo, fator global com "vale a partir de <data>".
- **Pedidos retidos**: link para `/admin/roteamento` (a fila de pendências já mora lá; não duplicar).

- [ ] **Step 1: Write the failing test** da página.
- [ ] **Step 2: Implement** a página e o menu.
- [ ] **Step 3: Subir** com `portless` (skill `portless`; `appPort` 3636 no `portless.json` se ainda não existir) e abrir `/lotes` com dados de um banco de teste populado pelos fixtures.
- [ ] **Step 4: Validação de design obrigatória** — skill `hm-designer` sobre a tela, nos dois temas do app; corrigir o que reprovar.
- [ ] **Step 5: Commit** — `feat(web): tela de lotes`.

---

### Task 9: Docs, suíte completa e prova em cópia do Fire

**Files:**
- Create: `docs/ai/modules/consolidators.md`
- Modify: `docs/ai/00-index.md` (linha de tarefa → `consolidators`; linha de testes), `docs/ai/modules/routing.md` (Fase 2 existe), `docs/ai/modules/erp_eventos.md` (o `lote_fiscal` tem produtor; remover "não construída"), `docs/ai/modules/web.md` (rotas novas), `CLAUDE.md` (linha de `app/consolidators/` e contagem de testes)
- Create: `tools/lote_validar_copia.py`

- [ ] **Step 1: Docs** — `consolidators.md` com: responsabilidade, arquivos críticos, as 7 decisões deste plano, as tabelas, o fluxo (anexo → fechamento → envio), armadilhas (thread de um processo só; espelho não vai ao `/recebimento`; lote fechado não reabre), testes.
- [ ] **Step 2: Suíte completa** `.venv/bin/pytest tests/ -q` e `ruff check app/ tests/` — Expected: verde; registrar a contagem.
- [ ] **Step 3: Cópia do Fire** — `bash "../importar pedidos/tools/fire_copia_local.sh" status`. Se não houver cópia das duas bases ou estiver velha: **parar e pedir ao Samuel** para gerar (o backup lê o servidor de produção).
- [ ] **Step 4: `tools/lote_validar_copia.py`** — contra a cópia local (porta 3055), nunca produção (recusa host ≠ `localhost`/`127.0.0.1`): pega 3 pedidos reais de uma semana da Nasmar que já viraram lote manual (ex.: as pernas `1157, 1158, 1161` do `#4619`), insere como pernas de origem pelo `FirebirdExporter` na cópia `.4`, fecha o lote, cadastra o espelho na cópia `.7` e imprime: linhas e quantidade antes/depois, preço × 0,93 por linha, `DT_ENTREGA` = menor data, `OBS`, `PEDIDO_CLIENTE`, cliente = `CADASTRO.CODIGO 2`, e a comparação coluna a coluna de `CAB_VENDAS`/`CORPO_VENDAS` contra o lote manual `#4619`.
- [ ] **Step 5: Rodar** o script e colar a saída no PR.
- [ ] **Step 6: Commit** — `docs(ai): módulo consolidators; validação do lote em cópia do Fire`.
- [ ] **Step 7:** Revisão da branch inteira (superpowers:requesting-code-review, modelo diferente do executor); depois superpowers:finishing-a-development-branch. **Push e PR: perguntar antes.**

---

## Ligar em produção (fora deste plano, cada passo com OK do Samuel)

1. Deploy do pacote com tudo desligado (sem `lote_config`).
2. `EXPORT_MODE=db` em MM e Nasmar (decisão desta frente) + trava de preço da outra sessão em `observando`/`ligado`.
3. Cadastrar `lote_config nasmar→mm` com `ativo=1` e o fator 7,00% `desconto` (vale da segunda seguinte).
4. Primeira semana: conferir o espelho a olho antes de cadastrar no Fire (~170 linhas numa semana cheia).
