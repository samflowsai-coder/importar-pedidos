# Módulo: worker (APScheduler + jobs)

## Responsabilidade
Processo separado (`python -m app.worker`) que executa jobs periódicos:
drena o outbox para o Gestor, poll o Firebird por mudanças de status e
faz retenção/backup do banco. Não compartilha estado em memória com o
FastAPI — comunicação exclusivamente via SQLite.

## Entry point
```bash
python -m app.worker          # direto
docker compose up worker      # via Docker (mesmo Dockerfile, cmd diferente)
```

## Arquivos críticos

### Scheduler
- `app/worker/scheduler.py` — bootstrap APScheduler com `SQLAlchemyJobStore`
  no SQLite do app. Três jobs registrados:

| Job | Trigger | Função |
|---|---|---|
| `drain_outbox` | interval 15s | `run_drain_outbox()` |
| `poll_fire` | interval 60s | `run_poll_fire()` |
| `retention` | cron hour=3 | `run_retention()` |

Todos com `coalesce=True`, `max_instances=1`, `misfire_grace_time=30s`.

### drain_outbox
- `app/worker/jobs/drain_outbox.py`
- Pega até 20 linhas `pending` do outbox e posta para o Gestor de Produção.
- Sucesso: `mark_sent` + `set_gestor_order_id` + `POST_TO_GESTOR_SENT`.
- Falha: backoff exponencial (30s → 2m → 10m → 1h → 6h → `dead` após 5 tentativas).
- Ao final de cada execução: chama `update_outbox_metrics()` (atualiza Gauges Prometheus).

### poll_fire
- `app/worker/jobs/poll_fire.py`
- Consulta `CAB_VENDAS.STATUS` dos pedidos `sent_to_fire + production_status=none`
  nos últimos 7 dias.
- Stampa `fire_status_last_seen` + `fire_status_polled_at`.
- Se status mudou: emite `FIRE_STATUS_CHANGED`.
- Se status == `FIRE_TRIGGER_STATUS` (env var): enfileira no outbox + emite
  `POST_TO_GESTOR_REQUESTED`. Env vazia = automação desligada (padrão seguro).
- No-op silencioso se Firebird não configurado.
- Duração medida em `portal_poll_fire_duration_seconds` (Histogram Prometheus).

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

## Testes
- `tests/test_worker_drain_outbox.py`
- `tests/test_worker_poll_fire.py`
- `tests/test_retention.py` — purge por tabela, VACUUM INTO, manutenção de 7 backups.

```bash
.venv/bin/pytest tests/test_worker_drain_outbox.py tests/test_worker_poll_fire.py \
  tests/test_retention.py -v
```

## Armadilhas
- **Worker e FastAPI compartilham o mesmo SQLite via WAL.** Não usar
  `PRAGMA locking_mode=EXCLUSIVE` — quebraria o FastAPI.
- **Nunca usar `coalesce=False` ou `max_instances>1`** — jobs não são
  re-entrantes (Firebird connection pooling, locks de transação).
- **`FIRE_TRIGGER_STATUS` vazio é seguro.** Steps 1–3 do poll rodam sempre
  (observabilidade); step 4 (enqueue) nunca dispara sem trigger configurado.
- **Retenção não deleta `imports`.** Apenas lifecycle_events/audit — o
  histórico de pedidos em `imports.snapshot_json` é preservado indefinidamente
  (política separada não implementada).
