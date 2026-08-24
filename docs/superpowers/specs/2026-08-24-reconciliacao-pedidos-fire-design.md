# Reconciliação de pedidos com o Fire — design

**Data:** 2026-08-24
**Status:** aguardando revisão do Samuel
**Domínios:** `erp`, `worker`, `state`, `persistence`, `web`

---

## O problema

A lista de pedidos do cliente tem **308 registros e todos em "Em revisão"**. Ela só
cresce: 308 hoje, 400 no mês que vem, e nenhuma forma de distinguir "ainda preciso
fazer" de "já fiz em julho".

A causa não é bug, é um buraco no modelo. `PortalStatus.PARSED` significa, no próprio
código, *"in human review, not yet in Fire"*. O pedido só sai desse estado quando **o
portal** insere no Fire (`SENT_TO_FIRE`, quando cria a linha em `CAB_VENDAS`).

O cliente roda `EXPORT_MODE=xlsx`: a operadora exporta o XLS e cadastra à mão no Fire.
O portal nunca insere, logo nada nunca sai de `parsed`. **O portal não tem como saber
que o trabalho já foi feito.**

### O mecanismo já existe, gated na condição errada

`app/worker/jobs/poll_fire.py` roda a cada 60s e consulta o Fire — mas
`repo.list_pending_for_fire_poll` exige `fire_codigo IS NOT NULL`, e `fire_codigo` só é
preenchido quando o portal insere. **O worker só olha para os pedidos que ele mesmo
criou** — exatamente os que não precisam de reconciliação. Para o cliente XLS-only, ele
nunca teve o que fazer.

### Armadilha acoplada (§1.3 do BACKLOG)

`poll_fire.py:67` chama `conn.execute(...)`, que não existe em `fdb.Connection` (só em
`Cursor`), e indexa `row["STATUS"]` numa tupla. Hoje dorme porque o job nunca alcança
essa linha. **Este trabalho passa a preencher `fire_codigo`, o que acorda o bug.**
Corrigir não é escopo extra: é pré-requisito.

---

## Objetivo

Descobrir quais pedidos em `parsed` já existem no Fire, marcar essa condição e tirá-los
da visão padrão da lista — para que a tela mostre trabalho pendente, não arquivo morto.

### Não-objetivos

- **Não** inserir nada no Fire. Reconciliação é leitura; nunca cria, altera ou cancela.
- **Não** mexer em `EXPORT_MODE`. A decisão de inserir direto no Fire segue parada.
- **Não** reconciliar por valor ou por item. Ver "Chave de match".
- **Não** apagar nem arquivar pedido. Nada some do banco; só muda de estado e de filtro.

---

## Chave de match

**Um pedido é considerado cadastrado no Fire quando existe linha em `CAB_VENDAS` com o
mesmo número de pedido E o CNPJ do cliente conferindo.**

Os dois, sempre. Número sozinho pode colidir entre clientes distintos; o CNPJ fecha essa
porta. `PEDIDO_CLIENTE` já foi validado na Fire viva durante o de-para intercompany:
zero ambiguidade em 939 pedidos.

### Por que não `CHECK_ORDER_EXISTS`

A query existente exige o código inteiro do `CLIENTE` (`CADASTRO.CODIGO`), que o portal
**não persiste** para pedidos XLS-only. Exigi-lo obrigaria a resolver o cliente antes,
adicionando uma ida ao banco e um modo de falha novo.

`FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE` (`queries.py:307`), criada no de-para, já busca por
`PEDIDO_CLIENTE` com `JOIN CADASTRO` e devolve `CPF_CNPJ` — exatamente o que falta. A
query nova é essa, generalizada para receber uma lista.

### Regra de decisão

