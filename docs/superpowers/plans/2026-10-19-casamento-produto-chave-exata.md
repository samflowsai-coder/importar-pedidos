# Casamento de produto por chave exata — Plano de implementação (Portal)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** um item de pedido só fica ligado a um produto do Fire por chave exata (EAN
único ativo, de-para confirmado por escopo, `CODPROD_ALTERN` único ativo). Sem chave
exata, o pedido fica salvo, mas não sai para o Fire nem para o Flow até uma pessoa
escolher. A escolha vira de-para.

**Architecture:** uma função pura (`app/erp/produto_chave.py`) decide o estado de cada
item a partir dos candidatos de cada chave. O `check_order` busca esses candidatos (com
`BLOQUEADO` e sem "o menor SEQ vence") e grava o resultado em `item["produto"]`. Um
interruptor de 3 estados por ambiente (`produto_chave_modo`) decide se a decisão só
é observada ou se trava. A trava entra na frente da trava de preço em
`decidir_envio` e no `_run_exporters`. O de-para v2 (`produto_vinculo_v2`) tem
escopo (rede ou raiz do CNPJ), revogação e auditoria.

**Tech Stack:** Python 3.11, FastAPI, SQLite (banco por ambiente e banco compartilhado),
firebird (`FirebirdConnection`), pytest, ruff, HTML/JS puro em
`app/web/static/index.html`.

**Spec:** `docs/superpowers/specs/2026-10-19-casamento-produto-chave-exata-design.md`

**Fora deste plano:** o lado Flow (pcp-app: ligar `/recebimento` pelo `erpProdutoId`),
que ganha plano próprio no pcp-app depois da Task 11.

## Global Constraints

- Nunca casar por descrição, semelhança, prefixo, IA ou `confiancaParser`. A descrição
  só aparece como **sugestão** na tela.
- Produto ativo = `BLOQUEADO <> 'Sim'` (inclui NULL).
- Chave exata vale só com **exatamente 1** produto ativo. Dois ou mais é `ambiguo`,
  nunca "menor SEQ".
- Normalização: EAN = só dígitos. Código = `strip().upper()`, sem tirar espaços nem
  hífens do meio.
- Ordem das chaves e da fonte registrada: `ean` → `depara` → `codprod_altern`.
- Escopo do vínculo: `rede:<rede_id>` (via `precos_repo.resolver_rede`) e depois
  `raiz:<8 dígitos>`. O lookup tenta os dois, nessa ordem.
- Interruptor `produto_chave_modo` ∈ `desligado | observando | ligado`. Nasce
  `desligado`. Em `desligado` nada muda no comportamento de hoje.
- Vínculo nunca é apagado: revogar grava `revogado_em` e `revogado_por`.
- Textos de UI e motivos em português, frases curtas, sem jargão interno.
- Schema novo entra por `TABLES_SQL`, `COLUMN_MIGRATIONS` e `INDEXES_SQL`
  (`app/persistence/schema_env.py`, `schema_shared.py`). **Aplicar em produção pede
  OK do Samuel** (deploy do pacote). A migração dos dados v1→v2 é uma ferramenta
  manual, com dry-run por padrão.
- Lint `ruff check app/ tests/ tools/` e suíte `.venv/bin/pytest tests/` verdes antes
  de cada commit de task (direcionado durante, completo no fim).

## Review Focus

- **EAN com zero à esquerda ou espaço** ("07898…" vindo como EAN-14, " 789… "): só
  dígitos, comparado exatamente. EAN-14 com zero à esquerda não casa com EAN-13
  (fica `a_identificar`, não "quase igual"). Teste na Task 3.
- **Código do cliente com espaço interno** ("KG 07 004" contra o produto "KG07004"):
  não casa. Teste na Task 3.
- **Pedido com itens de raízes de CNPJ diferentes** (agrupado de várias empresas): o
  escopo é calculado por item, não pelo pedido. Teste na Task 2.
- **Dois usuários vinculando a mesma chave ao mesmo tempo:** o índice único parcial
  faz o segundo receber 409 de conflito, e não um vínculo duplo. Teste na Task 7.
- **SEQ do vínculo apagado ou bloqueado no Fire depois de vinculado:** vira
  `vinculo_invalido`, nunca `ok`. Teste na Task 3.

---

## File Structure

