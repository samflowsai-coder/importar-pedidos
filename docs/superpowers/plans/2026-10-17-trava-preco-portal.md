# Trava de preço por rede (lado Portal) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** O Portal avalia o preço de cada item contra uma cópia local da tabela do Flow, manda o que diverge para a fila de aprovação do Flow e só libera o envio (Fire ou XLSX) quando todos os itens estão iguais à tabela ou aprovados.

**Architecture:** Cópia da tabela e aprovações moram no `app_state_<slug>.db`. Um avaliador puro (`preco_avaliacao`) decide a situação de cada item a partir do `check_order` (que já identifica o produto pelo SEQ do Fire) e da cópia. Um portão único (`preco_trava.decidir_envio`) substitui o bloco duplicado de `is_blocking` em `_send_one_to_fire` e `_export_one_xlsx`, respeitando o interruptor de 3 estados do ambiente. A conversa com o Flow (sync da tabela, envio da pendência, leitura das decisões) entra na thread que já existe em `envio_portal.ciclo`.

**Tech Stack:** Python 3.11, FastAPI, SQLite (schema_env/schema_shared + COLUMN_MIGRATIONS), pydantic v2, httpx via `FlowPCPClient`, pytest.

**Spec:** `docs/superpowers/specs/2026-10-17-tabela-preco-por-cliente-design.md` (aprovada 17/10). Contrato das rotas do Flow: entregue pela sessão do pcp-app (ver Task 7).

## Global Constraints

- Preço sempre comparado em **centavos inteiros**, tolerância zero (`_to_cents` de `app/erp/product_check.py`).
- Chave do produto = `fire_product_id` do `check_order` = `str(SEQ)` = `produtos.fire_produto_id` do Flow.
- Rede = raiz do CNPJ (8 dígitos); um CNPJ completo (14) cadastrado vence a raiz.
- Interruptor por ambiente: `desligado | observando | ligado`, nasce `desligado`. `desligado` é **bit a bit** o comportamento de hoje.
- `observando` mantém a trava de hoje valendo e não envia nada ao Flow.
- O Portal nunca edita preço. Nenhuma tela de gestão de preço no Portal.
- Cópia com mais de **24 h** sem sync bem-sucedido → item "não avaliado" (bloqueia), com motivo visível.
- Preço aprovado: vai o do pedido; o aprovador só define o preço quando o pedido veio sem preço. Vale para Fire e XLSX.
- Pedido cuja raiz do cliente é uma empresa do grupo (`environments.intercompany_cnpj` de qualquer ambiente) fica fora da trava, com marca.
- Mudança de schema (Tasks 1, 2, 4): **pedir OK do Samuel antes de executar**.
- Lint `ruff check app/ tests/`; não reformatar arquivos que já estavam fora do padrão na main.

## Review Focus

1. **Pedido sem CNPJ no cabeçalho (Riachuelo, Magic Feet):** o CNPJ real vem por loja (`item.delivery_cnpj`). Esperado: se todos os itens têm a mesma raiz, a rede sai dela; raízes diferentes ou nenhuma → não avaliado. Teste na Task 3.
2. **Cliente trocado à mão no preview (`cliente_override_codigo`):** a rede deve sair do cliente escolhido (`check["client"]["cnpj"]` reescrito pelo override), não do cabeçalho. Teste na Task 5.
3. **Item editado depois de aprovado** (preço ou quantidade mudou, arquivo reprocessado): a aprovação antiga não vale. Teste na Task 4 (assinatura).
4. **Fire fora do ar no envio** com pedido já avaliado no commit: usa o `entry["check"]` salvo; nunca avaliado → bloqueia. Teste na Task 5.
5. **Mesmo produto em duas linhas do pedido com preços diferentes:** cada linha é avaliada e aprovada por si (chave = posição do item), nunca agregada por produto. Teste na Task 3.

---

## File Structure

