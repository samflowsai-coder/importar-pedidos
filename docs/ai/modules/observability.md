# Módulo: observability (trace_id + métricas Prometheus)

## Responsabilidade
Dois subsistemas independentes: propagação de `trace_id` por request/job
(via `contextvars`) e métricas Prometheus expostas em `/metrics`.

## Arquivos críticos

### Trace ID
- `app/observability/trace.py`
- `trace_id_var: ContextVar[str]` — UUID4 minted na entrada de cada operação.
- API:
  - `new_trace_id()` → str (UUID4.hex)
  - `current_trace_id()` → str atual do contexto
  - `set_trace_id(value)` → Token (para `reset_trace_id`)
  - `reset_trace_id(token)` → restaura valor anterior
  - `with_trace_id(trace_id=None)` → context manager; gera se não fornecido
- Propagado em: logs loguru (campo `trace_id`), header `X-Trace-Id` nas
  chamadas HTTP outbound, coluna `trace_id` em `order_lifecycle_events` e `outbox`.

### Métricas Prometheus
- `app/observability/metrics.py`

| Nome | Tipo | Atualizado por |
|---|---|---|
| `portal_outbox_pending_total` | Gauge | `update_outbox_metrics()` — drain_outbox job a cada 15s |
| `portal_outbox_dead_total` | Gauge | idem |
| `portal_poll_fire_duration_seconds` | Histogram | `poll_fire_duration_seconds.time()` — poll_fire job |
| `portal_webhook_received_total{provider}` | Counter | `webhook_received_total.labels(provider=...).inc()` — webhook handler (pós-HMAC) |

- `update_outbox_metrics()` — executa `SELECT COUNT(*) FROM outbox GROUP BY status`
  e atualiza as Gauges. Chamada ao fim de `run_drain_outbox`.

### Endpoint `/metrics`
- Rota em `app/web/server.py`: `GET /metrics` (sem auth, `include_in_schema=False`).
- Retorna `generate_latest()` com `Content-Type: text/plain; version=0.0.4`.
- **Em produção:** restringir ao range de IP do Prometheus no reverse-proxy/firewall.
  Não expor na internet.

## Como adicionar uma nova métrica

1. Declarar o objeto em `app/observability/metrics.py` (Counter/Gauge/Histogram).
2. Incrementar/observar no código de negócio relevante.
3. Para Gauges baseadas em DB: adicionar query em `update_outbox_metrics()` ou
   criar função separada chamada pelo job pertinente.

## Testes
- `tests/test_metrics.py` — endpoint 200, content-type, nomes presentes,
  `update_outbox_metrics` com DB real, incremento de Counter.

```bash
.venv/bin/pytest tests/test_metrics.py -v
```

## Armadilhas
- **prometheus_client usa registry global.** Múltiplos imports da mesma métrica
  em testes retornam o mesmo objeto — acumulado entre testes. Use `._value.get()`
  para ler o valor atual, não compare absolutos.
- **Gauges de outbox são atualizadas a cada 15s, não em tempo real.** Máximo 15s
  de defasagem — aceitável para alertas de operação.
- **Counter de webhooks conta apenas os que passaram HMAC.** Requisições não
  autenticadas rejeitadas antes do `.inc()` — isso é intencional.