| Arquivo | Papel |
|---|---|
| `app/erp/produto_chave.py` (novo) | Decisão pura por item: `Candidato`, `DecisaoProduto`, `decidir` |
| `app/erp/produto_escopo.py` (novo) | CNPJ do escopo por item e lista de escopos (`rede:`/`raiz:`) |
| `app/persistence/produto_vinculo_repo.py` (novo) | De-para v2: `lookup`, `vincular`, `revogar`, `listar` |
| `app/erp/produto_trava.py` (novo) | Modo do interruptor e decisão de trava a partir do check |
| `app/erp/queries.py` | Builders novos com `BLOQUEADO` e `CODPROD_ALTERN` |
| `app/erp/product_check.py` | Busca todos os candidatos e grava `item["produto"]` |
| `app/persistence/schema_env.py`, `schema_shared.py` | Tabela v2, coluna `codprod_altern` no catálogo, coluna `produto_chave_modo` |
| `app/persistence/catalogo_fire_repo.py` | Persistir e devolver `codprod_altern` |
| `app/persistence/environments_repo.py`, `app/web/routes_environments.py` | Ler e gravar o modo, rota admin |
| `app/erp/preco_trava.py` | Trava de produto na frente em `decidir_envio` |
| `app/web/server.py` | `_run_exporters`, `/api/process`, vincular v2, revogar, fila, saída por SEQ ou `CODPROD_ALTERN` |
| `app/integrations/flowpcp/schema.py`, `mapper.py`, `exporter.py`, `hook.py` | `erpProdutoId`, `fonteProduto`, `confiancaParser` |
| `app/web/static/index.html` | Estado do item, picker com candidatos, confirmação lado a lado, fila |
| `main.py` | CLI pula pedido com item fora de `ok` |
| `tools/migrar_produto_vinculo_v2.py` (novo) | Migração v1→v2, dry-run por padrão |
| `tools/regressao_chave_exata.py` (novo) | Regressão contra arquivos originais e Fire (só leitura) |

---

### Task 1: Schema e repositório do de-para v2

**Files:**
- Modify: `app/persistence/schema_env.py` (tabela em `TABLES_SQL`, índices em `INDEXES_SQL`)
- Create: `app/persistence/produto_vinculo_repo.py`
- Test: `tests/test_produto_vinculo_repo.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) Vinculo(id: int, escopo: str, chave_tipo: str, chave_valor: str, fire_seq: int, fire_codigo: str | None, fire_descricao: str, origem: str, criado_em: str, criado_por: str | None)`
  - `class VinculoConflito(Exception)` com `.atual: Vinculo`
  - `norm_chave(tipo: str, valor: str | None) -> str` (`ean` → só dígitos, `codigo` → `strip().upper()`, vazio → `""`)
  - `lookup(conn, escopos: list[str], *, codigos: list[str], eans: list[str]) -> dict[tuple[str, str], Vinculo]`: só vínculos vigentes. Para cada chave, vence o primeiro escopo da lista.
  - `vincular(conn, *, escopo: str, chave_tipo: str, chave_valor: str, fire_seq: int, fire_codigo: str | None, fire_descricao: str, por: str, pedido_id: str | None, substituir: bool = False, origem: str = "manual", agora: str) -> Vinculo`
  - `revogar(conn, vinculo_id: int, *, por: str, agora: str) -> Vinculo`
  - `listar(conn, *, escopo: str | None = None, incluir_revogados: bool = False) -> list[Vinculo]`

Tabela, exatamente:

```sql
CREATE TABLE IF NOT EXISTS produto_vinculo_v2 (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    escopo         TEXT NOT NULL,
    chave_tipo     TEXT NOT NULL CHECK (chave_tipo IN ('codigo','ean')),
    chave_valor    TEXT NOT NULL,
    fire_seq       INTEGER NOT NULL,
    fire_codigo    TEXT,
    fire_descricao TEXT NOT NULL,
    origem         TEXT NOT NULL CHECK (origem IN ('manual','migrado_v1')),
    pedido_id      TEXT,
    criado_em      TEXT NOT NULL,
    criado_por     TEXT,
    revogado_em    TEXT,
    revogado_por   TEXT
);
-- INDEXES_SQL
CREATE UNIQUE INDEX IF NOT EXISTS uq_vinculo_v2_vigente
    ON produto_vinculo_v2 (escopo, chave_tipo, chave_valor) WHERE revogado_em IS NULL;
```

- [ ] **Step 1: Escrever os testes que falham** (fixture `env_conn` igual à de
  `tests/test_produto_depara.py:11-18`)
  - `test_vincular_e_lookup_por_escopo`: vincula `rede:7` com `codigo` igual a
    `"kg02pr "`. `lookup(conn, ["rede:7","raiz:12345678"], codigos=["KG02PR"], eans=[])`
    devolve a chave `("codigo","KG02PR")` com `fire_seq` = 2077.
  - `test_lookup_rede_vence_raiz`: há vínculos para a mesma chave em `rede:7`
    (SEQ 1) e em `raiz:12345678` (SEQ 2). O lookup devolve SEQ 1.
  - `test_vincular_conflito_sem_substituir`: dois `vincular` com SEQs diferentes na
    mesma chave. O segundo levanta `VinculoConflito`, e `.atual.fire_seq` é o
    primeiro SEQ.
  - `test_substituir_revoga_antigo`: com `substituir=True`, o antigo fica com
    `revogado_em` preenchido, o novo fica vigente, e `listar(incluir_revogados=True)`
    devolve os dois.
  - `test_revogado_nao_aparece_no_lookup`.
  - `test_norm_chave`: `norm_chave("ean"," 07898-686 ")` vale `"07898686"`;
    `norm_chave("codigo","KG 07 004")` vale `"KG 07 004"`.