| Arquivo | Responsabilidade |
|---|---|
| `app/persistence/schema_shared.py` | coluna `preco_trava_modo`, `preco_fonte_slug` em `environments` |
| `app/persistence/schema_env.py` | tabelas `preco_rede`, `preco_rede_cnpj`, `preco_tabela`, `preco_sync`, `preco_aprovacao` |
| `app/persistence/precos_repo.py` (novo) | cópia da tabela: aplicar lote do sync, resolver rede, preço vigente, idade |
| `app/persistence/preco_aprovacao_repo.py` (novo) | pendências e decisões por item |
| `app/erp/preco_avaliacao.py` (novo) | avaliador puro, sem I/O |
| `app/erp/preco_trava.py` (novo) | portão: modo + avaliação + aprovações → bloqueia/libera + preços a aplicar |
| `app/integrations/flowpcp/precos.py` (novo) | sync, envio de pendência, leitura de decisões (usa o contrato) |
| `app/integrations/flowpcp/client.py` | 4 métodos novos no `FlowPCPClient` |
| `app/integrations/flowpcp/envio_portal.py` | `ciclo` chama `precos.rodar(agora)` fora das janelas |
| `app/web/server.py` | `_send_one_to_fire` e `_export_one_xlsx` usam o portão; preview expõe a avaliação |
| `app/web/routes_environments.py` + `static/admin-ambiente-edit.html` | interruptor por ambiente |
| `app/web/static/index.html` | coluna/estado de preço no preview |
| `docs/ai/modules/erp.md`, `flowpcp.md`, `environments.md` | seções afetadas |

---

### Task 1: Interruptor da trava por ambiente

**Files:**
- Modify: `app/persistence/schema_shared.py` (COLUMN_MIGRATIONS)
- Modify: `app/persistence/environments_repo.py`
- Modify: `app/web/routes_environments.py`, `app/web/static/admin-ambiente-edit.html`
- Test: `tests/test_preco_trava_modo.py`

**Interfaces:**
- Produces: `environments_repo.set_preco_trava_modo(env_id: str, valor: str, *, por: str) -> None` (levanta `ValueError` fora de `PRECO_MODOS`); `PRECO_MODOS = ("desligado", "observando", "ligado")`; o dict do ambiente passa a ter `preco_trava_modo: str` (default `"desligado"`) e `preco_fonte_slug: str | None` (None = o próprio slug).

- [ ] **Step 1: Testes**
  - `test_ambiente_novo_nasce_desligado`: env criado → `env["preco_trava_modo"] == "desligado"`.
  - `test_set_modo_invalido_recusa_sem_mudar`: `set_preco_trava_modo(id, "talvez", por="x")` → `ValueError`; modo segue `"desligado"`.
  - `test_banco_antigo_migra_coluna`: banco criado sem a coluna, `router` aplica a migração, leitura devolve `"desligado"`.
  - `test_rota_admin_troca_modo`: `PATCH /api/admin/environments/{id}/preco-trava` com `{"modo": "observando"}` como admin → 200 e audit; como operador → 403.
- [ ] **Step 2:** Rodar `.venv/bin/pytest tests/test_preco_trava_modo.py -v` → FAIL.
- [ ] **Step 3:** Colunas `preco_trava_modo TEXT NOT NULL DEFAULT 'desligado'` e `preco_fonte_slug TEXT` em `environments` (CREATE + COLUMN_MIGRATIONS). Setter no repo. Rota com `require_admin` (`app/web/auth.py:143`). No admin do ambiente, um seletor de 3 estados com o texto: "Desligado: como hoje. Observando: avalia e mostra, não bloqueia. Ligado: pedido só sai com preço da tabela ou aprovado no Flow."
- [ ] **Step 4:** Rodar os testes → PASS.
- [ ] **Step 5:** Commit `feat(preco): interruptor da trava de preço por ambiente`.

### Task 2: Cópia local da tabela

**Files:**
- Modify: `app/persistence/schema_env.py`
- Create: `app/persistence/precos_repo.py`
- Test: `tests/test_precos_repo.py`

