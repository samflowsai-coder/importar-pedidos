# Módulo: web (FastAPI)

## Responsabilidade
Interface humana de upload → preview → commit. Uvicorn em `:8000`.

## Arquivos críticos
- `app/web/server.py` — rotas FastAPI.
- `app/web/preview_cache.py` — cache em memória de pré-visualizações.
- `app/web/static/index.html` — frontend (vanilla, dark-first).
- `ui.py` — entrypoint `uvicorn`.

## Rotas
- `GET /` → SPA estática.
- `GET /api/config` → estado de envvars.
- `POST /api/process` → upload + parse + cache de preview.
- `GET /api/download?path=` → download xlsx (whitelisted, path traversal bloqueado).
- `GET /api/fs?path=` → listagem auxiliar.

## Segurança (não relaxar)
- Whitelist de extensão: `.pdf`, `.xls`, `.xlsx`.
- Limite de upload: 50 MB.
- `/api/download` aceita SOMENTE `.xlsx` e bloqueia `..`.

## Testes
- `tests/test_web_server.py`
- `tests/test_preview_cache.py`
- Comando: `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py -v`

## Armadilhas
- Não cachear bytes do arquivo original (vazamento de memória); só o `Order` parseado.
- Toda mudança de rota: atualizar este arquivo + `index.html` se afetar UI.
