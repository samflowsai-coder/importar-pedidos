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

## Log de leituras (`logs/leituras.jsonl`)

Uma linha JSON por arquivo que o portal lê (upload, pasta/worker, lote, reimport,
`/api/process` legado) e uma linha `etapa="rejeitado"` por arquivo barrado antes do parse
(extensão, tamanho, não encontrado, guarda do original). **Sem tela:** o arquivo é baixado do
servidor e lido aqui. Spec: `docs/superpowers/specs/2026-10-05-log-de-leituras-design.md`.

- **Código:** `app/observability/leituras.py` (`registrar`, `rejeitado`, `escrever`,
  `sinais_suspeitos`, `resumo_fire_check`); `pipeline.process_detailed(file, diag)` preenche o
  `Diagnostico` (parser usado/pulados, cabeçalho candidato, avisos do validador); `process()`
  continua sendo o wrapper stateless.
- **Arquivo:** `<raiz do app>/logs/leituras.jsonl` (ancorado em `app/utils/logger.py::LOG_DIR`,
  não no CWD; o caminho absoluto sai no log de inicialização). Rotação 10 MB, 30 dias.
- **Nunca é portão:** falha ao gravar vira `logger.warning` e o fluxo segue. Sem texto livre
  da planilha, sem `env`/senha; do cabeçalho entra UMA linha de rótulos curtos.
- **Chaves de ligação:** `trace_id` (mintado ANTES do parse; o commit do mesmo preview
  reusa, então casa com `imports.trace_id`, `audit_log` e `pipeline.log`) e `arquivo.sha256`
  (casa com `imports.file_sha256` e com a cópia em `recebidos/`).
- **Como ler** (`jq`): filtro de trabalho `jq -c 'select(.suspeito.forte)'`
  (`forte` = `parser_generico`, `llm_usado`, `item_parece_rotulo`, `numero_parece_rotulo`,
  `validacao_reprovada` ou `cliente_igual_ao_ambiente`); `select(.suspeito.sinais|length>0)`
  traz também os informativos;
  um arquivo `jq -c 'select(.arquivo.sha256|startswith("ab12"))'`; só falhas de leitura
  `jq -c 'select(.resultado|IN("422","erro"))'`; Fire fora do ar
  `jq -c 'select(.fire_check.available==false)'`.
- **Como decidir a causa:** parser genérico/LLM em `parser.usado` + `extracao.cabecalho_candidato`
  = formato novo ou título de coluna diferente (leitura); `fire_check.available=false` =
  Fire fora do ar, não cadastro; `fire_check.cliente.match=false` com `available=true` =
  cliente não cadastrado pelo CNPJ; `arquivo.sha_ja_visto`/`pedido.pedido_ja_importado` =
  duplicidade; `etapa="rejeitado"` = nunca chegou ao parse.
- **`resultado`:** `retido`, `pendencia_throttle`, `skip_duplicate`, `importado`, `preview`,
  `erro`, `400`, `404`, `413`, `422`. `"500"` nunca é produzido (exceção genérica vira `"erro"`).
- **Sinais informativos esperados** (não são defeito): `numero_gerado` em NBA/Tennis
  Station/Pulmão; `sem_cliente` em Desmembramento/Tennis Station/Pulmão;
  `preco_ausente_em_todos` em Desmembramento.
- **Worker:** não roda no cliente Windows (só o `ui.py`), então linhas `origem=worker` só
  existem em docker.
- **Armadilhas:** `resultado="importado"` é marcado logo depois do parse em `_process_file`
  e `/api/process`, e no worker DEPOIS do insert; falha posterior do export/insert NÃO reescreve a linha (os eventos
  pós-leitura são a entrega 2, `docs/BACKLOG.md` 2.18). A exceção que o chamador engole (scan do
  worker) é capturada em `registrar.processar`, então entra na linha mesmo assim.

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