**Interfaces:**
- Produces:
  - `Rede(id: str, nome: str, travada: bool, travada_motivo: str | None)` (dataclass frozen)
  - `aplicar_lote(conn, *, redes: list[dict], cnpjs: list[dict], precos: list[dict], cursor: str, agora: datetime) -> None` — upsert idempotente; `precos[i] = {rede_id, fire_produto_id, preco_centavos, vigente_desde: date, vigente_ate: date | None, removido: bool}`; `cnpjs[i] = {rede_id, cnpj: str (8 ou 14 dígitos), removido: bool}`.
  - `resolver_rede(conn, cnpj: str) -> Rede | None` — 14 dígitos exato vence a raiz.
  - `preco_vigente(conn, rede_id: str, fire_produto_id: str, data: date) -> int | None` — linha com `vigente_desde <= data` e (`vigente_ate` nulo ou `> data`), maior `vigente_desde`.
  - `estado_sync(conn) -> tuple[str | None, datetime | None]` — (cursor, última sync OK).

- [ ] **Step 1: Testes**
  - `test_cnpj_completo_vence_raiz`: raiz `00063960` → rede A; `00063960059170` → rede B; `resolver_rede(conn, "00.063.960/0591-70").id == "B"`, `resolver_rede(conn, "00063960009408").id == "A"`.
  - `test_preco_vigente_respeita_data`: 52,00 desde 01/10 e 54,60 desde 01/11 → `preco_vigente(..., date(2026,10,30)) == 5200`, `date(2026,11,1) == 5460`.
  - `test_reaplicar_lote_e_idempotente`: aplicar o mesmo lote 2x → mesmas linhas.
  - `test_removido_some`: preço com `removido=True` → `preco_vigente` devolve None.
  - `test_estado_sync_grava_cursor_e_hora`.
- [ ] **Step 2:** Rodar `.venv/bin/pytest tests/test_precos_repo.py -v` → FAIL.
- [ ] **Step 3:** Tabelas `preco_rede(id PK, nome, travada INTEGER, travada_motivo, atualizado_em)`, `preco_rede_cnpj(cnpj PK, rede_id)`, `preco_tabela(rede_id, fire_produto_id, vigente_desde, preco_centavos, vigente_ate, PRIMARY KEY(rede_id, fire_produto_id, vigente_desde))`, `preco_sync(id=1, cursor, ultima_ok_em)`. Datas em ISO.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(preco): cópia local da tabela de preço do Flow`.

### Task 3: Avaliador puro

**Files:**
- Create: `app/erp/preco_avaliacao.py`
- Test: `tests/test_preco_avaliacao.py`

**Interfaces:**
- Consumes: `Rede`, `preco_vigente`, `resolver_rede` (Task 2) — injetados como callables, para o avaliador não fazer I/O.
- Produces:
  - `Situacao = Literal["ok", "fila", "nao_avaliado", "fora_intercompany"]`
  - `Motivo = Literal["igual", "acima", "abaixo", "sem_preco_tabela", "sem_preco_pedido", "rede_travada", "rede_sem_tabela", "produto_nao_identificado", "cliente_sem_cnpj", "copia_desatualizada"]`
  - `ItemAvaliado(item_index: int, fire_produto_id: str | None, situacao: Situacao, motivo: Motivo, preco_pedido_centavos: int | None, preco_tabela_centavos: int | None)`
  - `Avaliacao(rede: Rede | None, cnpj_rede: str | None, itens: list[ItemAvaliado])` com propriedade `pendentes -> list[ItemAvaliado]` (situação `fila`) e `bloqueados -> list[ItemAvaliado]` (`nao_avaliado`).
  - `avaliar(order: Order, check: dict, *, cnpj_cliente: str | None, resolver_rede: Callable[[str], Rede | None], preco_vigente: Callable[[str, str, date], int | None], cnpjs_grupo: set[str], ultima_sync: datetime | None, agora: datetime) -> Avaliacao`
  - `cnpj_da_rede(order: Order, cnpj_cliente: str | None) -> str | None` — `cnpj_cliente` se houver; senão a raiz comum de `item.delivery_cnpj`; raízes diferentes ou nenhuma → None.

Ordem das regras por item (spec 5.3): cópia velha (`agora - ultima_sync > 24h` ou None) → `nao_avaliado/copia_desatualizada` para todos; CNPJ da rede None → `nao_avaliado/cliente_sem_cnpj`; raiz em `cnpjs_grupo` → `fora_intercompany`; `check.items[i].fire_product_id` None → `nao_avaliado/produto_nao_identificado`; rede None → `fila/rede_sem_tabela`; rede travada → `fila/rede_travada`; sem preço vigente → `fila/sem_preco_tabela`; `unit_price` None → `fila/sem_preco_pedido`; igual → `ok/igual`; maior → `fila/acima`; menor → `fila/abaixo`. Data de referência: `order.header.issue_date` (DD/MM/AAAA, via `app.erp.mapper.parse_date`); sem ela, `agora.date()`.

- [ ] **Step 1: Testes** (um por linha da tabela 5.3 da spec, mais):
  - `test_igual_ao_centavo_entra`: tabela 5200, pedido 52.00 → `ok/igual`.
  - `test_um_centavo_acima_vai_para_fila`: pedido 52.01 → `fila/acima`.
  - `test_rede_travada_manda_tudo_para_fila_mesmo_igual`.
  - `test_copia_com_25h_bloqueia_tudo`: `ultima_sync = agora - 25h` → todos `nao_avaliado/copia_desatualizada`.
  - `test_riachuelo_sem_cnpj_usa_raiz_das_lojas`: header sem CNPJ, 2 itens com `delivery_cnpj` de mesma raiz → `cnpj_da_rede` = essa raiz.
  - `test_lojas_de_raizes_diferentes_bloqueia`: → `nao_avaliado/cliente_sem_cnpj`.
  - `test_mesmo_produto_duas_linhas_avalia_cada_uma`: KIT X a 52,00 e a 54,00 → `[ok, fila/acima]`.
  - `test_intercompany_fica_fora`: raiz do cliente em `cnpjs_grupo` → `fora_intercompany`.
  - `test_vigencia_pela_data_de_emissao`: `issue_date="30/10/2026"`, reajuste em 01/11 → usa o preço antigo.
- [ ] **Step 2:** `.venv/bin/pytest tests/test_preco_avaliacao.py -v` → FAIL.
- [ ] **Step 3:** Implementar.
- [ ] **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(preco): avaliador de preço por item contra a tabela da rede`.