- [ ] **Step 2: Rodar** `.venv/bin/pytest tests/test_produto_vinculo_repo.py -v`.
  Esperado: FAIL (módulo inexistente).
- [ ] **Step 3: Implementar** a tabela e o repo. `vincular` roda numa transação só:
  procura o vigente; se é o mesmo SEQ, devolve o vigente; se é outro SEQ e
  `substituir` é False, levanta `VinculoConflito`; se `substituir` é True, revoga o
  antigo e insere o novo. Um `sqlite3.IntegrityError` do índice único vira
  `VinculoConflito`.
- [ ] **Step 4: Rodar o teste.** Esperado: PASS.
- [ ] **Step 5: Commit** com
  `git commit -m "feat(depara): produto_vinculo_v2 com escopo, revogação e conflito"`

---

### Task 2: Escopo do item e catálogo com `CODPROD_ALTERN`

**Files:**
- Create: `app/erp/produto_escopo.py`
- Modify: `app/persistence/schema_env.py` (linha nova em `COLUMN_MIGRATIONS`:
  `("catalogo_fire", "codprod_altern", "ALTER TABLE catalogo_fire ADD COLUMN codprod_altern TEXT")`)
- Modify: `app/persistence/catalogo_fire_repo.py:19-39` (`replace_all` grava
  `dto.codigo_alternativo`, `list_all` devolve `codprod_altern`)
- Modify: `app/web/server.py:3354-3442` (`/api/produtos/search` devolve `fire_codprod_altern` em cada resultado)
- Test: `tests/test_produto_escopo.py`, `tests/test_catalogo_fire_repo.py` (estender, ou criar se não existir)

**Interfaces:**
- Consumes: `precos_repo.resolver_rede(conn, cnpj) -> Rede | None`;
  `intercompany.resolucao_para(order, *, slug) -> ResolucaoCliente | None`.
- Produces:
  - `cnpj_do_item(order: Order, indice: int, *, cnpj_real: str | None = None) -> str | None`.
    Ordem: `cnpj_real` (cliente real do intercompany), depois
    `order.header.customer_cnpj`, depois `order.items[indice].delivery_cnpj`. Devolve
    só dígitos, ou None se não tiver 14 dígitos.
  - `escopos_para(conn, cnpj: str | None) -> list[str]`: `["rede:<id>", "raiz:<8>"]`,
    só `["raiz:<8>"]` sem rede, `[]` sem CNPJ.
  - `escopo_de_gravacao(conn, cnpj: str | None) -> str | None`: o primeiro de
    `escopos_para`.

- [ ] **Step 1: Escrever os testes que falham**
  - `test_cnpj_do_item_cabecalho_vence_entrega`.
  - `test_cnpj_do_item_sem_cabecalho_usa_entrega_do_item` (caso Riachuelo:
    cabeçalho vazio, item com `"33.200.056/0313-70"`, devolve `"33200056031370"`).
  - `test_cnpj_real_intercompany_vence_cabecalho`.
  - `test_escopos_por_item_em_pedido_misto`: item 0 com raiz `33200056` e item 1 com
    raiz `06347409` dão escopos diferentes.
  - `test_escopos_com_rede_e_sem_rede` (semear `preco_rede_v1` e
    `preco_rede_chave_v1` como em `tests/test_precos_repo.py`).
  - `test_catalogo_persiste_codprod_altern`: `replace_all` com DTO
    `codigo_alternativo="KG02PR"`, e `list_all()[0]["codprod_altern"]` vale `"KG02PR"`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_produto_escopo.py tests/test_catalogo_fire_repo.py tests/test_precos_repo.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(depara): escopo por item e CODPROD_ALTERN no catálogo local"`

---

### Task 3: Decisão pura por item e busca de candidatos no check

**Files:**
- Create: `app/erp/produto_chave.py`
- Modify: `app/erp/queries.py` (builders novos)
- Modify: `app/erp/product_check.py:126-338`
- Test: `tests/test_produto_chave.py`, `tests/test_product_check.py` (estender)

**Interfaces:**
- Consumes: `produto_vinculo_repo.lookup`, `norm_chave` (Task 1); `produto_escopo.cnpj_do_item`, `escopos_para` (Task 2).
- Produces:
  - `@dataclass(frozen=True) Candidato(seq: int, descricao: str, codprod_altern: str | None, bloqueado: bool, unidade: str | None, preco_venda: float | None)`
  - `Estado = Literal["ok","a_identificar","ambiguo","produto_bloqueado","vinculo_invalido","chaves_discordam"]`
  - `@dataclass(frozen=True) DecisaoProduto(estado: Estado, fire_seq: int | None, fonte: str | None, candidatos: tuple[Candidato, ...], motivo: str)`, com `fonte` ∈ `"ean" | "depara" | "codprod_altern"`.
  - `decidir(*, codigo: str | None, ean: str | None, por_ean: list[Candidato], por_codigo: list[Candidato], vinculo_seq: int | None, vinculo_candidato: Candidato | None) -> DecisaoProduto`
  - Builders: `find_produtos_chave_por_ean_sql(n)`, `find_produtos_chave_por_codigo_sql(n)`,
    `find_produtos_chave_por_seq_sql(n)`. Cada um seleciona
    `chave, SEQ, DESCRICAO, PRECO_VENDA, UNIDADE, BLOQUEADO, CODPROD_ALTERN`, com
    `TRIM(...) IN (...)` e `ORDER BY SEQ`. Para SEQ, a chave é o próprio SEQ.
  - `check_order` passa a gravar, em todo item: `item["produto"] = {"estado", "fire_seq", "fonte", "motivo", "candidatos": [{"seq","descricao","codprod_altern","bloqueado"}]}`,
    e `summary["items_produto_pendente"]` = contagem de estado diferente de `ok`.
    Com `available=False`, cada item recebe
    `{"estado": "fire_indisponivel", "fire_seq": None, ...}`. Os campos antigos
    (`match`, `match_source`, `fire_product_id` e o resto) **não mudam**.

