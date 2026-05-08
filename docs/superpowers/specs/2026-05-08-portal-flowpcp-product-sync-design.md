# Spec — Sincronização de Produtos: Portal Pedidos → FlowPCP

**Data:** 2026-05-08
**Status:** Design aprovado, aguardando implementação
**Lado coberto por esta spec:** Portal Pedidos (on-prem). A spec correspondente do FlowPCP vive em `GestorProduction/pcp-app/docs/superpowers/specs/2026-05-08-flowpcp-portal-product-sync-design.md` e referencia o **contrato HTTP** definido aqui.

---

## 1. Objetivo

Sincronizar 100% dos produtos do ERP Fire (Firebird, on-prem do cliente) para o FlowPCP (cloud, multi-tenant), incluindo a relação Kit (produto pai → produtos filhos), de forma que o Apontaê tenha o catálogo sempre coerente com a fábrica e possa abrir ordens de produção sem cadastro paralelo.

**Não-objetivos (fora deste spec):**
- Sincronizar BOM/insumos/ficha técnica (`bom_items`, `insumos`).
- Sincronizar fotos do produto (`PRODUTOS.FOTO_PROD` BLOB).
- Sync reverso FlowPCP → Portal.
- Webhook real-time a partir de trigger Firebird.

---

## 2. Premissas trancadas no brainstorming

| # | Decisão | Motivo |
|---|---|---|
| 1 | **Cadência híbrida**: scheduler 15 min + botão manual `Sincronizar agora` | Fluido sem mexer em trigger no banco do cliente |
| 2 | **Escopo: todos os produtos ativos** classificados como `simples` ou `kit` (pai em `PRODUTOS_KIT`); inativos viram `ativo=false` | 100% do catálogo, mas sem poluir com produto morto |
| 3 | **Change detection: hash por linha em SQLite local** (`product_sync_state`, `component_sync_state`) | Fire não tem `updated_at` confiável; hash é determinístico, idempotente, recuperável |
| 4 | **Auth: Bearer token por ambiente**, configurado em `/admin/ambientes/:slug` (cifrado via `secret_store`); FlowPCP valida `Authorization` → resolve `tenant_id` → rejeita se `body.tenant_id` divergir | Multi-empresa do Portal mapeando 1:1 para tenants do FlowPCP, sem possibilidade de cross-tenant leak |
| 5 | **Chave canônica: `codigo = SEQ::text`** (estável) + `codigo_alternativo = CODPROD_ALTERN` (humano) + `ean = CODIGO_EAN13` | Histórico de produção nunca quebra; UX humana preservada |
| 6 | **Contrato: endpoint único bulk** `POST /api/portal-pedidos/produtos/sync` com payload `{produtos:[…], componentes:[…]}` processado atomicamente em transação Postgres no FlowPCP, idempotente via `Idempotency-Key`, soft-delete embutido (`ativo=false`) | Volume típico cabe em uma chamada; transação evita estado intermediário inconsistente |

---

## 3. Modelo de dados — mapeamento Fire ↔ FlowPCP

### 3.1 `produtos`

| FlowPCP `produtos` | Fire `PRODUTOS` | Transformação |
|---|---|---|
| `tenant_id` | (config do ambiente) | injetado no payload |
| `codigo` | `SEQ::text` | `str(seq)`, sem zero-padding |
| `codigo_alternativo` *(coluna nova no FlowPCP)* | `CODPROD_ALTERN` | `trim`, `null` se vazio |
| `nome` | `DESCRICAO` | `trim`; rejeita vazio |
| `unidade` | `UNIDADE` | lower; default `un` se nulo |
| `ean` | `CODIGO_EAN13` | `trim`; null se vazio ou só zeros |
| `tipo` | derivado | `kit` se `KIT_ATIVO='Sim'` **ou** `SEQ` aparece como `CODPRODUTO_PAI` em `PRODUTOS_KIT`; senão `simples` |
| `ativo` | `INATIVO` | `INATIVO='Sim' → false`, ausência da query → `false` (tombstone) |
| `tem_bom` | constante `false` | escopo futuro |

### 3.2 `produto_componentes`