### Task 4: Aprovações locais

**Files:**
- Modify: `app/persistence/schema_env.py`
- Create: `app/persistence/preco_aprovacao_repo.py`
- Test: `tests/test_preco_aprovacao_repo.py`

**Interfaces:**
- Produces:
  - `assinatura(item: OrderItem, fire_produto_id: str | None) -> str` — sha256 de `fire_produto_id|unit_price em centavos|quantity`.
  - `registrar_pendencias(conn, import_id: str, itens: list[tuple[ItemAvaliado, str]], *, agora) -> list[int]` — (avaliado, assinatura); devolve ids das linhas novas; mesma assinatura já pendente/decidida não duplica.
  - `aplicar_decisao(conn, *, import_id: str, item_index: int, assinatura: str, decisao: Literal["acordo", "regra", "preco_inicial", "recusado", "auto_tabela"], preco_final_centavos: int | None, por: str, em: datetime, motivo: str | None, flow_id: str) -> bool` — False se a assinatura não bate (item mudou): a decisão é descartada.
  - `decisoes_validas(conn, import_id: str, assinaturas: dict[int, str]) -> dict[int, dict]` — só as com assinatura igual à atual.
  - `pendentes_nao_enviadas(conn, limite: int) -> list[dict]`; `marcar_enviada(conn, ids: list[int], *, em) -> None`.

- [ ] **Step 1: Testes**
  - `test_decisao_com_assinatura_antiga_e_descartada`: pendência com preço 54,00; item editado para 55,00; `aplicar_decisao` com a assinatura antiga → False; `decisoes_validas` vazio.
  - `test_reavaliar_nao_duplica_pendencia`.
  - `test_recusado_fica_registrado_com_motivo`.