Regra de `decidir`, a única lógica que os testes não determinam sozinhos:

```text
resultado de uma chave: lista de candidatos → ativos = não bloqueados
  1 ativo  → produto(seq)
  2+ ativos → problema "ambiguo" (candidatos = ativos)
  0 ativos e algum candidato → problema "produto_bloqueado"
  nenhum candidato → nada
de-para: vinculo_seq None → nada
         vinculo_candidato None ou bloqueado → problema "vinculo_invalido"
         senão → produto(vinculo_seq)
produtos = SEQs distintos dados por ean, depara e codprod_altern
  1 SEQ   → ok, fonte = primeira chave (ean, depara, codprod_altern) que deu esse SEQ
  2+ SEQs → chaves_discordam (candidatos = os produtos de cada chave)
  0 SEQ   → primeiro problema na prioridade vinculo_invalido > ambiguo >
            produto_bloqueado; sem problema → a_identificar
```

Motivos, exatamente:

- `a_identificar`: `"O cliente mandou o código {codigo}, que não está cadastrado no Fire."`
  Sem código: `"O item veio sem código e sem EAN cadastrado no Fire."`
- `ambiguo`: `"O {chave} {valor} está em {n} produtos ativos."`
- `produto_bloqueado`: `"O produto {seq} está bloqueado no Fire."`
- `vinculo_invalido`: `"O vínculo salvo aponta para o produto {seq}, que não existe mais ou está bloqueado."`
- `chaves_discordam`: `"O EAN e o código apontam para produtos diferentes."`
- `ok`: `""`

- [ ] **Step 1: Escrever os testes de `decidir`** (`tests/test_produto_chave.py`)
  - `test_ean_unico_ativo_ok`: fonte `"ean"`.
  - `test_ean_em_dois_ativos_ambiguo`: os 2 candidatos aparecem, e `fire_seq` é None.
  - `test_ean_so_bloqueado`: `produto_bloqueado`.
  - `test_ean_ambiguo_resolvido_por_depara`: o EAN cai em 2 produtos e o vínculo aponta
    para um deles. Resultado `ok` com fonte `"depara"`.
  - `test_chaves_discordam`: EAN → 10, código → 11.
  - `test_ean_e_codigo_concordam_fonte_ean`.
  - `test_vinculo_para_seq_inexistente_invalido` e
    `test_vinculo_para_bloqueado_invalido` (Review Focus).
  - `test_nada_a_identificar_motivo` (motivo exato com `KG02PR`).
- [ ] **Step 2: Escrever os testes do check** (estender `tests/test_product_check.py`).
  Usar um novo `_make_fb_ctx_chave(ean_rows, code_rows, seq_rows)`, roteado pelos
  builders novos, com linhas de 7 colunas.
  - `test_check_grava_produto_ok_por_codprod_altern`.
  - `test_check_ean_duplicado_nao_escolhe_menor_seq`: 2 linhas para o mesmo EAN dão
    `ambiguo`, e o `match_source` antigo continua `"ean"`.
  - `test_check_ean14_com_zero_nao_casa_ean13` (Review Focus).
  - `test_check_codigo_com_espaco_interno_nao_casa` (Review Focus).
  - `test_check_indisponivel_marca_fire_indisponivel`.
  - `test_check_usa_vinculo_v2_por_escopo` (patch `produto_vinculo_repo.lookup` e
    conferir que recebeu os escopos do item).
- [ ] **Step 3: Rodar** `.venv/bin/pytest tests/test_produto_chave.py tests/test_product_check.py -v`. Esperado: FAIL.
- [ ] **Step 4: Implementar.** `check_order` busca os candidatos com os builders
  novos, numa passada a mais e no mesmo cursor, chama `decidir` por item e preenche
  `item["produto"]`. O SEQ dos vínculos v2 é resolvido com
  `find_produtos_chave_por_seq_sql`. A busca antiga continua alimentando os campos
  legados.
- [ ] **Step 5: Rodar o teste.** Esperado: PASS. Rodar também
  `tests/test_preco_trava.py tests/test_preco_avaliacao.py`, que leem o check.
  Esperado: PASS.
