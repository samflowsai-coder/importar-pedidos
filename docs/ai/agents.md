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
