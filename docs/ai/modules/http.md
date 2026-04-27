# Módulo: http (outbound HTTP layer)

## Responsabilidade
Único cliente para chamadas HTTP de saída. Wraps `httpx` + `tenacity` com:
- Retry declarativo via `RetryPolicy` (3 sabores: read-only, idempotent
  POST, LLM call).
- `X-Trace-Id` injetado automaticamente do contextvar
  `app.observability.trace`.
- Logging estruturado por tentativa (método, URL, status, duração ms).
- Connection pooling (1 `httpx.Client` por instância).

## Arquivos críticos
- `app/http/client.py` — `OutboundClient`, `HttpError`,
  `_RetryableHttpStatusError` (interno). API: `post_json(url, json,
  headers, idempotency_key)`, `get(url, params, headers)`,
  `raise_for_status(response)` (helper estático).
- `app/http/policies.py` — `RetryPolicy` (dataclass) e três fábricas:
  - `read_only_policy()` — 3 attempts, 5xx + 429.
  - `idempotent_post_policy()` — 3 attempts, 5xx (incl. 500).
  - `llm_call_policy()` — **2 attempts**, só 502/503/504. Pessimista por
    causa de cobrança do modelo.

## Como usar

```python
from app.http import OutboundClient, idempotent_post_policy
from app.observability.trace import with_trace_id

client = OutboundClient(
    base_url="https://api.gestor.example.com",
    timeout=30.0,
    retry_policy=idempotent_post_policy(),
    default_headers={"Authorization": f"Bearer {api_key}"},
)

with with_trace_id(entry["trace_id"]):
    resp = client.post_json(
        "/orders",
        json={"order": {...}},
        idempotency_key=outbox_row.idempotency_key,
    )
    if not resp.is_success:
        # decide what to do — caller is in charge of error semantics
        ...
```

Para teste unit: passar `transport=httpx.MockTransport(handler)` no
construtor do `OutboundClient`. Ver `tests/test_outbound_client.py`.

## Decisões de design (não mudar sem razão)

- **Caller-owns-error-semantics:** `_send_with_retry` retorna a `Response`
  final mesmo após retries esgotados em status retryable. Quem chama
  decide se vira erro ou não. `raise_for_status` é opt-in.
- **Sem retry em 4xx, em qualquer policy.** 4xx = bug de cliente; retry
  não vai consertar e na maioria das APIs quem cobra também cobra 4xx.
- **`llm_call_policy` ≠ `idempotent_post_policy`.** LLM nunca retentar em
  500 nem em 429. Modelo pode ter cobrado e duplicaríamos custo. Outras
  POSTs idempotentes podem.
- **`Idempotency-Key` é responsabilidade do caller.** O cliente só
  encaminha o header se passado. Para outbox (Fase 3) a key vem da row.

## Testes
`.venv/bin/pytest tests/test_outbound_client.py -v` — 15 testes cobrindo
trace_id, idempotency, retry de 5xx/conexão, recusa de 4xx, política
LLM, wrapper OpenRouter (auth bearer, propagação de trace_id).

## Armadilhas
- `httpx.MockTransport` precisa ser passado como kwarg `transport=` no
  `OutboundClient.__init__`, não no `httpx.Client` diretamente.
- A `RetryPolicy` é frozen dataclass; clonar com `dataclasses.replace` se
  precisar customizar para um call específico.
- Loguru filter de trace_id (`app/utils/logger.py`) carimba a string `-`
  quando não há contexto. Não usar para correlação se vir `trace=-`.