- [ ] **Step 6: Commit** com `git commit -m "feat(check): decisão de produto só por chave exata (observada)"`

---

### Task 4: Interruptor `produto_chave_modo`

**Files:**
- Modify: `app/persistence/schema_shared.py` (coluna `environments.produto_chave_modo TEXT NOT NULL DEFAULT 'desligado'` mais a linha em `COLUMN_MIGRATIONS`)
- Modify: `app/persistence/environments_repo.py` (`PRODUTO_MODOS = ("desligado","observando","ligado")` e `set_produto_chave_modo(env_id, valor, *, por) -> None`, igual a `set_preco_trava_modo` em `:364-377`)
- Create: `app/erp/produto_trava.py`
- Modify: `app/web/routes_environments.py` (`PATCH /api/admin/environments/{env_id}/produto-chave`, `require_admin`, body `ProdutoChaveRequest{modo: Literal["desligado","observando","ligado"]}`, com audit no shared igual ao da trava de preço)
- Test: `tests/test_produto_trava.py`, `tests/test_routes_environments.py` (estender)

**Interfaces:**
- Produces:
  - `modo(env: dict | None) -> str`: `"desligado"` se o env é None ou o valor é inválido.
  - `@dataclass(frozen=True) TravaProduto(bloqueado: bool, reason: str | None, itens: list[dict])`.
    `reason` ∈ `"produto_a_identificar" | "fire_indisponivel" | "troca_no_fire" | "sem_codigo_fire"`.
    Cada item é `{"index", "estado", "motivo"}`.
  - `avaliar(check: dict, *, env: dict | None, export_mode: str) -> TravaProduto`.
    Em `desligado` e `observando`, `bloqueado` é sempre False, mas `itens` vem
    preenchido em `observando`. Em `ligado`, trava se `available` é False
    (`fire_indisponivel`), se algum `item["produto"]["estado"]` é diferente de `"ok"`
    (`produto_a_identificar`) e, com `export_mode == "xlsx"`, pelas regras da Task 6.

- [ ] **Step 1: Escrever os testes que falham.**
  - `test_modo_padrao_desligado`.
  - `test_ligado_trava_item_pendente`.
  - `test_ligado_trava_fire_indisponivel`.
  - `test_observando_nao_trava_mas_lista`.
  - `test_patch_produto_chave_admin_only` (403 para não-admin) e
    `test_patch_produto_chave_valor_invalido_422`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_produto_trava.py tests/test_routes_environments.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(trava): interruptor produto_chave_modo por ambiente"`

---

### Task 5: Trava em todos os caminhos que saem para o Fire

**Files:**
- Modify: `app/erp/preco_trava.py:936-983` (`decidir_envio`)
- Modify: `app/web/server.py:456-495` (`_run_exporters`), `:3740-3770` (`/api/process`)
- Modify: `main.py`
- Test: `tests/test_preco_trava.py` (estender), `tests/test_produto_trava_wiring.py` (novo)

**Interfaces:**
- Consumes: `produto_trava.avaliar`, `produto_trava.modo` (Task 4); `item["produto"]` (Task 3).
- Produces:
  - Em `decidir_envio`, logo depois de `live = pc.check_order(...)`:
    `tp = produto_trava.avaliar(live, env=env, export_mode=config.EXPORT_MODE)`. Se
    `tp.bloqueado`, devolve
    `DecisaoEnvio(bloqueado=True, reason=tp.reason, detalhe=<motivo do 1º item>, block_detail={"produto": tp.itens}, metric_labels=["produto"], precos_aplicar={}, avaliacao=None, aplicados=[])`
    antes de qualquer lógica de preço. Em `observando`, grava o audit
    `produto_observado` com `tp.itens`, sem travar.
  - `_run_exporters`: com `produto_trava.modo(env) == "ligado"`, não exporta e devolve
    `out["xlsx_adiado"] = "produto_a_identificar"`. Se a trava de preço também estiver
    ativa, a de produto vem primeiro.
  - `/api/process`: com o modo `ligado`, responde 409 com
    `{"detail": "Trava de produto ligada: use a revisão de pedidos."}`.
  - `main.py`: para cada pedido, roda `check_order(order)`. Se `available` e algum item
    está fora de `ok`, pula o pedido e loga
    `"Pedido {numero} não exportado: item {i} sem produto exato ({motivo})."`.

- [ ] **Step 1: Escrever os testes que falham.**
  - `test_decidir_envio_produto_vem_antes_do_preco`: modo de produto `ligado`, item
    `a_identificar` e preço em `mismatch`. `reason` é `"produto_a_identificar"`.
  - `test_decidir_envio_produto_observando_audita_e_segue`.
  - `test_decidir_envio_desligado_igual_hoje` (os testes atuais de
    `test_preco_trava.py` continuam passando sem mudar).
  - `test_run_exporters_adia_com_produto_ligado` (`/api/import` com `TestClient`,
    `xlsx_adiado == "produto_a_identificar"` e nenhum arquivo em `output/`).
  - `test_process_409_com_produto_ligado`.
  - `test_cli_pula_pedido_com_item_pendente` (patch em `check_order`; `ERPExporter.export`
    não é chamado para esse pedido).
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_preco_trava.py tests/test_produto_trava_wiring.py tests/test_web_server.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(trava): produto trava antes do preço em todo caminho para o Fire"`