| Situação no Fire | Decisão |
|---|---|
| Nenhuma linha com o número | Não casou. Segue `parsed`. |
| Uma ou mais linhas, CNPJ igual ao do pedido | **Casou.** Usa a de menor `CODIGO`. |
| Linhas existem, nenhuma com CNPJ igual | Não casou. Segue `parsed`, sem evento. |
| Linhas com CNPJs diferentes, uma delas igual | **Casou** na que bate. As outras são de outro cliente. |
| Pedido sem CNPJ no header | **Não tenta.** Sem a segunda chave não há match seguro. |

Comparação de CNPJ por **dígitos apenas**, via `app/erp/cnpj.py` (já existe).

---

## Modelo de dados

### Estado novo

`PortalStatus.FOUND_IN_FIRE = "found_in_fire"` — *"existe no Fire; o portal não foi quem
inseriu"*.

Distinto de `SENT_TO_FIRE` de propósito: preservar **quem** cadastrou importa para
auditoria e para o dia em que o insert direto for ligado. Reusar `sent_to_fire` faria o
histórico mentir sobre a origem.

Transição permitida: `parsed → found_in_fire`. Apenas essa. Um pedido `sent_to_fire`,
`cancelled` ou `error` nunca é tocado pela reconciliação.

### Evento de ciclo de vida

`LifecycleEvent.FOUND_IN_FIRE`, com `EventSource.FIRE` — que já existe no enum e é
descrito como *"observed in the Firebird ERP (poll worker)"*. Payload: `fire_codigo`,
`fire_status`, `pedido_cliente`, `cnpj_conferido`.

### Colunas

Nenhuma coluna nova. Reusa o que existe em `imports`:

- `portal_status` → `found_in_fire`
- `fire_codigo` → `CAB_VENDAS.CODIGO` encontrado
- `fire_status_last_seen` → `CAB_VENDAS.STATUS` no momento
- `fire_status_polled_at` → timestamp da reconciliação

Efeito colateral desejado: com `fire_codigo` preenchido, o `poll_fire` que já existe
passa a acompanhar esse pedido normalmente — o pedido cadastrado à mão entra no
monitoramento de status sem código adicional.

---

## Componentes

Quatro unidades, cada uma com uma responsabilidade e testável sozinha.

### 1. `app/erp/fire_reconcile.py` — leitura pura

```
buscar_no_fire(numeros: list[str], *, env_slug: str) -> dict[str, HitFire]
```

Só lê o Firebird e devolve o que achou. Não conhece `imports`, não decide estado, não
grava nada. Espelha a forma de `app/erp/depara_cliente.py`:

- **Nunca levanta.** Falha vira dicionário vazio + log.
- **Cool-down** por ambiente após erro de conexão, armado **só em volta do
  `connect_with_config`** — não no bloco inteiro. Este é o §1.4 do BACKLOG feito certo
  desde o começo, em vez de repetido.
- **Consulta em lote:** `WHERE TRIM(PEDIDO_CLIENTE) IN (...)`, blocos de 200. 308 pedidos
  viram 2 idas ao banco, não 308.

### 2. `app/persistence/repo.py` — candidatos e aplicação

- `list_parsed_for_reconcile(limit)` — pedidos em `parsed` **com CNPJ no header**, mais
  antigos primeiro.
- `mark_found_in_fire(import_id, *, fire_codigo, fire_status, at)` — grava as quatro
  colunas e o evento, em uma transação.

### 3. `app/worker/jobs/reconcile_fire.py` — o job

Para cada ambiente ativo: lista candidatos → busca em lote → aplica os que casaram.
Um ambiente com Firebird fora **não derruba os outros** (mesmo padrão do `poll_flowpcp`).

Registrado no scheduler existente com `coalesce=True` + `max_instances=1`, como os
demais.

### 4. `app/web/` — gatilho manual e lista

- `POST /api/imported/reconciliar-fire` — dispara para o ambiente ativo, devolve quantos
  casaram. Exige usuário autenticado.
- Botão **"Verificar no Fire"** ao lado de "Atualizar".
- Chip de filtro **"No Fire"**; a visão padrão passa a excluir `found_in_fire`.

