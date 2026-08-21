# 00 — Índice de Contexto (Roteador IA)

> **Regra:** este é o ÚNICO arquivo que sempre deve ser lido antes de qualquer task.
> Ele mapeia tarefa → módulo → arquivos a carregar → testes a rodar.
> Se sua task não cabe em nenhuma linha abaixo, leia `01-project-overview.md`.

## Mapa rápido: tarefa → módulo

| Se a task envolve... | Domínio | Leia |
|---|---|---|
| Adicionar/editar ambiente (multi-empresa MM/Nasmar/...) | `environments` | `modules/environments.md` |
| De-para de cliente intercompany (pedido no nome da revenda) | `erp` + `environments` | `modules/erp.md`, `modules/environments.md` |
| Adicionar/ajustar parser de cliente novo (PDF ou XLS) | `parsers` | `modules/parsers.md` |
| Bug em parser específico (Riachuelo, Centauro, Kolosh, etc.) | `parsers` | `modules/parsers.md` |
| Importação no Firebird, queries SQL, mapper de colunas | `erp` | `modules/erp.md` |
| Configurar caminho/host/credenciais do Firebird via UI (`/configuracoes/banco`) | `erp` + `security` | `modules/erp.md`, `modules/security.md` (seção secret_store) |
| Rotas FastAPI, preview, upload, download, app shell | `web` | `modules/web.md` |
| Log de execuções em SQLite, repositório de pedidos processados | `persistence` | `modules/persistence.md` |
| Mudar status de pedido, adicionar evento ao ciclo de vida, propagar trace_id | `state` | `modules/state.md` |
| Chamada HTTP de saída (Gestor, OpenRouter, qualquer API externa) | `http` | `modules/http.md` |
| Integração Gestor de Produção (outbox, mapper, rota post-to-gestor) | `gestor` | `modules/gestor.md` |
| Integração FlowPCP (push de pedido, decisões→Fire, catálogo, clientes) | `flowpcp` | `modules/flowpcp.md` |
| Auto-update do portal (pacote .zip, `/admin/atualizacao`, updater) | `updates` | `modules/updates.md` |
| Webhooks inbound, HMAC, replay protection, idempotency | `security` + `gestor` | `modules/security.md`, `modules/gestor.md` (seção webhooks) |
| Login, sessão, cookie, proteger rota nova com auth, criar usuário via CLI | `auth` | `modules/auth.md` |
| Rate-limit, token bucket, secrets abstraction, bcrypt, HMAC | `security` | `modules/security.md` |
| Métricas Prometheus (/metrics), trace_id, observabilidade | `observability` | `modules/observability.md` |
| Worker APScheduler, drain_outbox, poll_fire, retention, backup | `worker` | `modules/worker.md` |
| LLM fallback (OpenRouter / Gemini / Haiku) | `llm` | `modules/llm.md` |
| Geração de XLSX, split por loja, naming de arquivo | `exporters` | `modules/exporters.md` |
| Orquestração (ordem dos parsers, fluxo geral) | `pipeline` | `modules/pipeline.md` |
| Modelos Pydantic (Order, OrderItem, ERPRow) | `models` | `modules/models.md` |
| Normalização (datas, case, CNPJ) | `normalizers` | `modules/normalizers.md` |
| Validação (campos obrigatórios, qty>0) | `validators` | `modules/validators.md` |
| Extração de texto/tabela de PDF ou XLS | `extractors` | `modules/extractors.md` |

## Mapa rápido: domínio → testes