---

### Task 6: Saída para o Fire com o produto decidido

**Files:**
- Modify: `app/web/server.py:2378-2396` (`_vinculos_de_para`), `:2462-2506` (envio), `:2696-2725` (export-xlsx)
- Modify: `app/erp/product_check.py` (para item `ok`, roda `_prever_import_fire` também sobre o `CODPROD_ALTERN` do SEQ decidido e grava `item["produto"]["troca_no_fire"]`, `{seq_previsto, codigo}` ou None; o `troca_no_fire` legado não muda)
- Modify: `app/erp/produto_trava.py` (regras de xlsx no `avaliar`)
- Test: `tests/test_produto_saida.py` (novo), `tests/test_product_check.py` (estender)

**Interfaces:**
- Consumes: `item["produto"]` (Task 3); `produto_trava.avaliar` (Task 4).
- Produces:
  - `produto_saida.py` não é necessário: no modo `ligado`, `_vinculos_de_para` é
    substituído por
    `_produtos_decididos(check: dict) -> tuple[list[dict], dict[int, int]]`, que monta,
    a partir dos itens `ok`: os vínculos no formato que `depara_apply.aplicar` já
    aceita (`item_index`, `fire_produto_id`, `fire_codigo=codprod_altern`,
    `fire_ean=None`, `orig=código do cliente`) e o mapa `produtos_por_item` (índice →
    SEQ). Em `desligado` e `observando`, continua `_vinculos_de_para`.
  - `avaliar(..., export_mode="xlsx")` em `ligado` também trava com:
    - `sem_codigo_fire`: item `ok` cujo `candidatos[0].codprod_altern` (ou o
      candidato do SEQ decidido) está vazio. Motivo:
      `"O produto {seq} não tem código no Fire; a planilha não consegue apontá-lo."`
    - `troca_no_fire`: `item["produto"]["troca_no_fire"]` preenchido. Motivo:
      `"O importador do Fire trocaria este código pelo produto {seq_previsto}."`

- [ ] **Step 1: Escrever os testes que falham.**
  - `test_planilha_leva_codprod_altern_e_ref_cliente_na_obs`: cliente com `KG02PR`
    por EAN, SEQ 2077 com `CODPROD_ALTERN` `KG02PR`. Na `ERPRow`, `codigo_produto`
    vale `"KG02PR"` e `obs` contém `"ref cliente: KG02PR"`.
  - `test_planilha_trava_quando_previsao_de_prefixo_diverge` (usar `_fire_fisico` de
    `tests/test_product_check.py:425`, com `NB01-3G` antes de `NB01-3GG`).
  - `test_planilha_trava_sem_codprod_altern`.
  - `test_insert_direto_usa_seq_de_todo_item_ok` (`FirebirdExporter.export` recebe
    `produtos_por_item` com todos os índices).
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_produto_saida.py tests/test_product_check.py tests/test_firebird_exporter_produto.py tests/test_depara_apply.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(saida): Fire recebe o produto decidido (SEQ ou CODPROD_ALTERN)"`

---

### Task 7: Vincular, trocar e revogar (API)

**Files:**
- Modify: `app/web/server.py:3438-3556`
- Test: `tests/test_web_vincular_v2.py` (novo)

**Interfaces:**
- Consumes: `produto_vinculo_repo` (Task 1); `produto_escopo` (Task 2); `check_order` (Task 3).
- Produces:
  - `POST /api/imported/{import_id}/vincular-produto`, com body
    `VincularProdutoRequest{item_index: int, fire_produto_id: str, substituir: bool = False}`:
    - O escopo vem de `escopo_de_gravacao(conn, cnpj_do_item(order, item_index, cnpj_real=...))`.
      Sem escopo, responde 422 com
      `"Este pedido não tem CNPJ do cliente; o vínculo não pode ser reaproveitado."`.
    - Grava a chave `codigo` (se houver) e a `ean` (se houver) no v2 e, enquanto o
      modo do ambiente não for `ligado`, também no v1 (`produto_depara_repo.upsert`),
      para o check legado.
    - Com `VinculoConflito`, responde 409 com
      `{"conflito": {"chave_tipo","chave_valor","atual": {"fire_seq","fire_descricao","criado_por","criado_em"}}}`.
    - Audit `produto_vinculo_criado` ou `produto_vinculo_trocado`, com escopo, chave,
      SEQ antigo e novo, usuário e pedido.
    - Recheca este pedido e todos os pedidos `parsed` do ambiente com item no mesmo
      escopo e na mesma chave fora de `ok`. Responde
      `{"entry_id", "vinculos": [...], "check": <check>, "rechecados": [import_id, ...]}`.
  - `DELETE /api/produtos/vinculo/{vinculo_id}`: chama `revogar`, grava o audit
    `produto_vinculo_revogado` e responde `{"revogado": id}`. O
    `DELETE /api/produtos/depara/{id}` antigo continua como está.
  - `GET /api/produtos/a-identificar`: lista os pedidos `parsed` do ambiente ativo
    cujo `check_json` tem item com `produto.estado` diferente de `ok`. Cada linha é
    `{import_id, pedido, cliente, item_index, codigo, ean, descricao, estado, motivo, importado_em}`,
    em ordem de `importado_em`.

