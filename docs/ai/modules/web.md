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
- `GET /health` → `{"status":"ok"}` — health check, sem auth.
- `GET /metrics` → scrape Prometheus (text/plain, sem auth — restringir no
  reverse-proxy em produção). Atualizado por jobs a cada 15s (Gauges) e em
  tempo real (Counter/Histogram).
- `GET /api/config` → estado de envvars.
- `POST /api/process` → upload + parse + cache de preview.
- `GET /api/download?path=` → download xlsx (whitelisted, path traversal bloqueado).
- `GET /api/fs?path=` → listagem auxiliar.
- `GET /api/clientes/search?q=&limit=` → busca em `CADASTRO` (razão social ou
  CNPJ). Min 2 chars; clamp `limit` em [1, 50]. 503 se Fire não configurado.
  Usado pelo picker manual de cliente (CLIENT_NOT_FOUND recovery).
- `POST /api/imported/{id}/override-cliente` body `{cliente_codigo, reason?}` →
  aplica seleção manual ao pedido (sidecar em `imports.cliente_override_*`).
  Só permitido em `portal_status='parsed'`. Logs em `audit_log`
  (`cliente_override_selected`) com `user=None` (preparado para auth v5).

## Segurança (não relaxar)
- Whitelist de extensão: `.pdf`, `.xls`, `.xlsx`.
- Limite de upload: 50 MB.
- `/api/download` aceita SOMENTE `.xlsx` e bloqueia `..`.
- `POST /api/auth/login` — rate-limit 10 req/15 min/IP via token bucket SQLite.
  Retorna 429 + `Retry-After: 900` quando esgotado.
  Env `RATE_LIMIT_ENABLED=false` desativa (dev/test).

## Testes
- `tests/test_web_server.py`
- `tests/test_preview_cache.py`
- Comando: `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py -v`

## Armadilhas
- Não cachear bytes do arquivo original (vazamento de memória); só o `Order` parseado.
- Toda mudança de rota: atualizar este arquivo + `index.html` se afetar UI.