| FlowPCP `produto_componentes` | Fire `PRODUTOS_KIT` | Transformação |
|---|---|---|
| `produto_pai_id` | `CODPRODUTO_PAI` | resolve `(tenant_id, codigo=str(pai))` no upsert |
| `produto_filho_id` | `CODPRODUTO` | resolve `(tenant_id, codigo=str(filho))` no upsert |
| `quantidade` | `QTD` | `numeric(12,4)`; rejeita `<=0` |
| `posicao` | constante `0` | Fire não tem ordem |

### 3.3 Estado local (SQLite por ambiente, em `app_state_<slug>.db`)

```sql
CREATE TABLE product_sync_state (
  seq          INTEGER PRIMARY KEY,
  content_hash TEXT NOT NULL,
  last_synced_at TEXT NOT NULL    -- ISO8601 UTC
);

CREATE TABLE component_sync_state (
  codigo       INTEGER PRIMARY KEY,  -- PRODUTOS_KIT.CODIGO
  content_hash TEXT NOT NULL,
  last_synced_at TEXT NOT NULL
);

CREATE TABLE product_sync_runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  sync_id      TEXT NOT NULL UNIQUE,
  trigger      TEXT NOT NULL,            -- 'scheduler' | 'manual' | 'reconcile'
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  status       TEXT NOT NULL,            -- 'running' | 'applied' | 'failed' | 'partial'
  delta_count_produtos    INTEGER NOT NULL DEFAULT 0,
  delta_count_componentes INTEGER NOT NULL DEFAULT 0,
  delta_count_tombstones  INTEGER NOT NULL DEFAULT 0,
  applied_count INTEGER NOT NULL DEFAULT 0,
  errors_json  TEXT,                     -- array de {codigo, reason}
  trace_id     TEXT
);
CREATE INDEX ix_product_sync_runs_started ON product_sync_runs(started_at DESC);
```

---

## 4. Contrato HTTP (source of truth — leia-se também na spec do FlowPCP)

### 4.1 Endpoint

```
POST {flowpcp_base_url}/api/portal-pedidos/produtos/sync
Authorization: Bearer <api_key>
Content-Type: application/json
Idempotency-Key: <ulid do sync>
X-Trace-Id: <ulid de correlação>
```

### 4.2 Request body

```json
{
  "tenant_id": "9b2c-…-uuid",
  "sync_id": "01HXXXX...",
  "generated_at": "2026-05-08T12:00:00Z",
  "delta_kind": "incremental",
  "produtos": [
    {
      "codigo": "10042",
      "codigo_alternativo": "CAL-0042-PR",
      "nome": "TÊNIS XYZ PRETO 38",
      "unidade": "un",
      "ean": "7891234567890",
      "tipo": "kit",
      "ativo": true
    },
    { "codigo": "10043", "ativo": false }
  ],
  "componentes": [
    {
      "produto_pai_codigo": "10042",
      "produto_filho_codigo": "10042-CABEDAL",
      "quantidade": 1.0,
      "posicao": 0
    }
  ]
}
```

**Observações do contrato:**
- `delta_kind`: `incremental` na operação normal; `full_reconcile` quando o Portal opta por reenviar tudo (ex.: state local foi perdido; spec não exige isso na v1, mas o campo existe pra extensão).
- Tombstone: produto marcado `ativo:false` sem outros campos é válido (servidor desativa).
- Componente referenciando `produto_pai_codigo` ou `produto_filho_codigo` que não existe no payload **e não existe no banco** → erro registrado no `errors[]` do response, componente ignorado, restante da transação prossegue.
- Quando um pai (`produto_pai_codigo`) está presente no payload `componentes`, o conjunto enviado é **autoritativo** para esse pai: componentes existentes no banco daquele pai que não estiverem no delta são removidos (`DELETE`). Pais ausentes do payload **não** têm componentes mexidos.

### 4.3 Response (200)

```json
{
  "sync_id": "01HXXXX...",
  "applied": { "produtos": 12, "componentes": 5, "tombstones": 1 },
  "skipped": 0,
  "errors": [
    { "codigo": "10044", "reason": "componente_filho_inexistente" }
  ]
}
```

### 4.4 Códigos de erro