| Domínio | Arquivo de teste | Comando |
|---|---|---|
| parsers | `tests/test_new_parsers.py`, `tests/test_generic_parser.py` | `.venv/bin/pytest tests/test_new_parsers.py -v` |
| normalizers | `tests/test_normalizer.py` | `.venv/bin/pytest tests/test_normalizer.py -v` |
| persistence | `tests/test_persistence_repo.py` | `.venv/bin/pytest tests/test_persistence_repo.py -v` |
| state | `tests/test_state_machine.py` | `.venv/bin/pytest tests/test_state_machine.py -v` |
| http | `tests/test_outbound_client.py` | `.venv/bin/pytest tests/test_outbound_client.py -v` |
| gestor | `tests/test_gestor_integration.py`, `tests/test_outbox_repo.py` | `.venv/bin/pytest tests/test_gestor_integration.py tests/test_outbox_repo.py -v` |
| security | `tests/test_hmac_verify.py` | `.venv/bin/pytest tests/test_hmac_verify.py -v` |
| webhooks | `tests/test_webhooks.py`, `tests/test_idempotency_repo.py` | `.venv/bin/pytest tests/test_webhooks.py tests/test_idempotency_repo.py -v` |
| auth | `tests/test_passwords.py`, `tests/test_users_repo.py`, `tests/test_sessions_repo.py`, `tests/test_auth_routes.py` | `.venv/bin/pytest tests/test_passwords.py tests/test_users_repo.py tests/test_sessions_repo.py tests/test_auth_routes.py -v` |
| security (rate-limit, secrets) | `tests/test_rate_limit.py`, `tests/test_secrets.py`, `tests/test_hmac_verify.py` | `.venv/bin/pytest tests/test_rate_limit.py tests/test_secrets.py tests/test_hmac_verify.py -v` |
| observability | `tests/test_metrics.py` | `.venv/bin/pytest tests/test_metrics.py -v` |
| flowpcp | `tests/test_flowpcp_hook.py`, `tests/test_flowpcp_poll.py`, `tests/test_flowpcp_intercompany.py`, `tests/test_catalogo_sync.py`, `tests/test_clientes_sync.py` | `.venv/bin/pytest tests/test_flowpcp_hook.py tests/test_flowpcp_poll.py tests/test_flowpcp_intercompany.py -v` |
| updates | `tests/test_update_package.py`, `tests/test_update_routes.py`, `tests/test_update_state.py` | `.venv/bin/pytest tests/test_update_package.py tests/test_update_routes.py tests/test_update_state.py -v` |
| erp | `tests/test_product_check.py`, `tests/test_depara_cliente.py`, `tests/test_depara_apply.py`, `tests/test_smoke_erp_mapper.py`, `tests/test_firebird_*.py` | `.venv/bin/pytest tests/test_product_check.py tests/test_depara_cliente.py tests/test_depara_apply.py -v` |
| exporters | `tests/test_exporter_split.py`, `tests/test_smoke_exporter.py`, `tests/test_firebird_exporter_override.py` | `.venv/bin/pytest tests/test_exporter_split.py tests/test_smoke_exporter.py -v` |
| pipeline | `tests/test_smoke_pipeline.py` | `.venv/bin/pytest tests/test_smoke_pipeline.py -v` |
| worker | `tests/test_worker_drain_outbox.py`, `tests/test_worker_poll_fire.py`, `tests/test_retention.py` | `.venv/bin/pytest tests/test_worker_drain_outbox.py tests/test_worker_poll_fire.py tests/test_retention.py -v` |
| llm | `tests/test_smoke_llm_fallback.py`, `tests/test_outbound_client.py` | `.venv/bin/pytest tests/test_smoke_llm_fallback.py tests/test_outbound_client.py -v` |
| web | `tests/test_web_server.py`, `tests/test_preview_cache.py` | `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py -v` |
| Suite completa (antes de commit) | todos | `.venv/bin/pytest tests/ -v` |

> **Suíte completa: 877 testes em 84 arquivos.** `erp`, `exporters` e `pipeline` hoje TÊM
> teste (ver linhas acima), mas nenhum toca Firebird de verdade — mudança em SQL/mapper
> ainda pede validação manual com `.fdb` de **cópia** e sample real.

## Helpers compartilhados (sempre considerar antes de criar novos)

- `app/parsers/base_parser.py` — `_find(text, pattern)`, `_parse_br_number(value)`
- `app/utils/logger.py` — logger loguru singleton
- `app/models/order.py` — `Order`, `OrderHeader`, `OrderItem`, `ERPRow`
- `app/config.py` — leitura de env vars (diretórios, modo de exportação)
- `app/persistence/environments_repo.py` — CRUD multi-ambiente (substitui `firebird_config.py` no fluxo multi-empresa); senha cifrada via `app/security/secret_store.py`
- `app/persistence/router.py` — `shared_connect()` / `env_connect(slug)` para roteamento de DB
- `app/persistence/context.py` — ContextVar `active_env`; `db.connect()` lê daqui
- `app/firebird_config.py` — **legado**: config singleton para deploy single-empresa (mantido para compat)
- `app/web/static/css/tokens.css`, `shell.css`, `app/web/static/js/shell.js` — app shell compartilhado entre páginas autenticadas
- `app/http/client.py` — cliente HTTP de saída (retry/timeout); toda chamada externa passa por aqui
- `app/security/secret_store.py` — cifra/decifra segredo persistido (Fernet). Nenhum segredo em texto plano no banco

## Fluxos completos

Workflows passo-a-passo em `workflows.md`:
- Bug fix
- Feature (novo parser, nova rota, novo exporter)
- Refactor
- Investigação

## Visão de produto

`01-project-overview.md` — só leia se a task não cabe em nenhum domínio acima.

## Escopo aberto

`../BACKLOG.md` — bugs conhecidos, dívida e o que está bloqueado em terceiros.
Antes de "consertar" algo que parece errado, confira se já está catalogado lá.

## Histórico

`../history/` (PRD e ARCHITECTURE da v1) e `../superpowers/` (specs e planos) são
**registro congelado**, não fonte de verdade. Nunca cite um deles como estado atual.
