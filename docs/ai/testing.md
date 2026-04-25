# Estratégia de testes

## Princípio
Rode o **menor conjunto de testes que prova a mudança**. Suite completa é gate de commit, não de iteração.

## Mapa módulo → suíte
| Módulo | Suíte |
|---|---|
| parsers | `tests/test_new_parsers.py`, `tests/test_generic_parser.py` |
| normalizers | `tests/test_normalizer.py` |
| persistence | `tests/test_persistence_repo.py` |
| web | `tests/test_web_server.py`, `tests/test_preview_cache.py` |

## Sem teste isolado (validação manual obrigatória)
- `erp/`, `exporters/`, `llm/`, `extractors/`, `validators/`, `pipeline.py`
- Validar com sample em `samples/` + suíte completa.

## Quando rodar suíte completa
- Antes de qualquer commit.
- Após refactor.
- Antes de abrir PR.

## Comando padrão
```bash
.venv/bin/pytest tests/ -v
```

## Fallback (sem venv)
```bash
python -m pytest tests/ -v
```

## Cobertura
Não há gate de cobertura hoje. Foco: cada parser tem ao menos 1 teste com sample real.
