# Módulo: integrations/gestor (Gestor de Produção)

## Status: PLACEHOLDER WIRE FORMAT

Esta integração foi construída na **Fase 3** com spec mockada. Quando a
spec real chegar:
1. Editar [app/integrations/gestor/schema.py](../../../app/integrations/gestor/schema.py)
   (campos do request/response).
2. Ajustar [app/integrations/gestor/mapper.py](../../../app/integrations/gestor/mapper.py)
   (conversões — datas, fields opcionais, etc.).
3. Confirmar `_CREATE_ORDER_PATH` em
   [app/integrations/gestor/client.py](../../../app/integrations/gestor/client.py).
4. Atualizar `.env.example` se auth mudar de Bearer.

A plumbing (outbox, route, state machine, retry, trace_id) **não muda**.

## Responsabilidade
Posta um pedido (já em Fire) para a API do Gestor de Produção, que
encaminha para o Apontaê (PCP). Captura o id externo (`gestor_order_id`)
para correlação dos webhooks de status (Fase 4).

## Arquivos críticos
- [app/integrations/gestor/schema.py](../../../app/integrations/gestor/schema.py)
  — modelos pydantic do wire format (PLACEHOLDER).
- [app/integrations/gestor/mapper.py](../../../app/integrations/gestor/mapper.py)
  — `build_gestor_payload(import_id, order, metadata)`. Converte
  `DD/MM/YYYY` → `YYYY-MM-DD`. Default de `external_item_id` é
  `product_code` ou índice posicional.
- [app/integrations/gestor/client.py](../../../app/integrations/gestor/client.py)
  — `GestorClient`, `GestorClientError`, `GESTOR_TARGET_NAME='gestor'`.
  Usa `OutboundClient` com `idempotent_post_policy`. Lê
  `GESTOR_BASE_URL` + `GESTOR_API_KEY` do env (ou aceita `outbound`
  injetado em testes).
- [app/persistence/outbox_repo.py](../../../app/persistence/outbox_repo.py)
  — fila durável. API: `enqueue`, `claim_next`, `mark_sent`,
  `mark_failed`, `find_by_idempotency_key`, `list_for_import`,
  `OutboxDuplicateError`.

## Fluxo (Fase 3 — gatilho manual + drain inline)

```
POST /api/imported/{import_id}/post-to-gestor
   │
   ├─ valida pré-condições (portal_status='sent_to_fire',
   │  production_status='none', snapshot válido)
   │
   ├─ build_gestor_payload(...)
   ├─ outbox.enqueue(target='gestor', endpoint='/v1/orders', ...)
   │     ↳ idempotency_key = uuid4 (UNIQUE em DB)
   │
   ├─ transition(POST_TO_GESTOR_REQUESTED)
   │     ↳ production_status: none → production_requested
   │
   ├─ GestorClient().create_order(...)  ← drain inline
   │     ├─ sucesso: outbox.mark_sent + repo.set_gestor_order_id
   │     │           + transition(POST_TO_GESTOR_SENT)
   │     │             ↳ production_status: requested → in_production
   │     │
   │     └─ falha: outbox.mark_failed (não-dead, próximo worker tenta)
   │              + transition(POST_TO_GESTOR_FAILED)
   │
   └─ retorna { gestor_order_id, production_status, outbox_id, trace_id }
```

Na **Fase 5**, o worker de polling assume o drain — o passo "drain inline"
sai da rota e vira job background com backoff (30s, 2m, 10m, 1h, 6h, dead).

## Variáveis de ambiente

```
GESTOR_BASE_URL=https://api.gestor-producao.example.com  # PLACEHOLDER
GESTOR_API_KEY=token-do-gestor                           # PLACEHOLDER
```

## Testes
- [tests/test_outbox_repo.py](../../../tests/test_outbox_repo.py) — 11
  testes do repositório (enqueue, idempotência, claim_next com filtro de
  target/timeline, mark_sent/failed, cascade delete, trace_id contextvar).
- [tests/test_gestor_integration.py](../../../tests/test_gestor_integration.py)
  — 12 testes: mapper (formatos de data, campos opcionais, metadata),
  GestorClient (sucesso, 4xx, schema mismatch, missing api_key), rota
  end-to-end com `httpx.MockTransport` (happy path, recusa fora de Fire,
  recusa duplicado, marcação de falha em 4xx).

`.venv/bin/pytest tests/test_outbox_repo.py tests/test_gestor_integration.py -v`

## Webhook inbound (Fase 4)

Recebe atualizações parciais de status do Gestor (que repassa do Apontaê).

**Rota:** `POST /api/webhooks/gestor`
**Headers obrigatórios:** `X-Signature`, `X-Timestamp` (ver
`modules/security.md` para HMAC).
**Schema do body:** `app/integrations/gestor/webhook_schema.py`
(`GestorWebhookEvent`) — **PLACEHOLDER**, espelha um shape Stripe-like
com `event_id`, `event_type`, `external_id` (= portal import_id),
`gestor_order_id`, `apontae_order_id`, `occurred_at`, `payload`.

**Pipeline da rota** (`app/web/webhooks.py`):

```
verify HMAC + timestamp  →  401/403 se falhar
   ↓
parse JSON + pydantic    →  422 se shape errado
   ↓
record_attempt(provider, event_id)  →  cached → replay (idempotente)
   ↓
resolve import_id (external_id preferido; fallback gestor_order_id)
   →  404 se não correlaciona
   ↓
if apontae_order_id: repo.set_apontae_order_id(...)
   ↓
transition(event_type → LifecycleEvent, source=GESTOR, payload=...)
   →  409 InvalidTransitionError se estado errado
   ↓
finalize(provider, event_id, status, body)  →  cache da resposta
```

**Mapping event_type → LifecycleEvent** (em `app/web/webhooks.py`):
- `production_update` → `PRODUCTION_UPDATE` (mantém in_production)
- `production_completed` → `PRODUCTION_COMPLETED` (terminal)
- `production_cancelled` → `PRODUCTION_CANCELLED` (terminal)

Adicionar evento novo no enum SEM mapear aqui surfaces como 422.

**Idempotência:** dedup por `(provider='gestor', event_id)` em
`inbound_idempotency`. Replay retorna o body cacheado com mesmo status.
Em-flight (response_status NULL) retorna 202 para o provider tentar
novamente — evita race entre dois retries simultâneos.

**Trace_id:** webhook adota o `trace_id` original do pedido (mintado no
commit). Logs do webhook se conectam aos do commit/send-to-fire.

**Testes:** `tests/test_webhooks.py` (17 testes), `tests/test_hmac_verify.py`
(11), `tests/test_idempotency_repo.py` (8).

## Armadilhas

- **Não mexer em portal_status pelo client/mapper.** Toda mutação passa
  por `app.state.transition`. O client só fala HTTP, o mapper só converte.
- **Idempotency-Key é UNIQUE em DB.** Reusar a mesma key gera
  `OutboxDuplicateError`. Cada call de `/api/imported/{id}/post-to-gestor`
  minta uma key nova (uuid4).
- **`extra="ignore"` em todos os schemas.** Se o Gestor adicionar campos
  novos, o cliente não quebra. Se remover ou renomear, falha em
  `ValidationError` → `GestorClientError("schema validation")`.
- **Fase 3 é gatilho manual.** O usuário precisa chamar a rota
  explicitamente. Fase 5 automatiza via worker que reage ao status do ERP.
- **Drain inline é síncrono.** Latência da rota = latência do Gestor +
  retries (até 3 attempts × backoff). Aceitável enquanto volume é baixo
  e gatilho é manual; Fase 5 desacopla.
