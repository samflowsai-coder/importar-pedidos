# Módulo: persistence (SQLite log)

## Responsabilidade
Persistência local em SQLite: histórico de pedidos importados (`imports`),
log humano de auditoria (`audit_log`) e log append-only do ciclo de vida
(`order_lifecycle_events`, escrito pela state machine — ver `state.md`).

## Arquivos críticos
- `app/persistence/db.py` — conexão sqlite, schema, migrations idempotentes
  via `_COLUMN_MIGRATIONS`. Tabelas: `imports`, `audit_log`,
  `order_lifecycle_events`.
- `app/persistence/repo.py` — repositório de `imports` + `audit_log`.
  - `insert_import(entry)` — cria/upsert linha de pedido. **Não clobbera**
    colunas SM-owned (`portal_status`, `production_status`, `state_version`,
    `sent_to_fire_at`, `released_at`, `released_by`) nem as colunas de
    cliente override (`cliente_override_*`) em conflito de id.
  - `update_fire_metadata(import_id, fire_codigo, db_result, output_files,
    sent_to_fire_at)` — atualiza só campos auxiliares pós-Fire. Não toca
    status. Use antes de `transition(SEND_TO_FIRE_SUCCEEDED)`.
  - `set_gestor_order_id(import_id, gestor_order_id)` — id externo
    devolvido pelo Gestor de Produção. Use após `POST_TO_GESTOR_SENT`.
  - `set_client_override(import_id, *, codigo, razao, user=None)` —
    persiste seleção manual de cliente (recovery do CLIENT_NOT_FOUND).
    Last-write-wins; `audit_log` mantém histórico de tentativas. `user`
    fica `None` até auth (v5).
  - `append_audit(import_id, event_type, detail)` — log humano (quem
    clicou em quê). Convive com `lifecycle_events`.
- `app/persistence/outbox_repo.py` — fila durável de outbound. Ver
  `modules/gestor.md`. API: `enqueue`, `claim_next`, `mark_sent`,
  `mark_failed`, `find_by_idempotency_key`, `list_for_import`.
- `tools/migrate_log_to_sqlite.py` — migração one-shot do log antigo.

## Schema

### Campos da Fase 1 (state machine)
- `imports.trace_id TEXT` — UUID por pedido, mintado no boundary
  (commit/send-to-fire/webhook). Preservado em upserts via COALESCE.
- `imports.state_version INTEGER NOT NULL DEFAULT 1` — bump em cada
  `transition()` para optimistic concurrency.
- `order_lifecycle_events` — tabela append-only, FK em `imports`.
  Cascade DELETE. Lida exclusivamente por `app.state.transition`.

### Campos da Fase 3 (Gestor de Produção)
- `imports.gestor_order_id TEXT` — id externo devolvido pelo Gestor.
  Setado por `repo.set_gestor_order_id` após `POST_TO_GESTOR_SENT`.
- `outbox` — fila durável de chamadas outbound. `idempotency_key UNIQUE`,
  `status pending|sent|dead`, `attempts`, `next_attempt_at` (backoff),
  `last_error`, `response_json`, `trace_id`. FK em `imports`, cascade
  DELETE. Ver `modules/gestor.md`.

### Campos da Fase 4 (webhooks inbound)
- `imports.apontae_order_id TEXT` — id do Apontaê. Setado em qualquer
  webhook que carregue o campo (idempotente).
- `inbound_idempotency` — dedup de webhooks por
  `(provider, event_id)`. PK composta. Cacheia `response_status` +
  `response_body` para replay devolver bytes idênticos. Ver
  `modules/security.md`.

### Cliente override (CLIENT_NOT_FOUND recovery)
- `imports.cliente_override_codigo INTEGER` — `CADASTRO.CODIGO` escolhido
  pelo usuário no picker. Lido por `_send_one_to_fire` e passado como
  `override_client_id` ao `FirebirdExporter.export`.
- `imports.cliente_override_razao TEXT` — razão social denormalizada
  (evita re-query ao Fire só para mostrar na UI).
- `imports.cliente_override_at TEXT` — ISO timestamp da seleção.
- `imports.cliente_override_by TEXT` — email do usuário autenticado que
  aplicou (vem de `User.email` via `require_user`). NULL apenas em rows
  legadas anteriores ao auth.
- Mutação só via `repo.set_client_override(...)`; `insert_import` upsert
  NÃO clobbera essas colunas.
- Override **não muda `portal_status`** — não há lifecycle event próprio;
  rastreio operacional fica em `audit_log` com `event_type=cliente_override_selected`.

## Testes
- `tests/test_persistence_repo.py` —
  `.venv/bin/pytest tests/test_persistence_repo.py -v`
- `tests/test_state_machine.py` (cobre transition + projection drift) —
  `.venv/bin/pytest tests/test_state_machine.py -v`

## Armadilhas
- Banco é local-only por design. Não promover sem mudar estratégia
  (multi-tenant é v5).
- **Nunca escrever direto em `portal_status`/`production_status`** em
  novos call-sites. Toda mutação passa por `app.state.transition()` — o
  `repo.insert_import` upsert já protege essas colunas, mas o invariante
  precisa ser respeitado também na entrada (criação inicial).
