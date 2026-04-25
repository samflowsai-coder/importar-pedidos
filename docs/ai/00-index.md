# 00 — Índice de Contexto (Roteador IA)

> **Regra:** este é o ÚNICO arquivo que sempre deve ser lido antes de qualquer task.
> Ele mapeia tarefa → módulo → arquivos a carregar → testes a rodar.
> Se sua task não cabe em nenhuma linha abaixo, leia `01-project-overview.md`.

## Mapa rápido: tarefa → módulo

| Se a task envolve... | Domínio | Leia |
|---|---|---|
| Adicionar/ajustar parser de cliente novo (PDF ou XLS) | `parsers` | `modules/parsers.md` |
| Bug em parser específico (Riachuelo, Centauro, Kolosh, etc.) | `parsers` | `modules/parsers.md` |
| Importação no Firebird, queries SQL, mapper de colunas | `erp` | `modules/erp.md` |
| Rotas FastAPI, preview, upload, download | `web` | `modules/web.md` |
| Log de execuções em SQLite, repositório de pedidos processados | `persistence` | `modules/persistence.md` |
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
| web | `tests/test_web_server.py`, `tests/test_preview_cache.py` | `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py -v` |
| Suite completa (antes de commit) | todos | `.venv/bin/pytest tests/ -v` |

> **erp / exporters / pipeline / llm não têm testes isolados hoje.** Para mudanças nesses domínios, validar manualmente com sample real + rodar suite completa.

## Helpers compartilhados (sempre considerar antes de criar novos)

- `app/parsers/base_parser.py` — `_find(text, pattern)`, `_parse_br_number(value)`
- `app/utils/logger.py` — logger loguru singleton
- `app/models/order.py` — `Order`, `OrderHeader`, `OrderItem`, `ERPRow`
- `app/config.py` — leitura de env vars

## Fluxos completos

Workflows passo-a-passo em `workflows.md`:
- Bug fix
- Feature (novo parser, nova rota, novo exporter)
- Refactor
- Investigação

## Visão de produto

`01-project-overview.md` — só leia se a task não cabe em nenhum domínio acima.