- [ ] **Step 2:** FAIL. **Step 3:** tabela `preco_aprovacao(id PK, import_id, item_index, assinatura, motivo_fila, preco_pedido_centavos, preco_tabela_centavos, situacao, decisao, preco_final_centavos, decidido_por, decidido_em, motivo_recusa, flow_id, criado_em, enviada_em, UNIQUE(import_id, item_index, assinatura))`. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(preco): aprovações de preço por item com assinatura`.

### Task 5: Portão único no envio (Fire e XLSX)

**Files:**
- Create: `app/erp/preco_trava.py`
- Modify: `app/web/server.py` (`_send_one_to_fire` ~2360-2400, `_export_one_xlsx` ~2590-2630)
- Test: `tests/test_preco_trava.py`, `tests/test_web_server.py`

**Interfaces:**
- Consumes: Tasks 1-4; `check_order`, `is_blocking` (`app/erp/product_check.py`).
- Produces:
  - `DecisaoEnvio(bloqueado: bool, reason: str | None, detalhe: str | None, precos_aplicar: dict[int, float], avaliacao: Avaliacao | None)`
  - `decidir_envio(import_id: str, order: Order, entry: dict, *, env: dict | None, agora: datetime) -> DecisaoEnvio`
  - `aplicar_precos(order: Order, precos: dict[int, float]) -> None` — muta `order.items[i].unit_price` (transiente, como o `depara_apply`).

Comportamento por modo: `desligado` → só a regra de hoje (`check_order` + `is_blocking` + ack), mesmo texto de erro; `observando` → regra de hoje + avaliação gravada em audit `preco_observado` (nunca bloqueia por ela); `ligado` → regra de hoje **não** é usada; bloqueia se há `nao_avaliado`, ou `fila` sem decisão válida, ou alguma decisão `recusado`; ao liberar, `precos_aplicar` traz o preço do aprovador nos itens `sem_preco_pedido`. Check do Fire indisponível no envio (`available` False) → usa `entry["check"]` do commit; sem ele → bloqueia `nao_avaliado/produto_nao_identificado`. CNPJ da rede: `check["client"]["cnpj"]` quando `entry["cliente_override_codigo"]` existe, senão `order.header.customer_cnpj`. Texto do bloqueio (ligado): "Pedido aguardando preço: N item(ns) na fila de aprovação do Flow (Rafael/Erica)." / "N item(ns) sem avaliação de preço: <motivo legível>." / "Preço recusado no Flow: <motivo>."

- [ ] **Step 1: Testes**
  - `test_desligado_e_bit_a_bit_hoje`: mesmos 3 cenários de bloqueio do `is_blocking` atual dão o mesmo `detalhe`.
  - `test_observando_nao_bloqueia_e_audita`.
  - `test_ligado_item_na_fila_bloqueia`; `test_ligado_aprovado_libera`; `test_ligado_recusado_bloqueia_com_motivo`.
  - `test_ligado_sem_preco_no_pedido_aplica_preco_do_aprovador`: decisão `preco_inicial` 48,00 → `precos_aplicar == {0: 48.0}`.
  - `test_fire_fora_usa_check_salvo`; `test_fire_fora_sem_check_salvo_bloqueia`.
  - `test_override_de_cliente_define_a_rede`.
  - Em `test_web_server.py`: `test_send_to_fire_bloqueado_pela_trava_ligada` (409, `reason="preco_aguardando"`) e `test_export_xlsx_leva_preco_aprovado` (planilha sai com 48,00).
- [ ] **Step 2:** FAIL. **Step 3:** Implementar e trocar os dois blocos duplicados de `server.py` por `decidir_envio` + `aplicar_precos` (chamado antes do `depara_apply`). **Step 4:** `.venv/bin/pytest tests/test_preco_trava.py tests/test_web_server.py tests/test_product_check.py -v` → PASS.
- [ ] **Step 5:** Commit `feat(preco): portão único de preço no envio ao Fire e no XLSX`.

### Task 6: Preview mostra a situação de preço

**Files:**
- Modify: `app/web/server.py` (payload do preview/detalhe do pedido, ~3574), `app/web/static/index.html`
- Test: `tests/test_web_server.py`

**Interfaces:**
- Consumes: `decidir_envio` (Task 5) em modo leitura.
- Produces: no payload do pedido, `preco: {modo, itens: [{item_index, situacao, motivo, preco_pedido, preco_tabela, decisao}], resumo}`; ausente em `desligado`.

- [ ] **Step 1:** `test_detalhe_traz_avaliacao_de_preco_em_observando`; `test_detalhe_sem_bloco_preco_em_desligado`.
- [ ] **Step 2:** FAIL. **Step 3:** Backend + na UI uma pílula por item (Entra / Na fila do Flow / Aprovado / Recusado / Sem avaliação) e um banner "Aguardando Rafael/Erica no Flow" quando houver fila. Usar os tokens de `tokens.css`; passar pela skill `hm-designer` antes do commit. **Step 4:** PASS + conferir no navegador via `portless`.
- [ ] **Step 5:** Commit `feat(preco): situação de preço por item no preview`.

### Task 7: Conversa com o Flow (contrato do pcp-app)

**Bloqueada até o contrato chegar.** A sessão do pcp-app entrega um doc versionado com as 3 rotas (tabela incremental, pedido aguardando preço, decisões + confirmar). Os modelos pydantic desta task copiam aquele contrato literalmente.

**Files:**
- Modify: `app/integrations/flowpcp/client.py`
- Create: `app/integrations/flowpcp/precos_schema.py` (modelos do contrato, no padrão de `catalogo_schema.py`)
- Create: `app/integrations/flowpcp/precos.py`
- Modify: `app/integrations/flowpcp/envio_portal.py` (`ciclo`)
- Test: `tests/test_flowpcp_precos.py`

**Interfaces:**
- Consumes: Tasks 2 e 4; `flowpcp_config_for_slug` (`app/integrations/flowpcp/config.py:52`).
- Produces: `precos.rodar(agora: datetime) -> None` — para cada ambiente com `preco_trava_modo != "desligado"`: sincroniza a cópia (config do Flow de `preco_fonte_slug or slug`); só em `ligado`, envia pendências e lê decisões (confirmando cada uma). **Fora das janelas de envio**: pendência de preço não é demanda; não espera 8/11/14/17h.

- [ ] **Step 1: Testes** (Flow fake no mesmo padrão de `tests/test_flowpcp_poll.py`): sync aplica lote e avança cursor; falha do Flow não avança cursor nem apaga a cópia; `observando` não envia pendência; decisão com assinatura velha é confirmada como `item_alterado` e descartada; decisão `regra` dispara um sync na mesma volta.
- [ ] **Step 2:** FAIL. **Step 3:** Implementar; `ciclo` chama `precos.rodar(agora)` num `try/except` próprio, como os outros. **Step 4:** PASS.
- [ ] **Step 5:** Commit `feat(preco): sync da tabela, pendências e decisões de preço com o Flow`.

### Task 8: Documentação

**Files:** `docs/ai/modules/erp.md` (seção nova "Trava de preço por rede"), `docs/ai/modules/flowpcp.md` (3 rotas novas), `docs/ai/modules/environments.md` (2 colunas), `docs/ai/00-index.md` (linha de roteamento + testes), `docs/BACKLOG.md` §4.2 marcado como coberto por esta feature.

- [ ] **Step 1:** Atualizar só as seções afetadas.
- [ ] **Step 2:** Suíte completa `.venv/bin/pytest tests/ -q` + `ruff check app/ tests/` → verde.
- [ ] **Step 3:** Commit `docs(preco): trava de preço por rede nos módulos`.

---

## Em aberto que afeta este plano

- **Nasmar não é tenant no Flow** (`enabled_flowpcp_envs`: "Só o ambiente MM liga"). A tabela da Nasmar precisa morar em algum lugar. O plano já prevê `preco_fonte_slug` (a Nasmar sincroniza pela config de outro ambiente), mas a resposta muda o contrato: ou a Nasmar ganha tenant próprio no Flow, ou a tabela do tenant MM ganha a dimensão "empresa que fatura". **Decisão do Samuel.**
- Modelo de tabela (uma por rede ou base + exceção) e base do preço (bruto/líquido): decidem com o PDF; nenhuma task do Portal muda, só o conteúdo que o sync traz.