- [ ] **Step 1: Escrever os testes que falham.**
  - `test_vincular_grava_v2_com_escopo_raiz`.
  - `test_vincular_conflito_409_sem_substituir` (Review Focus: o segundo usuário
    recebe 409).
  - `test_vincular_substituir_revoga_e_audita_trocado`.
  - `test_vincular_sem_cnpj_422`.
  - `test_vincular_recheca_outros_pedidos_do_escopo` (dois pedidos Riachuelo de lojas
    diferentes; vincular no primeiro destrava o segundo).
  - `test_revogar_audita_e_some_do_lookup`.
  - `test_fila_a_identificar_lista_so_pendentes_do_ambiente`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.**
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_web_vincular_v2.py tests/test_web_server.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(depara): vincular por escopo, trocar com confirmação, revogar e fila"`

---

### Task 8: Tela — estado do item, picker, confirmação e fila

**Files:**
- Modify: `app/web/static/index.html` (`renderPreview` em `:2378` e a célula de status em `:2600-2660`; `openProductPicker` em `:1901`; `renderProductCandidates` em `:1997`; `confirmProductLink` em `:2036`)
- Test: `tests/test_web_static_produto.py` (novo: confere no HTML servido os ids e textos fixos), mais a validação visual

**Interfaces:**
- Consumes: `item.produto` do check (Task 3); rotas da Task 7.
- Produces (JS):
  - `produtoBadge(produto) -> string HTML`, com rótulos fixos: `ok` vira "Produto
    confirmado"; `a_identificar` vira "Escolher produto"; `ambiguo` vira "Mais de um
    produto"; `produto_bloqueado` vira "Produto bloqueado"; `vinculo_invalido` vira
    "Vínculo inválido"; `chaves_discordam` vira "Chaves discordam";
    `fire_indisponivel` vira "Fire sem resposta". O motivo aparece logo abaixo do
    rótulo.
  - O picker abre com 3 seções, nesta ordem: "Candidatos exatos"
    (`produto.candidatos`), "Sugestões pela descrição" (as que já existem, com a
    marca "sugestão") e a busca.
  - `divergenciasAtributos(descCliente, descProduto) -> {tamanho, pares, cor}`, cada
    um como par ou null. Usa as mesmas regras da auditoria: tamanho `\d{2}\s*(-|A|/)\s*\d{2}`,
    pares `(\d+)\s*PARES` e `(\d)\s*X\s*\d`, cores pela lista BRANC, PRET, CINZA,
    SORTID, MESCLA, AZUL, MARINHO, ROSA, VERDE, VERMELH, BEGE, NUDE, AMAREL. A
    confirmação mostra a linha do cliente e o produto lado a lado, com a divergência
    em destaque, e não bloqueia.
  - Conflito 409: um painel na própria página, sem `confirm()`, com o texto "Este
    código já aponta para {fire_descricao} ({criado_por}, {data}). Trocar para o
    produto escolhido?" e os botões "Trocar" (reenvia com `substituir: true`) e
    "Manter".
  - Item de menu "Itens a identificar": uma lista vinda de
    `GET /api/produtos/a-identificar`, em que cada linha abre o pedido no item.
- [ ] **Step 1: Escrever o teste** que confere a presença de `produtoBadge`,
  `divergenciasAtributos` e dos rótulos fixos no HTML servido em `/`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar**, seguindo o estilo e os tokens já usados no
  `index.html`.
- [ ] **Step 4: Rodar o teste.** Esperado: PASS. Depois **subir local por `portless`**
  (skill `portless`, nunca `python ui.py` cru) com o modo `observando` num ambiente
  de teste e passar a **validação de design** (skill `hm-designer`): estados do
  badge, picker com as 3 seções, confirmação com divergência, painel de conflito e
  fila, em largura de desktop e de telefone.
- [ ] **Step 5: Commit** com `git commit -m "feat(ui): produto a identificar, confirmação lado a lado e fila"`

---

### Task 9: Envio ao Flow com o produto decidido

**Files:**
- Modify: `app/integrations/flowpcp/schema.py:44-56`, `mapper.py:28-83`, `exporter.py:18`, `hook.py:74-93`
- Modify: os chamadores de `push_new_order` em `app/web/server.py` (envio e export), para passar o check
- Test: `tests/test_flowpcp_mapper.py` (estender, ou criar), `tests/test_flowpcp_hook.py` (estender)

