# Agentes por domínio

> Cada agente = **contexto mínimo do domínio + skill base**. Use o agente `Explore` ou `general-purpose` do Claude Code passando o briefing abaixo no `prompt`.

## parser-agent
**Briefing pra colar no prompt do agente:**
> Você está atuando no domínio `parsers` do Portal de Pedidos. Leia primeiro `docs/ai/modules/parsers.md`. Sempre herde de `BaseParser`, reuse `_find` e `_parse_br_number`. Registre o parser em `app/pipeline.py` antes do `GenericParser`. Adicione sample em `samples/` e teste em `tests/test_new_parsers.py`. Rode `.venv/bin/pytest tests/test_new_parsers.py -v` ao final.

## erp-agent
> Domínio `erp` (Firebird/Fire Sistemas). Leia `docs/ai/modules/erp.md`. Padrões reais: `STATUS='PEDIDO'`, flags `'Sim'/'Nao'`, charset `WIN1252`, idempotência por `PEDIDO_CLIENTE+CLIENTE`. Nunca rode `tools/explore_firebird.py` em produção. Validação manual com `.fdb` de cópia.

## web-agent
> Domínio `web` (FastAPI). Leia `docs/ai/modules/web.md`. Não relaxe segurança: whitelist de extensão, limite 50MB, path traversal bloqueado. Testes em `tests/test_web_server.py` e `tests/test_preview_cache.py`.

## llm-agent
> Domínio `llm`. Leia `docs/ai/modules/llm.md`. Provider é OpenRouter via OpenAI SDK (não Anthropic direto). Saída do LLM passa pelo `OrderValidator` igual aos parsers determinísticos.

## persistence-agent
> Domínio `persistence` (SQLite). Leia `docs/ai/modules/persistence.md`. Migrations idempotentes em `db.py`. Teste: `tests/test_persistence_repo.py`.

## exporter-agent
> Domínio `exporters`. Leia `docs/ai/modules/exporters.md`. Split por loja é responsabilidade do exporter, não do parser.

## pipeline-agent
> Domínio `pipeline`. Leia `docs/ai/modules/pipeline.md`. Mudar ordem da cascata exige justificativa explícita.

## environments-agent
> Domínio `environments` (multi-empresa). Leia `docs/ai/modules/environments.md`. Toda query de dado de pedido roda no SQLite DO AMBIENTE (`app_state_<slug>.db`), nunca no `app_shared.db` — vazar dado de uma empresa na listagem de outra é o pior bug possível aqui. Senha de Firebird e token são cifrados via `app/security/secret_store.py`.

## auth-agent
> Domínio `auth`. Leia `docs/ai/modules/auth.md`. Rota nova nasce protegida (`require_user`/`require_admin`); abrir é decisão explícita. Senha com bcrypt, sessão em cookie. Testes: `tests/test_auth_routes.py`, `tests/test_sessions_repo.py`, `tests/test_users_repo.py`, `tests/test_passwords.py`.

## security-agent
> Domínio `security`. Leia `docs/ai/modules/security.md`. HMAC com comparação em tempo constante, replay protection, rate-limit por token bucket, segredo sempre via `secret_store` (nunca texto plano no banco). Testes: `tests/test_hmac_verify.py`, `tests/test_rate_limit.py`, `tests/test_secrets.py`.

## worker-agent
> Domínio `worker` (APScheduler). Leia `docs/ai/modules/worker.md`. Job novo tem que ser idempotente e não pode derrubar os outros: um ambiente com erro nunca aborta a varredura dos demais. Testes: `tests/test_worker_drain_outbox.py`, `tests/test_worker_poll_fire.py`, `tests/test_retention.py`.

## gestor-agent
> Domínio `gestor`. Leia `docs/ai/modules/gestor.md`. Saída sempre pelo outbox + `drain_outbox`, nunca HTTP no request path. Webhook inbound exige HMAC + idempotência. Testes: `tests/test_gestor_integration.py`, `tests/test_webhooks.py`, `tests/test_outbox_repo.py`.

## flowpcp-agent
> Domínio `flowpcp`. Leia `docs/ai/modules/flowpcp.md`. Tudo é best-effort e por ambiente: o pedido já entrou no Fire/XLS quando o push acontece, então falha vira outbox/retry e nunca derruba o fluxo. Gate desligado (`catalogo_push`/`clientes_push`) é caminho normal, não erro. Testes: `tests/test_flowpcp_*.py`.

## updates-agent
> Domínio `updates`. Leia `docs/ai/modules/updates.md`. Nunca afrouxe a allowlist do pacote nem a denylist de segredo/dado (`.env`, `.secret.key`, `*.db`, `*.fdb`). Concorrência exige os DOIS guards (`is_locked` + status em andamento). Testes: `tests/test_update_package.py`, `tests/test_update_routes.py`, `tests/test_update_state.py`.

## state-agent
> Domínio `state`. Leia `docs/ai/modules/state.md`. Transição nova entra na máquina, não em `if` espalhado pela rota; todo evento carrega `trace_id`. Teste: `tests/test_state_machine.py`.