| Código | Cenário | Lado Portal faz |
|---|---|---|
| `200` | Aplicado total ou parcial | Comita state dos itens **sem erro**; itens em `errors[]` ficam pendentes pro próximo run |
| `200` (replay) | Mesmo `sync_id` já processado | Não comita state extra; trata como sucesso |
| `400` | Payload inválido (Zod fail) | Marca run como `failed`, log do erro, **não retenta** (bug → exige fix) |
| `401` | Bearer ausente/inválido | Marca run `failed`, alerta admin (key revogada ou errada) |
| `403` | `body.tenant_id` ≠ tenant da key | Marca run `failed`, alerta admin (config errada) |
| `409` | `Idempotency-Key` repetido com payload diferente | Bug — log + `failed` + alerta |
| `422` | Schema válido mas regra de negócio falhou | `failed`, log, retentável apenas se transitório |
| `5xx` | Falha do servidor | Retry 3× via `OutboundClient`; se persistir, `failed` + state local não comita |

---

## 5. Componentes — Portal

### 5.1 Estrutura de pastas

```
app/sync/
├── __init__.py
├── fire_reader.py        # leitura + canonicalização + hash
├── diff_engine.py        # snapshot vs state → SyncDelta
├── flowpcp_client.py     # HTTP client (usa OutboundClient)
├── sync_state_repo.py    # SQLite por ambiente
├── runner.py             # orquestrador (read → diff → send → commit)
└── models.py             # ProductRow, ComponentRow, SyncDelta (pydantic)
```

### 5.2 Responsabilidades

**`fire_reader.py`**
- `read_products_snapshot(env) -> list[ProductRow]`
  - SQL: `SELECT SEQ, DESCRICAO, UNIDADE, CODIGO_EAN13, CODPROD_ALTERN, COALESCE(INATIVO,'Nao'), COALESCE(KIT_ATIVO,'Nao') FROM PRODUTOS`
  - Marca `is_kit = (KIT_ATIVO='Sim') or (SEQ in pai_set)` (com `pai_set` calculado a partir de `PRODUTOS_KIT`).
- `read_components_snapshot(env) -> list[ComponentRow]`
  - SQL: `SELECT CODIGO, CODPRODUTO_PAI, CODPRODUTO, QTD FROM PRODUTOS_KIT`
  - Filtra linhas com `CODPRODUTO_PAI IS NULL OR CODPRODUTO IS NULL OR QTD<=0` (registra warning).
- `canonical_hash(row) -> str` — JSON canônico (chaves ordenadas, sem espaço) sha256 hex. Hash inclui todos os campos enviados ao FlowPCP.

**`sync_state_repo.py`**
- `load_product_state(env) -> dict[seq, hash]`
- `load_component_state(env) -> dict[codigo, hash]`
- `commit_states(env, *, products, components)` — em uma transação SQLite, atualiza/insere/remove conforme passado pelo runner.
- `record_run_start(...)` / `record_run_finish(...)`.

**`diff_engine.py`**
- `compute_delta(snapshot_products, snapshot_components, state_products, state_components) -> SyncDelta`
- Para produtos: `inserts` (não estão em state), `updates` (hash mudou), `tombstones` (estão em state e não no snapshot **ou** vieram com `INATIVO=Sim`).
- Para componentes: mesma lógica; `tombstones` de componentes viram **omissão** (servidor remove o que não vier para um pai presente).

**`flowpcp_client.py`**
- `class FlowPCPClient` recebe `base_url`, `api_key`, `tenant_id`, e um `OutboundClient` injetado (igual `GestorClient`).
- `sync_products(delta: SyncDelta, *, sync_id: str, trace_id: str) -> SyncResponse`.
- Reusa `idempotent_post_policy` (3 retries, backoff exp).

**`runner.py`**
- `run(env, *, trigger: str) -> RunResult`
  1. Lock por slug (já existe lock util? se não, simples `fcntl` ou advisory na própria SQLite).
  2. `record_run_start`.
  3. Lê snapshots + state.
  4. `compute_delta`.
  5. Se delta vazio: `record_run_finish(applied)` e retorna.
  6. Monta payload, chama client.
  7. Se 2xx: `commit_states` (excluindo itens com erro), `record_run_finish(applied|partial)`.
  8. Se erro de rede/HTTP/auth: `record_run_finish(failed)`, **não comita state**.

