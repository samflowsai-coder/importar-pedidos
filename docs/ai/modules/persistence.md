# Módulo: persistence (SQLite log)

## Responsabilidade
Log estruturado de execuções (arquivo, parser usado, status, timestamp) em SQLite local.

## Arquivos críticos
- `app/persistence/db.py` — conexão sqlite, schema, migrations.
- `app/persistence/repo.py` — repositório (insert log, query histórico).
- `tools/migrate_log_to_sqlite.py` — migração one-shot do log antigo.

## Testes
`tests/test_persistence_repo.py` — `.venv/bin/pytest tests/test_persistence_repo.py -v`

## Armadilhas
- Banco é local-only por design. Não promover sem mudar estratégia (multi-tenant é v5).