---

## Gatilhos

| Gatilho | Quando | Comportamento |
|---|---|---|
| **Agendado** | 3x/dia (07h, 12h, 18h, hora do servidor) | Todos os ambientes ativos |
| **Manual** | Botão na tela | Só o ambiente ativo, resposta com o total |
| **Entrada do operador** | `POST /api/env/select` | Só aquele ambiente, **em background** |

### Por que `env/select` e não `auth/login`

No `POST /api/auth/login` **ainda não existe ambiente ativo** — o operador escolhe a
empresa depois, em `/selecionar-ambiente`. Reconciliar no login não teria contra qual
Firebird consultar. `env/select` é o momento em que o portal sabe a empresa e é
exatamente quando a pessoa vai olhar a lista. Do ponto de vista de quem usa, continua
sendo "ao entrar".

### Não bloqueia

O gatilho de entrada dispara em background e a resposta volta na hora. Consulta ao
Firebird na rede do cliente leva segundos; ninguém deve esperar isso para ver a tela.

**Trava de 10 minutos por ambiente:** se já reconciliou nesse intervalo, o gatilho de
entrada não repete. Dois operadores entrando em seguida não batem duas vezes no banco.
O botão manual **ignora a trava** — é pedido explícito.

---

## Erro e degradação

Reconciliação é observação. Toda falha degrada para "não sei", nunca para dado errado.

- **Firebird fora:** nada muda, log em `warning`, cool-down arma. Próxima janela tenta.
- **Pedido sem CNPJ:** não entra na lista de candidatos. Não é erro.
- **Match ambíguo sem CNPJ conferindo:** não casa. Silêncio é melhor que marcar errado.
- **`fire_codigo` órfão** (linha some do Fire depois): fora de escopo. O `poll_fire`
  existente já lida com pedido não encontrado.

O caminho é idempotente: rodar duas vezes no mesmo pedido não gera evento duplicado —
`mark_found_in_fire` só age sobre quem está em `parsed`.

---

## Testes

Firebird falso, como o resto do repo. Sem teste que toque banco real.

**`tests/test_fire_reconcile.py`** — casa exato; CNPJ divergente não casa; várias linhas
mesmo CNPJ casa na de menor `CODIGO`; CNPJs diferentes casa só na certa; sem match;
Firebird fora devolve vazio sem levantar; cool-down arma só em erro de conexão; lote
acima de 200 quebra em blocos.

**`tests/test_worker_reconcile_fire.py`** — ambiente ruim não derruba os outros;
idempotência; só toca `parsed`.

**`tests/test_web_reconciliar_fire.py`** — rota exige auth; devolve total; trava de 10min
barra o gatilho de entrada e não barra o botão; filtro `found_in_fire` sai do padrão.

**Correção acoplada:** `tests/test_worker_poll_fire.py` ganha o caso que hoje não existe
— `fire_codigo` preenchido, exercitando a linha 67 que quebraria.

---

## Riscos

**O bug do `poll_fire` acorda.** Alto e certo. Mitigação: corrigir no mesmo PR, com teste
que falha antes.

**Falso positivo marca pedido como feito sem estar.** Baixo com a chave dupla, alto em
consequência — some da lista de trabalho. Mitigação: exigir CNPJ; nada some do banco; o
chip "No Fire" mostra tudo; reverter é uma linha de SQL.

**Carga no Firebird do cliente.** Baixo: 2 consultas em lote, 3x/dia. Menor que o
`poll_fire` atual, que roda a cada 60s.

**A lista "esvaziar" assusta.** Real. O contador do chip "No Fire" e uma linha de
resultado ("N pedidos já estavam no Fire") tornam a mudança explicável em vez de
misteriosa.

---

## Fora de escopo

Reconciliar por item ou valor. Insert direto no Fire. Desfazer reconciliação pela UI
(reverter é SQL, e o caso deve ser raro). Notificar a operação quando um lote grande for
reconciliado.