### 5.3 Mudanças em código existente

- **`app/persistence/environments_repo.py`** — colunas novas:
  ```
  flowpcp_enabled            INTEGER NOT NULL DEFAULT 0
  flowpcp_base_url           TEXT
  flowpcp_tenant_id          TEXT     -- UUID
  flowpcp_api_key_ciphertext BLOB     -- via secret_store
  ```
  Migration SQLite com `ALTER TABLE` (estilo das migrations existentes).

- **`app/web/admin/ambientes.py`** — aba "FlowPCP" no editor. Botão "Testar conexão" chama `GET {base}/api/portal-pedidos/health` com a key.

- **`app/worker/scheduler.py`** — registra job:
  ```python
  scheduler.add_job(
      sync_products_all_envs,
      "interval", minutes=15,
      id="flowpcp_product_sync",
      max_instances=1, coalesce=True,
  )
  ```
  Itera ambientes com `flowpcp_enabled=1`.

- **Nova rota `app/web/admin/produtos_sync.py`**:
  - `GET /admin/produtos/sync/:slug` — última run + histórico (paginado).
  - `POST /admin/produtos/sync-now/:slug` — dispara `runner.run(env, trigger='manual')` inline (com timeout reasonable; resposta SSE/streaming opcional na v2).

### 5.4 Variáveis de ambiente

Nenhuma nova env var global. **Toda config de FlowPCP é por ambiente**, via UI.

Apenas comportamento global, em `.env.example`:
```
PORTAL_SYNC_ENABLED=1                # kill switch master
PORTAL_SYNC_INTERVAL_MINUTES=15      # default 15
```

---

## 6. Erros, retry, observabilidade

### 6.1 Retry e idempotência
- `OutboundClient` faz 3 tentativas com backoff exponencial em 5xx/timeout. 4xx não retenta.
- `Idempotency-Key = sync_id` garante que retry da mesma request não duplica.
- State local **só comita** se a resposta for 2xx. Falhas → próximo run de 15 min refaz.

### 6.2 Falha por ambiente — circuit breaker leve
- Após **5 runs `failed` consecutivos** num ambiente, o scheduler marca o ambiente como `flowpcp_circuit_open=1` (campo novo) e pula ele. Reset manual no admin (botão "Tentar novamente"). Evita martelar API quebrada.

### 6.3 Métricas Prometheus (extender `app/observability/metrics.py`)

```
portal_product_sync_duration_seconds{env, status}    histogram
portal_product_sync_items_total{env, kind, status}   counter   # kind=upsert|tombstone|component
portal_product_sync_errors_total{env, reason}        counter
portal_product_sync_last_success_timestamp{env}      gauge
```

### 6.4 Tracing
`trace_id` propagado no header `X-Trace-Id` (existe `app/observability/trace.py`). FlowPCP loga com o mesmo trace_id → debug cross-system trivial.

### 6.5 Logs

- `INFO`: início/fim de run, contagens, sync_id.
- `WARNING`: erro parcial (item ignorado), circuit breaker abrindo.
- `ERROR`: falha total, auth fail, schema fail.
- **Nunca** logar `api_key` clara. Header `Authorization` redacted no logger (já existe convenção no projeto).

---

## 7. Segurança

- HTTPS obrigatório (TLS 1.2+).
- API key 32 bytes random, prefix `pp_live_`. Armazenada cifrada via `secret_store.encrypt(...)` (já existe).
- Validação tempo-constante no FlowPCP (responsabilidade da spec do FlowPCP).
- Rate limit no FlowPCP: 60 req/min por key (FlowPCP já tem rate limit infra).
- `Authorization` redacted em logs do Portal.
- Nunca expor `api_key` no DOM da UI admin (usa input `password` + reveal explícito).
- Botão "Revogar" no FlowPCP gera key nova; antiga vira inválida (Portal precisa atualizar).

---

## 8. Testes

