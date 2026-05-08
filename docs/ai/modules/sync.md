# Módulo: sync (Portal → FlowPCP product catalog sync)

## Status
Produção. Liga em `/admin/ambientes/<slug>` (aba FlowPCP), testa conexão,
dispara `Sincronizar agora` e depois confirma o scheduler de 15 min na aba
`/admin/produtos/sync/<slug>`.

## Responsabilidade
Lê `PRODUTOS` + `PRODUTOS_KIT` do Firebird de cada ambiente, calcula delta
contra estado local (SQLite, hash por linha) e envia para o endpoint
`/api/portal-pedidos/produtos/sync` do FlowPCP. Idempotente, com circuit
breaker (5 falhas consecutivas).

## Arquivos críticos

**Engine:**
- [app/sync/fire_reader.py](../../../app/sync/fire_reader.py) — leitura SQL
  read-only do Firebird (PRODUTOS + PRODUTOS_KIT).
- [app/sync/canonical.py](../../../app/sync/canonical.py) — `canonical_json` +
  `canonical_hash(obj) -> str` (sha256 hex).
- [app/sync/diff_engine.py](../../../app/sync/diff_engine.py) —
  `compute_delta(...)`, `build_product_payload(p)`,
  `build_component_payload(c)`.
- [app/sync/sync_state_repo.py](../../../app/sync/sync_state_repo.py) —
  estado por ambiente (load/commit) + audit (`record_run_*`,
  `consecutive_failure_count`).
- [app/sync/runner.py](../../../app/sync/runner.py) — orquestrador
  (`run(env, trigger)`); wrapper externo emite métricas Prometheus.
- [app/sync/models.py](../../../app/sync/models.py) — pydantic: `ProductRow`,
  `ComponentRow`, `SyncDelta`, `RunResult`, `RunStatus`, `Trigger`.

**Integração HTTP (FlowPCP):**
- [app/integrations/flowpcp/client.py](../../../app/integrations/flowpcp/client.py)
  — `FlowPCPClient.sync_products(...)`, `health()`.
- [app/integrations/flowpcp/schema.py](../../../app/integrations/flowpcp/schema.py)
  — wire format pydantic.

**Glue:**
- [app/worker/jobs/flowpcp_product_sync.py](../../../app/worker/jobs/flowpcp_product_sync.py)
  — job APScheduler (15 min default).
- [app/web/routes_produtos_sync.py](../../../app/web/routes_produtos_sync.py)
  — admin: GET `/admin/produtos/sync/{slug}`,
  POST `/admin/produtos/sync-now/{slug}`,
  POST `/admin/produtos/sync/{slug}/reset-circuit`.
- [app/web/routes_environments.py](../../../app/web/routes_environments.py)
  — campos FlowPCP no editor de ambiente + endpoint
  `POST /api/admin/environments/{env_id}/flowpcp/test`.
- [app/persistence/environments_repo.py](../../../app/persistence/environments_repo.py)
  — `set_flowpcp_config`, `get_flowpcp_secret`, `to_flowpcp_config`,
  `mark_flowpcp_failure/success`, `reset_flowpcp_circuit`.

## Fluxo

```
scheduler / botão manual
   └─ runner.run(env, trigger)
        ├─ to_flowpcp_config(env)            # decrypt api_key
        ├─ pre-flight: enabled? circuit_open? config completa?
        ├─ active_env(env)
        │     ├─ record_run_start(...)
        │     ├─ read_products_snapshot(fb_cfg)
        │     ├─ read_components_snapshot(fb_cfg)
        │     ├─ load_product_state() / load_component_state()
        │     ├─ compute_delta(snapshot, state)
        │     ├─ FlowPCPClient.sync_products(payload, idempotency_key=sync_id)
        │     ├─ commit_states(...)          # exclui itens com erro
        │     ├─ mark_flowpcp_success | mark_flowpcp_failure
        │     └─ record_run_finish(result)
        └─ retorna RunResult
```

## Variáveis de ambiente