**Interfaces:**
- Produces:
  - `ItemRecebimento` ganha `erpProdutoId: int | None = None` e
    `fonteProduto: Literal["ean","depara","codprod_altern"] | None = None`.
  - `build_recebimento_payload(..., produtos: dict[int, tuple[int, str]] | None = None)`,
    com índice → (SEQ, fonte).
    `confiancaParser = "alta"` se todos os itens têm fonte `ean` ou `codprod_altern`,
    `"media"` se algum tem fonte `depara` e `"alta"` (o valor de hoje) quando
    `produtos` é None (modo `desligado`).
  - `push_new_order(order, *, import_id, slug, produtos=None)` e
    `FlowPCPExporter.enqueue(..., produtos=None)` repassam o mapa.
- [ ] **Step 1: Escrever os testes que falham.**
  - `test_payload_leva_erp_produto_id_e_fonte`.
  - `test_confianca_media_com_depara`.
  - `test_payload_sem_produtos_igual_hoje`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar.** Os chamadores montam `produtos` a partir dos itens `ok`
  do check usado no envio, e só no modo `ligado` ou `observando`.
- [ ] **Step 4: Rodar** `.venv/bin/pytest tests/test_flowpcp_hook.py tests/test_flowpcp_mapper.py -v`. Esperado: PASS.
- [ ] **Step 5: Commit** com `git commit -m "feat(flowpcp): itens levam erpProdutoId e fonte do produto"`

---

### Task 10: Migração v1→v2 e regressão contra os pedidos reais

**Files:**
- Create: `tools/migrar_produto_vinculo_v2.py`
- Create: `tools/regressao_chave_exata.py`
- Test: `tests/test_migrar_produto_vinculo_v2.py`

**Interfaces:**
- Produces:
  - `migrar(conn, *, slug: str, aplicar: bool, agora: str) -> dict`, com o resultado
    `{"migrados": [...], "sem_cnpj_unico": [...], "ja_existiam": [...]}`. Uma linha v1
    com CNPJ vira um escopo por `escopo_de_gravacao`. Uma linha v1 com nome procura os
    CNPJs de `imports.customer_cnpj` onde `customer_name` normalizado é igual a ela:
    exatamente uma raiz migra, e o contrário vai para `sem_cnpj_unico`. Grava com
    `origem="migrado_v1"` e `criado_por` da linha v1. Sem `--aplicar`, não escreve.
    CLI: `python tools/migrar_produto_vinculo_v2.py --slug mm [--aplicar]`.
  - `tools/regressao_chave_exata.py --recebidos <dir> --db <app_state_*.db> --slug mm|nasmar`:
    relê cada arquivo pelo `pipeline.process` com o LLM desligado, roda o
    `check_order` novo contra a Fire (só SELECT, transação só de leitura) e compara
    com os itens do pedido ligado na Fire. Imprime `ok_igual`, `ok_seq_diferente` e
    `pendente`. Não escreve em nada.
- [ ] **Step 1: Escrever os testes que falham.**
  - `test_migrar_dry_run_nao_escreve`.
  - `test_migrar_cnpj_vira_raiz`.
  - `test_migrar_nome_com_raiz_unica`.
  - `test_migrar_nome_ambiguo_fica_de_fora`.
  - `test_migrar_idempotente`.
- [ ] **Step 2: Rodar.** Esperado: FAIL.
- [ ] **Step 3: Implementar** as duas ferramentas.
- [ ] **Step 4: Rodar o teste.** Esperado: PASS. Depois rodar a regressão contra a
  cópia da auditoria de 19/10 (scratchpad, fora do git). Esperado:
  `ok_seq_diferente = 0`, e os 18 itens da Riachuelo de 19/10 em `pendente`.
  Anotar a saída no PR.
- [ ] **Step 5: Commit** com `git commit -m "feat(tools): migração do de-para para v2 e regressão por chave exata"`

---

### Task 11: Documentação e suíte completa

**Files:**
- Modify: `docs/ai/modules/erp.md` (seção do check e do de-para), `docs/ai/modules/web.md` (rotas novas), `docs/ai/modules/flowpcp.md` (campos novos do `ItemRecebimento`), `docs/ai/00-index.md` (linha "casamento de produto por chave exata" na tabela de tarefas e linha de testes), `docs/BACKLOG.md` (fecha a 2.15 no modo `ligado`)
- [ ] **Step 1:** Atualizar **só** as seções afetadas.
- [ ] **Step 2: Rodar** `ruff check app/ tests/ tools/` e `.venv/bin/pytest tests/ -q`.
  Esperado: zero erros de lint e a suíte inteira verde.
- [ ] **Step 3: Commit** com `git commit -m "docs: casamento de produto por chave exata"`

---

## Depois do plano (fora do código, cada um com OK do Samuel)

1. Pacote e aplicação no cliente, que cria a tabela v2 e as colunas novas.
2. `migrar_produto_vinculo_v2.py --slug mm`, primeiro em dry-run e depois com
   `--aplicar`. O mesmo para a Nasmar.
3. MM em `observando` por alguns dias, lendo o audit `produto_observado`, e só
   depois `ligado`.
4. Plano do pcp-app: ligar o item pelo `erpProdutoId` (`fire_produto_id`) e nunca
   casar código ou EAN de pedido que vem do Portal.
