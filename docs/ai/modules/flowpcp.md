# Módulo: flowpcp (integração com o FlowPCP)

## Responsabilidade
Ponte entre o Portal e o **FlowPCP** (chão de fábrica). Quatro fluxos independentes,
todos **por ambiente** e todos com gate próprio:

1. **Pedido → Flow** — pedido aprovado sobe pro Flow (`/recebimento`).
2. **Decisões Flow → Fire** — o Flow decide o prazo pactuado; o Portal escreve a data de
   entrega de volta no Fire (Modelo B / OVERLAY).
3. **Catálogo Fire → Flow** — snapshot de produto, com cópia local sempre e envio opt-in.
4. **Clientes Fire → Flow** — mesma mecânica do catálogo, para master data de cliente.

**Regra que atravessa tudo: best-effort.** O pedido já entrou no Fire/XLS quando o push
acontece — uma falha aqui vira outbox/retry e **nunca** pode derrubar o fluxo principal.

## Arquivos críticos

| Arquivo | Papel |
|---|---|
| `config.py` | `FlowPCPConfig` + `flowpcp_config_for_slug()` / `enabled_flowpcp_envs()` |
| `hook.py` | `push_new_order(order, *, import_id, slug)` — o gatilho |
| `exporter.py` | `FlowPCPExporter.enqueue()` → outbox (`target=flowpcp`) |
| `envio_portal.py` | thread do processo web: entrega a fila (com triagem da fila acumulada) e, com interruptor, as decisões |
| `client.py` | HTTP para o Flow (service token, timeout por ambiente) |
| `mapper.py` / `schema.py` | `Order` → payload de `/recebimento` |
| `intercompany.py` | **política** do de-para de cliente (quando aplica) |
| `poll_decisoes.py` | consome decisões do Flow e reconcilia no Fire |
| `catalogo_*.py` / `clientes_*.py` | sync de produto e de cliente (extract → local → push) |

## Configuração — por ambiente, não global

Tudo vive em colunas `flowpcp_*` da tabela `environments` (`app_shared.db`); o token é
cifrado (`flowpcp_service_token_enc`, via `secret_store`).

| Coluna | Default | O que controla |
|---|---|---|
| `flowpcp_enabled` | `0` | liga a integração no ambiente |
| `flowpcp_base_url`, `flowpcp_tenant_id`, `flowpcp_service_token_enc` | — | destino e credencial |
| `flowpcp_dry_run` | `0` | simula sem gravar do lado do Flow |
| `flowpcp_poll_interval_s` | `30` | cadência do poll de decisões |
| `flowpcp_request_timeout_s` | `30.0` | timeout HTTP |
| **`flowpcp_catalogo_push`** | `0` | **OFF: o sync só atualiza a cópia local** |
| `flowpcp_catalogo_apenas_meias` | `0` | recorta o catálogo |
| **`flowpcp_clientes_push`** | `0` | idem para clientes |

Rotas admin: `PUT /api/admin/environments/{env_id}/flowpcp`,
`PUT /api/admin/environments/{env_id}/intercompany`,
`POST /api/admin/environments/{env_id}/flowpcp/sync-catalogo?apply=`,
`POST /api/admin/environments/{env_id}/flowpcp/sync-clientes?apply=` — todas exigem
`flowpcp_enabled`. Ver `modules/environments.md` para o CRUD do ambiente.

## 1. Pedido → Flow

`push_new_order` é chamado em `app/web/server.py` **depois** do sucesso no Fire/XLS.
Ele resolve o cliente (ver intercompany, abaixo), monta o payload e chama
`FlowPCPExporter.enqueue()`, que **enfileira no outbox** com
`idempotency_key = "send-{import_id}"`. Quem entrega e faz retry é uma thread do
processo web (`envio_portal.py`, `flowpcp-envio`) — no Windows do cliente só o `ui.py`
roda, e o worker (`drain_outbox`) nunca subiu lá: o envio ficou parado de 24/07 a 10/2026.
O worker continua fazendo o mesmo em deploy docker, onde a thread fica desligada
(`PORTAL_FLOWPCP_ENVIO=0` no `docker-compose.yml`). **Nunca há HTTP no request path** — a
UI não espera o Flow.

**Janelas de envio:** a fila só sai em janelas de 1 h às 8h, 11h, 14h e 17h (Brasília),
segunda a sábado (`app/utils/janelas_envio.py`, `PORTAL_FLOW_JANELAS`); fora delas o pedido
só enfileira. Resumo por janela no log (`janela … fechada: total=… por_ambiente=…`) e em
`/metrics` (`portal_flowpcp_janela_pedidos{janela,resultado}`, últimas 8; resultado =
enviado | recusado | descartado | retido). A contagem é lida do banco (`outbox.sent_at` e
audits `flowpcp_recusado/descartado/retido`, hora local do servidor), então sobrevive a
reinício e inclui o worker. Vale para thread e worker.

**Regra de envio** (`envio_portal.drenar_ambiente`, usada pela thread E pelo worker):
- pedido com `imports.fire_codigo`, qualquer idade: só vai se segue aberto no ERP
  (`CAB_VENDAS.STATUS`, READ ONLY, uma leitura por ciclo). Faturado, cancelado ou apagado
  → `dead` com `last_error = "descartado: …"` e audit `flowpcp_descartado`;
- sem código e com até 7 dias (`LIMITE_ATRASO`): vai (no modo xlsx o pedido ainda nem
  entrou no ERP);
- sem código e mais velha: `retido` + audit `flowpcp_retido`; volta sozinha à fila quando
  a reconciliação preenche o `fire_codigo` (`outbox_repo.release_retained`);
