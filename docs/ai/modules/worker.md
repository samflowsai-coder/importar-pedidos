# Módulo: worker (APScheduler + jobs)

## Responsabilidade
Processo separado (`python -m app.worker`) que executa jobs periódicos:
drena o outbox (Gestor + FlowPCP), poll o Firebird por mudanças de status,
poll de decisões do FlowPCP (reconciliação de data de entrega), ingesta
arquivos novos por ambiente e faz retenção/backup do banco. Não compartilha
estado em memória com o FastAPI — comunicação exclusivamente via SQLite.

## Entry point
```bash
python -m app.worker          # direto
docker compose up worker      # via Docker (mesmo Dockerfile, cmd diferente)
```

## Arquivos críticos

### Scheduler
- `app/worker/scheduler.py` — bootstrap APScheduler com `SQLAlchemyJobStore`
  no SQLite do app. Jobs registrados:

| Job | Trigger | Função |
|---|---|---|
| `drain_outbox` | interval 15s | `run_drain_outbox()` |
| `poll_fire` | interval 60s | `run_poll_fire()` |
| `scan_environments` | interval 30s | `run_scan_environments()` |
| `poll_flowpcp` | interval 30s | `run_poll_flowpcp()` |
| `retention` | cron hour=3 | `run_retention()` |
| `reconcile_fire` | cron 07h/12h/18h local | `reconciliar_todos_os_ambientes()` |

Todos com `coalesce=True`, `max_instances=1`, `misfire_grace_time=30s`.

### drain_outbox
- `app/worker/jobs/drain_outbox.py`
- Pega até 20 linhas `pending` do outbox e posta para o Gestor de Produção
  (`target=gestor`).
- Sucesso: `mark_sent` + `set_gestor_order_id` + `POST_TO_GESTOR_SENT`.
- Falha: backoff exponencial (30s → 2m → 10m → 1h → 6h → `dead` após 5 tentativas).
- **FlowPCP (`target=flowpcp`):** no mesmo loop por-ambiente, se `flowpcp.enabled`
  para o slug, drena rows `flowpcp` reenviando `send_order` (retry do push que
  falhou inline em `FlowPCPExporter`). `_process_flowpcp_row` reconstrói
  `RecebimentoRequest.model_validate(row.payload)`; backoff/dead próprio via
  `_handle_flowpcp_failure` (`mark_failed(..., error=..., next_attempt_at=...)` —
  `error` é **keyword-only**) — sem emitir eventos de lifecycle do Gestor.
- Ao final de cada execução: chama `update_outbox_metrics()` (atualiza Gauges Prometheus).

### poll_flowpcp
- `app/worker/jobs/poll_flowpcp.py` (ponte Importador↔FlowPCP, Modelo B / OVERLAY).
- A cada 30s, para CADA ambiente com FlowPCP habilitado, chama
  `poll_decisoes_once` (`app/integrations/flowpcp/poll_decisoes.py`): busca
  decisões pendentes e reconcilia a data de entrega no Fire
  (`UPDATE CAB_VENDAS.DT_ENTREGA` via `app/erp/fire_update.py`).
- 4 ramos: rejeitado → `cancelamento_pendente_manual`; sem mudança →
  `sem_acao_necessaria`; data nova → `update_dt_entrega` + `data_atualizada`
  (ou só log se `dry_run`); pedido não localizado no Fire → conta tentativas e
  confirma `pedido_nao_encontrado_no_fire` após 5×.
- Cursor + contador de tentativas persistidos em `flowpcp_repo`
  (`app/persistence/flowpcp_repo.py`, tabelas no `schema_env`).
- **Avanço do cursor:** `processar_decisao` retorna `bool` (True = confirmada,
  sai do feed; False = re-tentar). `poll_decisoes_once` só salva `proximo_cursor`
  se TODAS as decisões do lote confirmaram. O cursor do Flow é watermark
  `atualizado_em >=` com dedup `reconciliado_em IS NULL`; avançar com uma decisão
  não-confirmada no meio do lote a deixaria atrás do watermark para sempre (perda
  silenciosa). Manter o cursor é seguro: re-buscar é idempotente e o dedup do Flow
  filtra as já confirmadas.
- Auth = `X-Service-Token` + `X-Tenant-Id` (NÃO Bearer).
- Config per-ambiente persistida em `environments` (colunas `flowpcp_*`; token
  cifrado via `secret_store`), lida por `enabled_flowpcp_envs()` /
  `flowpcp_config_for_slug()` (`app/integrations/flowpcp/config.py`). Só ambientes
  ativos com `flowpcp_enabled=1` entram — só o MM liga. Um ambiente ruim não
  derruba os outros.

### poll_fire
- `app/worker/jobs/poll_fire.py`
- Consulta `CAB_VENDAS.STATUS` dos pedidos `sent_to_fire` OU `found_in_fire`
  (reconciliado à mão pode mudar de status no Fire tanto quanto o que o
  próprio portal inseriu) com `production_status=none`, `fire_codigo`
  presente, dentro da janela de 7 dias — medida a partir do último poll
  (`fire_status_polled_at`), caindo para `imported_at` em quem nunca foi
  polled (`repo.list_pending_for_fire_poll`, `app/persistence/repo.py:428`).
- Stampa `fire_status_last_seen` + `fire_status_polled_at`.
- Se status mudou: emite `FIRE_STATUS_CHANGED`.
- Se status == `FIRE_TRIGGER_STATUS` (env var): enfileira no outbox + emite
  `POST_TO_GESTOR_REQUESTED`. Env vazia = automação desligada (padrão seguro).
- No-op silencioso se Firebird não configurado.
- Duração medida em `portal_poll_fire_duration_seconds` (Histogram Prometheus).

