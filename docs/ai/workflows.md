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
1. Adicionar handler em `app/web/server.py`.
2. Validar input (whitelist, tamanho, path).
3. Atualizar `app/web/static/index.html` se afeta UI.
4. Teste em `tests/test_web_server.py`.
5. Atualizar `docs/ai/modules/web.md` (seção Rotas).

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
1. `LOG_LEVEL=DEBUG` + sample que reproduz.
2. Mapear caminho no pipeline.
3. Documentar achados no PR description, não em arquivo novo (a menos que vire decisão arquitetural).