- ERP fora, ou ERP que não acha NENHUM de vários códigos (banco trocado): quem tem código
  espera, com pausa de 5 min e nada é descartado; recentes vão antes das acumuladas;
- falha de envio pausa o ambiente por 5 min (as outras linhas não queimam tentativa);
- linha flowpcp **nunca morre por falha passageira** (rede, 5xx, 401/403/408/429): passado o
  escalonamento (30 s…6 h), tenta a cada 6 h. Recusa definitiva (outro 4xx, payload
  inválido) → `dead` com `last_error = "recusado: …"`. Linhas `retido` contam como pendentes
  em `/metrics`; descartadas e recusadas contam como `dead` (o `last_error` diz qual). `next_attempt_at` é hora local, no formato de `outbox_repo._now()`
  (antes era UTC com offset: no Brasil, 30 s viravam 3 h 30 s).

Duplicata no outbox (`OutboxDuplicateError`) é no-op com log, não erro.

## 2. De-para de cliente intercompany

Divisão de responsabilidade que não deve ser embaralhada:

- **`intercompany.py::resolucao_para(order, *, slug)`** — *política*. Casa o CNPJ do
  pedido contra `environments.intercompany_cnpj`; se bater, chama o resolver.
- **`app/erp/depara_cliente.py`** — *leitura*. Só lê o Firebird da revenda e devolve o
  cliente. Contrato, motivos e cool-down em `modules/erp.md`.

O `hook.py` ainda embrulha a chamada num guard próprio (`_resolucao_segura`) — cinto e
suspensório, porque `push_new_order` é o contrato de que `server.py` depende.

A resolução é auditada em `depara_cliente` com chave, motivo, CNPJ/nome reais e o
`revenda_slug`. A lista `pedidos_no_4` é capada em **50** itens no audit
(`pedidos_no_4_total` guarda a contagem real): uma chave genérica digitada à mão na
revenda (`PULMÃO`, `AF`, `AW`, `GFNASMAR`) casa centenas de linhas. **O cap é só no
audit** — pôr `LIMIT` na query esconderia um segundo CNPJ e quebraria a detecção de
ambiguidade.

## 3. Decisões Flow → Fire (`poll_decisoes`)

`poll_decisoes_once` lê o cursor persistido (`flowpcp_cursor_state`), busca decisões,
aplica o `UPDATE` da data de entrega no Fire e confirma de volta ao Flow. Job
`poll_flowpcp`, a cada 30s, **por ambiente habilitado** — um ambiente ruim não
derruba os outros. Roda **só com `PORTAL_FLOWPCP_DECISOES=1`** (worker e processo web)
(desligado por padrão: é escrita no ERP de produção, parada desde julho). O "Dry-run" da
tela **não** é interruptor: ele confirma a decisão no Flow sem gravar no ERP.

- Não achou o pedido pela chave do cliente real? Tenta de novo com
  `PEDIDO_CLIENTE + CNPJ da revenda` — no Fire o pedido continua no nome de quem faturou.
- Ainda não achou: `register_attempt`; em **5** tentativas (`_MAX_NAO_ENCONTRADO`) loga
  `critical`.
- **Bug aberto:** exceção no retry da revenda retorna sem contar tentativa e **segura o
  cursor**. Ver `docs/BACKLOG.md` §1.2.

## 4. Catálogo e clientes (Fire → local → Flow)

`run_catalogo_sync` / `run_clientes_sync` extraem do Firebird, **sempre** gravam o
snapshot local (`catalogo_fire` / `clientes_fire`, no db do ambiente) e só empurram ao
Flow se o gate correspondente estiver ligado. Gate OFF é caminho normal, não erro — o
log diz `envio ao Flow DESLIGADO`.

**Catálogo diário** (`catalogo_diario.py`, desde 15/10/2026): a thread
`flowpcp-envio` (`envio_portal.ciclo`) manda o catálogo inteiro gravando
(`dryRun=false`, `fullSync=true`) uma vez por dia, na hora
`PORTAL_FLOWPCP_CATALOGO_HORA` (padrão 5, Brasília), em cada ambiente com
`flowpcp_catalogo_push` ligado. "Já foi hoje" = `MAX(catalogo_fire.extraido_em)`
desde o início da hora. Falha antes da cópia local pausa 10 min. O promote do
Flow não desativa produto só do Flow (sai como `flowOnly` no relatório).

Tabelas locais em `modules/persistence.md`. Ferramentas de linha de comando:
`tools/sync_catalogo_fire.py`, `tools/configurar_flowpcp.py`.

## Testes

Cobertura larga — 25+ arquivos. Os principais:

```bash
.venv/bin/pytest tests/test_flowpcp_hook.py tests/test_flowpcp_exporter.py \
  tests/test_flowpcp_poll.py tests/test_flowpcp_intercompany.py \
  tests/test_catalogo_sync.py tests/test_clientes_sync.py -v
```

Config e wiring: `test_flowpcp_config.py`, `test_flowpcp_worker_wiring.py`,
`test_intercompany_config.py`. Mappers: `test_flowpcp_mapper_*.py`.

## Armadilhas

- **Gate OFF não é falha.** `catalogo_push`/`clientes_push` desligados só suprimem o
  envio; a cópia local continua atualizando.
- **O Flow casa cliente só por CNPJ.** Mandar razão social não ajuda; o CNPJ tem que ser
  o de faturamento correto.
- **`/recebimento` é insert-only e deduplica por `externalId`.** Re-enviar não corrige um
  pedido que subiu errado — corrigir histórico exige patch do lado do Flow.
- **Cliente criado automático no Flow nasce sem marca** (`grupoCodigo` vazio). Ver
  `docs/BACKLOG.md` §3.5.
