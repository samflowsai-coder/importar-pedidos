# Workflows (passo-a-passo)

## Bug fix
1. Reproduzir com sample real.
2. Adicionar teste falhando.
3. Corrigir (diff mínimo).
4. Teste do módulo verde → suite completa.
5. Atualizar "Armadilhas" no `modules/<dominio>.md` se for útil pra próximo agente.

## Feature: novo parser
1. Coletar 1–3 samples reais → `samples/`.
2. Identificar string-âncora estável para `can_parse` (header, CNPJ fixo, marcador único).
3. Criar `app/parsers/<nome>_parser.py` herdando `BaseParser`.
4. Implementar `can_parse` (barato) e `parse` (com `_find`/`_parse_br_number`).
5. Registrar em `app/pipeline.py` ANTES do `GenericParser`.
6. Teste em `tests/test_new_parsers.py`.
7. Rodar `.venv/bin/pytest tests/test_new_parsers.py -v`.
8. Atualizar `docs/ai/modules/parsers.md` (lista de parsers).

## Feature: nova rota web
1. Escolher o arquivo certo: `app/web/server.py` (fluxo principal) ou o router do
   assunto — `routes_environments.py` (`/api/admin/environments`), `routes_update.py`
   (`/api/admin/update`), `routes_env_select.py`, `webhooks.py` (`/api/webhooks`).
   Router novo precisa de `app.include_router(...)` em `server.py`.
2. Proteger: `require_user` ou `require_admin`. Rota nova nasce autenticada — abrir
   depois é decisão explícita, não default.
3. Validar input (whitelist de extensão, tamanho — upload é **50MB**, path traversal).
4. Atualizar o HTML em `app/web/static/` se afeta UI.
5. Teste em `tests/test_web_server.py` (ou o de teste do router).
6. Atualizar `docs/ai/modules/web.md` (seção Rotas).

## Feature: novo exporter (ex: Firebird novo cliente)
1. Rodar `python tools/explore_firebird.py --database empresa_COPIA.fdb > schema_report.txt` (NUNCA em produção).
2. Identificar tabelas no report.
3. Atualizar `app/erp/queries.py`.
4. Atualizar `app/erp/mapper.py`.
5. Implementar em `app/exporters/firebird_exporter.py`.
6. Validar com `EXPORT_MODE=both` em sample real.

## Refactor
1. Suite verde como baseline.
2. Escopo declarado, não-escopo declarado.
3. Diff puro (sem mudança de comportamento).
4. Suite verde ao final.

## Investigação
1. Reproduzir com o sample. **Não existe `LOG_LEVEL`**: o console sai em `INFO` e o
   arquivo em `logs/` já grava `DEBUG` — o rastro completo está lá (`app/utils/logger.py`).
2. Mapear o caminho no pipeline; `trace_id` costura o request inteiro
   (ver `modules/observability.md`).
3. Conferir se o achado já está catalogado em `docs/BACKLOG.md` antes de tratar como novo.
4. Documentar no PR description, não em arquivo novo — a menos que vire decisão
   arquitetural, que aí vai pra `01-project-overview.md`.
