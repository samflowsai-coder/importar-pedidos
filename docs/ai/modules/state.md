# Módulo: state (state machine + lifecycle events)

## Responsabilidade
Única API de mutação de estado de um pedido. Toda transição (`portal_status`,
`production_status`) passa por aqui, é validada contra uma tabela de
transições, é registrada como evento append-only em
`order_lifecycle_events` e projetada nas colunas de `imports` — tudo numa
única transação SQLite.

Por que existe: com 3 sistemas externos (ERP Firebird do cliente, Gestor de
Produção, Apontaê) escrevendo no mesmo pedido em pontos diferentes do
tempo, sem state machine explícita, surge bug do tipo "como esse pedido foi
parar nesse estado?". A SM transforma transições em invariante garantido
por código.

## Arquivos críticos
- `app/state/machine.py` — **puro**, sem I/O.
  - `PortalStatus` (parsed | sent_to_fire | cancelled | error)
  - `ProductionStatus` (none | production_requested | in_production |
    completed | production_cancelled)
  - `LifecycleEvent` — vocabulário completo de eventos, **incluindo os de
    fases futuras** (POST_TO_GESTOR_*, PRODUCTION_*, FIRE_STATUS_CHANGED).
    Vocabulário travado aqui evita drift.
  - `EventSource` (portal | fire | gestor | apontae | system)
  - `PORTAL_TRANSITIONS`, `PRODUCTION_TRANSITIONS` — dicts
    `{(state, event): new_state}`. Ausência = transição inválida.
  - `apply_event(portal, prod, event)` — pura, retorna novo estado ou
    `InvalidTransitionError`.
- `app/state/events.py` — DB-aware.
  - `transition(import_id, event, *, source, payload, trace_id,
    expected_state_version)` — **única API que muta status**. Lê estado
    atual + version, valida, insere evento, faz UPDATE, bumpa version.
    Tudo em transação.
  - `append_event(...)` — só log, sem projeção. Use raramente.
  - `replay_state(import_id) -> (PortalStatus, ProductionStatus)` —
    reconstrói estado a partir do log; usado em property test contra a
    projeção em `imports`.
  - `list_events(import_id)` — eventos em ordem cronológica.
  - `StaleStateError` — concorrência otimista violada.
- `app/observability/trace.py` — `trace_id` por pedido via ContextVar.
  - `with_trace_id(trace_id=None)` — context manager que mintsa ou herda;
    o id flui para todo log line e é gravado no evento.

## Como usar

```python
from app.state import LifecycleEvent, EventSource, transition
from app.observability.trace import with_trace_id

with with_trace_id(entry["trace_id"]):
    result = transition(
        import_id,
        LifecycleEvent.SEND_TO_FIRE_SUCCEEDED,
        source=EventSource.PORTAL,
        payload={"fire_codigo": 42, "items_inserted": 8},
    )
# result.portal_status == PortalStatus.SENT_TO_FIRE
# result.state_version foi bumpado
```

## Como adicionar um evento novo
1. Adicionar entrada em `LifecycleEvent` (machine.py).
2. Adicionar entrada(s) em `PORTAL_TRANSITIONS` e/ou
   `PRODUCTION_TRANSITIONS`. Se o evento é informacional (não muda
   status), mapear `(estado, evento) -> mesmo_estado`.
3. Cobrir em `tests/test_state_machine.py`. O teste
   `test_is_valid_matches_apply` verifica exaustividade.
4. Chamar `transition()` no call-site novo (worker, webhook, etc.).

## Testes
`tests/test_state_machine.py` — 20 testes cobrindo:
- Tabela pura (`apply_event`, `is_valid`).
- DB-backed (`transition`, idempotência, optimistic concurrency, cascade
  delete).
- Property test: replay do log == projeção em `imports` para 20 random
  walks da SM.

`.venv/bin/pytest tests/test_state_machine.py -v`

## Armadilhas
- **Nunca atribuir `portal_status` ou `production_status` direto.** Quem
  precisar mudar status: `transition()`. Quem precisar atualizar
  metadados (fire_codigo, db_result, output_files): `repo.update_fire_metadata`.
- Eventos de fases futuras já estão no enum mas suas transições só serão
  ativadas nas Fases 3-5. Não tente emitir antes — `InvalidTransitionError`.
- `replay_state` ignora eventos sem transição válida no estado corrente
  (defesa contra logs órfãos). Não use como verdade contra estados
  inválidos.
- `audit_log` (humano) e `order_lifecycle_events` (máquina) coexistem.
  Audit é breadcrumb operacional ("usuário X clicou Y às Z"). Lifecycle é
  fonte da verdade do estado.
- **Override manual de cliente NÃO é evento de SM.** Não muda `portal_status`
  nem `production_status` — é metadado sidecar em `imports.cliente_override_*`
  + breadcrumb em `audit_log` (`cliente_override_selected`). Não pollute
  o `LifecycleEvent` enum nem a tabela de transições com ações que não
  movem estado.