```
PORTAL_SYNC_ENABLED=1                # kill switch master (default 1)
PORTAL_SYNC_INTERVAL_MINUTES=15      # intervalo do scheduler (default 15)
```

Configuração específica do FlowPCP é **por ambiente** em
`/admin/ambientes/<slug>` (aba FlowPCP) — base_url, tenant_id, api_key.
API key cifrada via [app/security/secret_store.py](../../../app/security/secret_store.py).

## Endpoint FlowPCP

`POST /api/portal-pedidos/produtos/sync` — ver
[spec do FlowPCP](../../../docs/superpowers/specs/2026-05-08-portal-flowpcp-product-sync-design.md)
(seção 4 — Contrato HTTP).

## Detecção de mudança (hash)

Cada `ProductRow` ou `ComponentRow` é serializado em JSON canônico (sorted
keys, no whitespace) e hasheado em sha256. O hash é comparado com o estado
local (`product_sync_state.content_hash` / `component_sync_state.content_hash`).
Hash diferente → upsert. Ausente do snapshot → tombstone (produto) ou
component_tombstone (componente).

Hash inclui campos derivados: `tipo` baseado em `KIT_ATIVO='Sim'` OU
pertencimento a `PRODUTOS_KIT` como pai. Mudança em `PRODUTOS_KIT` que
recalcula pais altera hash de produtos antes "simples".

## Circuit breaker

Após 5 runs `failed` consecutivos por ambiente, `flowpcp_circuit_open=1`.
O scheduler pula esse ambiente. Reset:
- automático no próximo sucesso (`mark_flowpcp_success`)
- manual via `POST /admin/produtos/sync/<slug>/reset-circuit`

## Testes

```
.venv/bin/pytest tests/test_sync_*.py \
                 tests/test_flowpcp_client.py \
                 tests/test_admin_produtos_sync_routes.py \
                 tests/test_environments_repo_flowpcp.py -v
```

Cobertura:
- `tests/test_sync_canonical.py` — JSON canônico + hash determinístico
- `tests/test_sync_models.py` — pydantic models, validators
- `tests/test_sync_fire_reader.py` — leitura mockada de Firebird, classificação kit
- `tests/test_sync_state_repo.py` — schema + load/commit + run records + circuit
- `tests/test_sync_diff_engine.py` — upsert/tombstone/component_tombstone, edge cases
- `tests/test_flowpcp_client.py` — happy/401/403/5xx/partial/health (httpx MockTransport)
- `tests/test_sync_runner.py` — orquestração end-to-end
- `tests/test_admin_produtos_sync_routes.py` — rotas admin
- `tests/test_environments_repo_flowpcp.py` — CRUD por ambiente + circuit

## Métricas Prometheus

```
portal_product_sync_duration_seconds{env, status}     histogram
portal_product_sync_items_total{env, kind, status}    counter   # kind=produto|componente|tombstone
portal_product_sync_errors_total{env, reason}         counter
portal_product_sync_last_success_timestamp{env}       gauge
```

## Armadilhas

- **Não comitar state se a resposta não foi 2xx.** A próxima rodada precisa
  refazer o mesmo delta. Estado local SÓ avança em sucesso (parcial: avança
  apenas para itens sem erro).
- **Idempotency-Key é o `sync_id`.** Reuso quebra com 409 no FlowPCP — só
  reuse se quiser replay garantido (e o servidor responde com a resposta
  original cacheada).
- **`flowpcp_circuit_open` para o scheduler de tentar.** Reset manual em
  `POST /admin/produtos/sync/<slug>/reset-circuit` ou após
  `mark_flowpcp_success` (próximo sucesso).
- **Hash inclui campos derivados** (`tipo`). Mudar `PRODUTOS_KIT` recalcula
  pais e altera hash de produtos antes "simples".
- **`record_run_start` cria row com status='running'.** Crash entre start e
  finish deixa row órfã. Hoje inofensivo (`consecutive_failure_count` só
  conta `'failed'`); cuidado se uma feature futura ler `'running'`.
