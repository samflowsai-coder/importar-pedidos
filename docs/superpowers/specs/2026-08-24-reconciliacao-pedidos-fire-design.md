# Reconciliação de pedidos com o Fire — design

**Data:** 2026-08-24 · **Revisão 2** (pós-review, com medição em dado real)
**Status:** aprovado para implementação
**Domínios:** `erp`, `worker`, `state`, `persistence`, `web`

---

## O problema

A lista do cliente tem **308 pedidos, todos em "Em revisão"**, e só cresce. Não há como
distinguir "ainda preciso fazer" de "já fiz em julho".

Não é bug, é buraco no modelo. `PortalStatus.PARSED` significa, no próprio código, *"in
human review, not yet in Fire"*, e o pedido só sai desse estado quando **o portal**
insere (`SENT_TO_FIRE`). Com `EXPORT_MODE=xlsx`, a operadora exporta o XLS e cadastra à
mão no Fire — o portal nunca insere, nada nunca sai de `parsed`.

### Três fatos medidos que moldam o desenho

**1. Riachuelo não tem CNPJ no header.** `MercadoEletronicoParser` monta o `OrderHeader`
sem `customer_cnpj`, de propósito (`mercado_eletronico_parser.py:56`: *"Customer CNPJ is
not in the header — each store has its own CNPJ in items"*). Como `imports.customer_cnpj`
deriva do header, **os 308 pedidos que motivam a feature não têm CNPJ de cliente**. Uma
chave que exija CNPJ de header entrega zero no caso motivador.

**2. Um import Riachuelo vira N linhas no Fire.** No backup real da MM, cada número de
pedido aparece em ~3 registros de `CAB_VENDAS` — um por loja, cada um com cliente e CNPJ
próprios. O modelo é 1↔N, não 1↔1.

**3. O worker não roda no cliente.** `scripts/setup-service.ps1` registra apenas `ui.py`
como tarefa agendada. `python -m app.worker` só existe no `docker-compose.yml`. **Nenhum
job do APScheduler dispara na MM.** Qualquer coisa periódica precisa viver no processo
web.

### Armadilha acoplada (§1.3 do BACKLOG)

`poll_fire.py:67` chama `conn.execute(...)`, que não existe em `fdb.Connection`, e indexa
`row["STATUS"]` numa tupla. Este trabalho preenche `fire_codigo` e liga o caminho —
corrigir é pré-requisito.

---

## Objetivo

Descobrir quais pedidos em `parsed` já existem no Fire, marcar, e tirá-los da visão
padrão — para a tela mostrar trabalho pendente em vez de arquivo morto.

### Não-objetivos

Inserir no Fire. Mexer em `EXPORT_MODE`. Reconciliar por valor ou item. Apagar ou
arquivar pedido. Disparar FlowPCP a partir da reconciliação.

---

## Chave de match

**Regra geral: sempre duas pernas — número do pedido E identidade do cliente.** O que
muda é de onde vem a segunda perna.

### Três caminhos, em ordem de força

| # | Quando | Segunda perna | Marca quando |
|---|---|---|---|
| 1 | `cliente_override_codigo` presente | `CAB_VENDAS.CLIENTE = <codigo>` | achou ≥1 linha |
| 2 | `customer_cnpj` no header | CNPJ do `CADASTRO` da linha | achou ≥1 linha com CNPJ igual |
| 3 | Sem CNPJ no header (Riachuelo, NBA) | conjunto de `delivery_cnpj` dos itens | **TODAS** as lojas têm linha |

O caminho 1 é o mais forte e sai de graça: o override já persiste o código do cliente
(`schema_env.py:36`).

O caminho 3 mantém a chave dupla — o CNPJ da loja **pertence comprovadamente ao pedido**,
veio do próprio arquivo. A regra **"todas as lojas"** é a trava conservadora: pedido com
3 lojas e só 2 no Fire continua `parsed`. Meio-cadastrado é trabalho pendente, e silêncio
é melhor que marca errada.

### Normalização do número

Comparação por variantes, sempre com a perna do cliente junto:

- exato;
- sem sufixo `-NNNN` — **Sam's guarda `06654993-0000` no portal e `06654993` no Fire**;
  sem isso, 100% de falso negativo em Sam's;
- sem zeros à esquerda.

### Guarda temporal contra reuso de número

Kallan usa `K01`, Magic Feet `MF048`, Authentic `AF198` — códigos curtos e sequenciais,
**com** CNPJ no header. Um `K02` novo casaria com o `K02` do ano passado: mesmo número,
mesmo cliente. A chave dupla **não** fecha isso.

A query devolve `DATA_PEDIDO` e só aceita linha com data **≥ (data do pedido − 90 dias)**.

### Query

Generalização de `FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE` (`queries.py:307`) recebendo lista
de números, devolvendo `DATA_PEDIDO`, com **as colunas duplicadas aliasadas** (`V.CODIGO`
e `C.CODIGO` colidem na original). Lotes de 200: 308 pedidos viram 2 idas ao banco.

---

## Modelo de dados

**Estado novo:** `PortalStatus.FOUND_IN_FIRE = "found_in_fire"` — *"existe no Fire; o
portal não inseriu"*. Distinto de `SENT_TO_FIRE` para preservar **quem** cadastrou.

**Evento:** `LifecycleEvent.FOUND_IN_FIRE`, origem `EventSource.FIRE`. Payload:
`fire_codigo`, `fire_status`, `pedido_cliente`, `caminho_match` (1|2|3), `lojas_casadas`.

**Transições a wirar em `app/state/machine.py`** — nos dois eixos:

- `(PARSED, FOUND_IN_FIRE) → FOUND_IN_FIRE`
- `(FOUND_IN_FIRE, FIRE_STATUS_CHANGED) → FOUND_IN_FIRE`
- `(FOUND_IN_FIRE, POST_TO_GESTOR_REQUESTED) → FOUND_IN_FIRE`

As duas últimas não são teoria: sem elas, `_enqueue_gestor` (`poll_fire.py:121-148`)
enfileira no outbox e a transição estoura depois, deixando **outbox órfão** — e o `except
Exception` genérico engole o erro.

**Colunas:** nenhuma nova. Reusa `portal_status`, `fire_codigo`, `fire_status_last_seen`,
`fire_status_polled_at`.

**Limitação 1↔N documentada:** com N lojas, `fire_codigo` guarda a linha de menor
`CODIGO`. É representação parcial, inofensiva enquanto o poll não roda na MM. Se um dia
importar, vira coluna de ligação própria.

---

## Componentes

**`app/erp/fire_reconcile.py`** — leitura pura. Recebe candidatos, devolve o que achou.
Não conhece `imports`, não decide estado, não grava. Nunca levanta. Cool-down por
ambiente após erro de conexão, armado **só em volta do `connect_with_config`** (o §1.4 do
BACKLOG feito certo desde o começo, em vez de repetido).

**`app/persistence/repo.py`** — `list_parsed_for_reconcile()` (elegíveis: com override,
ou com CNPJ de header, ou com `delivery_cnpj` nos itens) e `mark_found_in_fire()`.

**`app/reconcile/runner.py`** — orquestra para um ambiente: lista → busca → aplica.
Chamado pelos três gatilhos, sem duplicar lógica.

**`app/web/`** — rota `POST /api/imported/reconciliar-fire`, botão, chip, filtro.

---

## Gatilhos

| Gatilho | Onde vive | Trava |
|---|---|---|
| Periódico 3x/dia (07h, 12h, 18h) | **processo web** (o worker não sobe na MM) | sim |
| Botão "Verificar no Fire" | rota | **ignora** — é pedido explícito |
| Entrada do operador | `POST /api/env/select`, background | sim |

Registrar também no scheduler do worker, para deploys docker onde ele existe. Os dois
caminhos chamam o mesmo runner.

**Por que `env/select` e não `auth/login`:** no login ainda não há ambiente ativo — o
operador escolhe a empresa depois. Não haveria contra qual Firebird consultar. O
disparo em background precisa **ativar `active_env` explicitamente** com o ambiente
recém-selecionado; o contexto do request ainda aponta pro anterior.

**Trava:** 10 min por ambiente, dict em memória no processo web (uvicorn é
single-process). Não coordena com o worker — quem cobre a corrida é o CAS abaixo.

---

## Idempotência — de verdade

`transition()` lê o estado **fora** da transação de escrita (conexão `DEFERRED`;
`router.py:123`). Web e worker são processos distintos: dois gatilhos podem ler `parsed`
e ambos gravar o evento. Duplicata no log canônico.

`mark_found_in_fire` faz **compare-and-set**, não usa `transition()` cru:

1. `UPDATE imports SET portal_status='found_in_fire', ... WHERE id=? AND
   portal_status='parsed'` — adquire o write-lock;
2. `rowcount == 1`? Se não, outro gatilho ganhou: sai sem evento;
3. só então grava o evento de ciclo de vida.

---

## Wiring do poll (o que a revisão 1 errou)

A revisão 1 afirmava que preencher `fire_codigo` faria o `poll_fire` adotar o pedido.
**Falso:** `list_pending_for_fire_poll` (`repo.py:428`) filtra `portal_status =
'sent_to_fire'`. Correções:

- incluir `'found_in_fire'` no filtro;
- reavaliar a janela `imported_at >= now-7d` (`repo.py:431`) — pedido reconciliado tarde
  nunca seria polled. Passa a considerar a data da reconciliação.

---

## UI

**Colisão de nome:** já existe chip "No Fire" = `sent_to_fire` (`index.html:718`).
Resolução: "No Fire" passa a significar `IN ('sent_to_fire','found_in_fire')`, com badge
distinguindo a origem; o padrão da tela vira **"Em revisão"**, que é o trabalho pendente.

Isso exige `_build_where`/`list_imports`/`count_imports` (`repo.py:154-222`) aceitarem
**multi-status** — hoje só fazem igualdade. Contrato que muda.

`portalStatusLabel/Color/Bg` (`index.html:1234-1250`) ganham `found_in_fire`.

**Rotas que passam a ver o estado novo:**

- `cancel` (`server.py:2209`) só bloqueia `sent_to_fire`; cancelar um `found_in_fire`
  cairia no 409, mas o `append_audit("cancelled")` grava **antes** da transição —
  auditoria mentirosa. Ajustar guard e ordem.
- `export-xlsx` (`server.py:1836`) passa a recusar pedido reconciliado: já está no ERP,
  reexportar convida a duplicata.

**Botão manual devolve `{verificados, casaram, erro_conexao}`.** "Nunca levanta" é certo
para o job e errado para o pedido explícito: sem distinguir, a Grazi vê "0 casaram" com o
Firebird fora e conclui que quebrou.

---

## Erro e degradação

Reconciliação é observação: toda falha degrada para "não sei", nunca para dado errado.
Firebird fora → nada muda, log, cool-down. Pedido inelegível → fora dos candidatos, não é
erro. Match parcial no caminho 3 → continua `parsed`.

`found_in_fire` **não** dispara `push_new_order` do FlowPCP — status quo preservado.

---

## Testes

Firebird falso; nada toca banco real.

**`test_fire_reconcile.py`** — os 3 caminhos de chave; "todas as lojas" (parcial não
marca); variante sem sufixo (caso Sam's `06654993-0000`); zeros à esquerda; guarda
temporal barra número reusado fora da janela; CNPJ divergente não casa; Fire fora devolve
vazio sem levantar; cool-down só em erro de conexão; lote > 200.

**`test_reconcile_runner.py`** — ambiente ruim não derruba os outros; CAS: dois gatilhos
concorrentes geram **um** evento; só toca `parsed`.

**`test_web_reconciliar_fire.py`** — auth; payload distingue 0-casaram de erro de conexão;
trava barra entrada e não barra botão; filtro multi-status; cancel e export-xlsx sobre
`found_in_fire`.

**`test_worker_poll_fire.py`** — o fake atual dá `ctx.execute` de MagicMock e **jamais
pegaria o §1.3**. Fake novo com `cursor()` e linhas-tupla (sem acesso por nome), que falha
antes da correção.

---

## Riscos

**Falso positivo tira pedido da fila sem estar no Fire.** Travas: chave dupla nos 3
caminhos, "todas as lojas", guarda temporal de 90 dias. Nada sai do banco; o chip mostra
tudo; reverter é uma linha de SQL.

**Falso negativo em massa faz a feature parecer quebrada.** Era o risco fatal da revisão
1 (zero em Riachuelo). Fechado pelo caminho 3 e pela normalização do número.

**O bug do `poll_fire` acorda.** Certo. Corrigido no mesmo PR, com teste que falha antes.

**A lista esvaziar assusta.** Contador no chip e resultado explícito no botão ("N pedidos
já estavam no Fire").

---

## Nota sobre evidência

A validação "939 pedidos, 0 ambiguidade" citada na revisão 1 é da Fire **da revenda**
(`queries.py:302-306`), não da Fire da MM que esta feature consulta. Vale como indício de
que `PEDIDO_CLIENTE` é chave utilizável, não como prova para este banco. É por isso que a
chave é dupla nos três caminhos, e não confia no número sozinho.