| Arquivo | Cobertura |
|---|---|
| `tests/test_sync_fire_reader.py` | Hash determinístico; tratamento de NULL/whitespace; classificação `kit` por `KIT_ATIVO` ou pertencimento a `PRODUTOS_KIT` |
| `tests/test_sync_diff_engine.py` | Inserts, updates, tombstones por sumiço, tombstones por `INATIVO=Sim`, componentes adicionados/removidos por pai, idempotência (state == snapshot → delta vazio) |
| `tests/test_sync_state_repo.py` | Persistência, commit atômico (rollback se falhar parcial), histórico de runs |
| `tests/test_sync_flowpcp_client.py` | `httpx.MockTransport`: 200, 200 com errors[], 401, 403, 422, 5xx com retry, idempotência |
| `tests/test_sync_runner.py` | End-to-end: Fire fake (in-memory) + FlowPCP mock; flow happy, parcial, falha total, replay, circuit breaker |
| `tests/test_admin_produtos_sync_route.py` | Auth admin, dispatch manual, listagem de runs |

Comando: `.venv/bin/pytest tests/test_sync_*.py tests/test_admin_produtos_sync_route.py -v`

---

## 9. Rollout

1. **FlowPCP primeiro**: migrations (`codigo_alternativo`, `portal_sync_runs`, `portal_api_keys`), endpoint, UI de gerar API key, deploy. Endpoint funciona mas ninguém aponta.
2. **Portal**: migration `environments`, módulo `app/sync/`, UI admin.
3. **Smoke em ambiente de teste** (cópia da MM ou ambiente sandbox): admin gera key no FlowPCP, cola no Portal, clica "Testar conexão", clica "Sincronizar agora", valida no FlowPCP.
4. **Liga scheduler 15 min** em MM. Observa métricas por 24h.
5. **Replica para Nasmar e demais ambientes** ativados.
6. **Documentar em `docs/ai/modules/sync.md`** + atualizar `docs/ai/00-index.md`.

---

## 10. Aberto / Decisões deferidas

- **Fotos do produto** (`PRODUTOS.FOTO_PROD` BLOB) — fora dessa v1. Quando entrar, será via storage do Supabase referenciado por URL no payload (não embed BLOB).
- **Ficha técnica/BOM** — fora dessa v1.
- **Sync de clientes (`CADASTRO`)** — fora; FlowPCP já tem `clientes` table mas isso é outro spec.
- **Modo `full_reconcile` automático** — não implementado na v1; campo no payload já existe pra extensão.
- **Notificação ao admin em falha persistente** — webhook/email — pode entrar via `app/observability` em outro PR.
- **Paginação do payload** — só quando catálogo passar de ~10k produtos.

---

## 11. Critérios de aceite

- [ ] Admin consegue habilitar FlowPCP num ambiente do Portal e testar conexão.
- [ ] Sync manual transfere todos os produtos ativos do Fire para o FlowPCP em um único call.
- [ ] Sync manual roda uma segunda vez sem duplicar nada (idempotente, hash não muda).
- [ ] Alterar `DESCRICAO` de um produto no Fire e rodar sync atualiza só esse produto no FlowPCP.
- [ ] Marcar `INATIVO='Sim'` em um produto no Fire e rodar sync vira `ativo=false` no FlowPCP.
- [ ] Adicionar/remover componentes de um kit no Fire e rodar sync reflete no FlowPCP (incluindo deletes).
- [ ] Scheduler 15 min roda automaticamente, métricas Prometheus expostas.
- [ ] Falha de rede no FlowPCP não comita state local; próxima rodada recupera.
- [ ] Token revogado no FlowPCP gera 401 no Portal e marca run como `failed` sem retry infinito.
- [ ] 5 falhas seguidas abrem circuit breaker; admin consegue resetar.
- [ ] Suíte de testes passa: `pytest tests/test_sync_*.py -v`.

---

## 12. Referências cruzadas

- Spec FlowPCP: `GestorProduction/pcp-app/docs/superpowers/specs/2026-05-08-flowpcp-portal-product-sync-design.md`
- Padrão de outbound HTTP existente: `app/integrations/gestor/client.py`, `app/http/outbound_client.py`
- Padrão de secret encryption: `app/security/secret_store.py`
- Padrão de multi-ambiente: `docs/ai/modules/environments.md`
- Tabela Fire `PRODUTOS_KIT` (93 linhas em backup MM): `bkp fire/schema_report.txt`