### reconcile_fire
- `app/reconcile/runner.py` (não um job próprio em `app/worker/jobs/` — o
  módulo é compartilhado com o processo web, ver seção "Gatilho periódico"
  abaixo).
- Acha, entre os pedidos `parsed` do portal, os que já foram cadastrados à
  mão no Fire (o cliente roda `EXPORT_MODE=xlsx`; a operadora exporta e
  cadastra sem o portal saber). Leitura do Fire em `app/erp/fire_reconcile.py`
  (ver `erp.md`, seção "Reconciliação com o Fire"); estado novo
  `found_in_fire` em `app/state/machine.py` (ver `state.md`).
- Registrado aqui com `CronTrigger(hour=_RECONCILE_HOURS)` (mesma grade
  07h/12h/18h de `HORARIOS_LOCAIS`), para deploys docker onde o worker
  existe.

### retention
- `app/worker/jobs/retention.py`
- Roda diariamente às 03:00 (configurável via `_RETENTION_HOUR`).
- Purges executados dentro de uma única transação SQLite:
  - `order_lifecycle_events` onde `occurred_at < now - RETENTION_DAYS` (default 180)
  - `audit_log` onde `created_at < now - RETENTION_DAYS`
  - `inbound_idempotency` onde `received_at < now - 90 dias` (TTL fixo)
  - `sessions` expiradas (`expires_at < now`)
  - `rate_limit_buckets` inativos há >1 dia
- VACUUM INTO: se `BACKUP_DIR` configurado, cria `app_state_YYYYMMDD.db` na pasta
  e mantém os 7 mais recentes. Operação atômica e segura com DB em uso.

## Env vars relevantes

```env
FIRE_TRIGGER_STATUS=       # status que dispara POST ao Gestor (vazio = desligado)
RETENTION_DAYS=180         # dias de retenção de lifecycle_events e audit_log
BACKUP_DIR=                # diretório para backup VACUUM INTO (vazio = desligado)
RATE_LIMIT_ENABLED=true    # false bypassa rate-limit (worker não usa, mas compartilha DB)
```

> FlowPCP não usa env var: config é per-ambiente na tabela `environments`
> (colunas `flowpcp_*`, token cifrado), editável em `/admin/ambientes`.

## Testes
- `tests/test_worker_drain_outbox.py`
- `tests/test_worker_poll_fire.py`
- `tests/test_retention.py` — purge por tabela, VACUUM INTO, manutenção de 7 backups.
- `tests/test_flowpcp_worker_wiring.py` — `run_poll_flowpcp` + `_list_flowpcp_envs`
  (gating ativo+enabled) + drain `_process_flowpcp_row` (sent / retry / dead).
- Lógica FlowPCP a montante: `tests/test_flowpcp_poll.py`,
  `tests/test_flowpcp_fire_update.py`, `tests/test_flowpcp_client.py`,
  `tests/test_flowpcp_repo.py`, `tests/test_flowpcp_config.py`,
  `tests/test_flowpcp_exporter.py`, `tests/test_flowpcp_schema.py`.

```bash
.venv/bin/pytest tests/test_worker_drain_outbox.py tests/test_worker_poll_fire.py \
  tests/test_retention.py -v
```

## Armadilhas
- **Worker e FastAPI compartilham o mesmo SQLite via WAL.** Não usar
  `PRAGMA locking_mode=EXCLUSIVE` — quebraria o FastAPI.
- **Nunca usar `coalesce=False` ou `max_instances>1`** — jobs não são
  re-entrantes (Firebird connection pooling, locks de transação).
- **O gatilho periódico da reconciliação (`reconcile_fire`) vive de
  verdade no processo WEB, não no worker.** `scripts/setup-service.ps1`
  registra só `ui.py` como tarefa agendada no Windows do cliente — o
  worker (`python -m app.worker`) nunca rodou lá. `app/web/server.py`
  sobe `app.reconcile.runner.loop_periodico` numa thread daemon no
  `@app.on_event("startup")`; o `CronTrigger` registrado aqui no
  scheduler do worker é redundância intencional para os deploys docker
  onde os dois processos existem — a trava por-ambiente de
  `app.reconcile.runner` (10 min, `_TRAVA_S`) evita trabalho dobrado
  quando ambos disparam perto um do outro. Se o cliente reportar que a
  reconciliação periódica "não roda", olhar o log do `ui.py` (linha
  `reconcile.runner: loop_periodico ...`), não o do worker.
- **Kill switch `PORTAL_RECONCILE_PERIODICO=0`** desliga os dois gatilhos
  verdadeiramente periódicos (loop do web + `CronTrigger` do worker) sem
  redeploy — checado em `app.reconcile.runner._periodico_habilitado()`.
  Qualquer valor diferente de `"0"` (inclusive ausente) mantém ligado.
  **Não afeta** o botão manual (`POST /api/imported/reconciliar-fire`)
  nem o gatilho de entrada do operador (`POST /api/env/select`) — só os
  dois que varrem todos os ambientes sozinhos. `tests/conftest.py` seta
  essa env var para `"0"` globalmente na suíte, para nenhum teste que
  suba `TestClient(app)` abrir Firebird de verdade se a rodada cruzar
  07h/12h/18h locais.
- **`FIRE_TRIGGER_STATUS` vazio é seguro.** Steps 1–3 do poll rodam sempre
  (observabilidade); step 4 (enqueue) nunca dispara sem trigger configurado.
- **Retenção não deleta `imports`.** Apenas lifecycle_events/audit — o
  histórico de pedidos em `imports.snapshot_json` é preservado indefinidamente
  (política separada não implementada).
