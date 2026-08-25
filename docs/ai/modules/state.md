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
  - `PortalStatus` (parsed | sent_to_fire | found_in_fire | cancelled | error)
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

## `found_in_fire` (reconciliação com o Fire)

Estado novo (`PortalStatus.FOUND_IN_FIRE = "found_in_fire"`,
`machine.py:25`): **existe no Fire, mas o portal NÃO foi quem inseriu.**
Distinto de `sent_to_fire` (que significa "o próprio portal chamou o
INSERT") só para preservar **quem** cadastrou — o cliente roda
`EXPORT_MODE=xlsx`, a operadora exporta o XLS e cadastra à mão no Fire, e
sem este estado o pedido ficava `parsed` pra sempre. Gravado por
`app/persistence/repo.py::mark_found_in_fire` via compare-and-set direto
em SQL, não por `transition()` — o `UPDATE ... WHERE portal_status =
'parsed'` é quem decide o vencedor entre web e worker concorrentes; só o
vencedor grava o evento `LifecycleEvent.FOUND_IN_FIRE` /
`EventSource.FIRE`.

Transições wiradas em `PORTAL_TRANSITIONS` (`machine.py:110-117`):
`(PARSED, FOUND_IN_FIRE) → FOUND_IN_FIRE`, e a partir de `FOUND_IN_FIRE`
os MESMOS eventos que `SENT_TO_FIRE` aceita —
`FIRE_STATUS_CHANGED`, `POST_TO_GESTOR_REQUESTED/SENT/FAILED`,
`PRODUCTION_UPDATE/COMPLETED/CANCELLED` — todas mantendo o estado
(`PRODUCTION_TRANSITIONS` correspondente em `machine.py:154`).

**Por que essas transições precisam existir a partir daqui, e não só a
partir de `sent_to_fire`:** `_enqueue_gestor`
(`app/worker/jobs/poll_fire.py:107-155`) primeiro grava a linha no outbox
(`outbox_repo.enqueue`, linha 129) e só DEPOIS chama `transition(...,
POST_TO_GESTOR_REQUESTED, ...)` (linha 137). Se `FOUND_IN_FIRE` não
aceitasse esse evento, `transition()` levantaria `InvalidTransitionError`
— e o `except Exception` genérico em volta do bloco inteiro
(`poll_fire.py:154-155`) engoliria o erro num `logger.exception` e
retornaria normalmente. O outbox já teria a linha (drenável, vai tentar
postar pro Gestor de qualquer forma), mas o `portal_status` do pedido
ficaria travado em `found_in_fire` sem o `production_status` ter avançado
para `production_requested` — **outbox órfão**: a tela mostra o pedido
como se nada tivesse acontecido, mas já existe uma postagem em voo pro
Gestor por trás.

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
