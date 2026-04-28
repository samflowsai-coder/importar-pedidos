# Módulo: web (FastAPI)

## Responsabilidade
Interface humana de upload → preview → commit. Uvicorn em `:8000`.

## Arquivos críticos
- `app/web/server.py` — rotas FastAPI.
- `app/web/preview_cache.py` — cache em memória de pré-visualizações.
- `app/web/static/index.html` — Pedidos (vanilla JS, dark-first).
- `app/web/static/admin-usuarios.html` — Configurações > Usuários.
- `app/web/static/config-banco.html` — Configurações > Banco de dados (Firebird).
- `app/web/static/config-diretorios.html` — Configurações > Diretórios (substitui modal antigo).
- `app/web/static/css/tokens.css`, `shell.css`, `js/shell.js` — app shell compartilhado.
- `ui.py` — entrypoint `uvicorn`.

## App shell (sidebar persistente)
Todas as páginas autenticadas (Pedidos, Configurações/*) carregam:
```html
<link rel="stylesheet" href="/static/css/tokens.css?v=1">
<link rel="stylesheet" href="/static/css/shell.css?v=1">
<script src="/static/js/shell.js?v=1" defer></script>
<div id="app-shell"></div>
```
`shell.js` injeta sidebar (Pedidos + Configurações com sub-itens Banco/Diretórios/Usuários) e topbar (status Firebird, user pill, logout). O grupo Configurações é admin-only. Páginas públicas (`login.html`, `invite.html`) carregam apenas `tokens.css`.

API pública para páginas-filho:
- `window.appShell.showError(msg, traceId)` — toast com botão "copiar trace_id".
- `window.appShell.showSuccess(msg)`, `showInfo(msg)`.
- `window.appShell.refreshFb()` — força refetch do `/api/config` para atualizar o pill Firebird.
- `window.__shellUser` — cache do `/api/auth/me` (use em vez de chamar de novo).

## Rotas
- `GET /` → `index.html` (Pedidos).
- `GET /configuracoes/banco` → `config-banco.html` (admin gating client-side; writes admin-only via API).
- `GET /configuracoes/diretorios` → `config-diretorios.html`.
- `GET /configuracoes/usuarios` → `admin-usuarios.html`.
- `GET /admin/usuarios` → 301 → `/configuracoes/usuarios` (legado).
- `GET /health` → `{"status":"ok"}` — sem auth.
- `GET /metrics` → scrape Prometheus (text/plain, sem auth — restringir no
  reverse-proxy em produção). Atualizado por jobs a cada 15s (Gauges) e em
  tempo real (Counter/Histogram).
- `GET /api/config` → `{watchDir, outputDir, exportMode, firebirdConfigured}`.
- `POST /api/config` → atualiza diretórios e modo (`require_user`).
- `GET /api/firebird/config` → `{path, host, port, user, charset, configured, passwordSet}` — **nunca** retorna senha (`require_user`).
- `POST /api/firebird/config` → salva config + chama `apply_to_env` (`require_admin`). Body: `{path, host, port, user, charset, password?}`. Senha omitida = mantém atual; vazia = limpa.
- `POST /api/firebird/test` → testa conexão com config salva ou payload ad-hoc (`require_admin`). Retorna `{ok: bool, error?, traceId}` (`current_trace_id()` injetado).
- `POST /api/process` → upload + parse + cache de preview.
- `POST /api/imported/{id}/export-xlsx` → gera XLSX do pedido `parsed` **sem** tocar Firebird (`require_user`). Mantém `portal_status='parsed'`. Retorna `{entry_id, output_files, portal_status}`. Usado quando `EXPORT_MODE='xlsx'`.
- `POST /api/batch/export-xlsx` → versão lote do anterior (mesmo limite 1..100).
- `GET /api/download?path=` → download xlsx (whitelisted, path traversal bloqueado).
- `GET /api/fs?path=` → listagem de pastas (usado pelo browser de `/configuracoes/diretorios`).
- `GET /api/clientes/search?q=&limit=` → busca em `CADASTRO` (razão social ou
  CNPJ). Min 2 chars; clamp `limit` em [1, 50]. 503 se Fire não configurado.
  Requer auth (`require_user`). Usado pelo picker manual de cliente
  (CLIENT_NOT_FOUND recovery).
- `POST /api/imported/{id}/override-cliente` body `{cliente_codigo, reason?}` →
  aplica seleção manual ao pedido (sidecar em `imports.cliente_override_*`).
  Requer auth. Só permitido em `portal_status='parsed'`. Logs em `audit_log`
  (`cliente_override_selected`) com `user_email`/`user_id` do autenticado.

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
- `tests/test_firebird_config_api.py` — endpoints `/api/firebird/*`, redirect legacy, gating por role.
- Comando: `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py tests/test_firebird_config_api.py -v`

## Reatividade de config (exportMode)
O botão de ação principal (`#pvCommitBtn` no preview e `#batchSendBtn` no log)
é dirigido por `cfg.exportMode` — fonte: `GET /api/config`. Mapeamento:
`xlsx` → "Gerar XLS" (chama `/api/imported/{id}/export-xlsx`); `db` → "Cadastrar
no Fire" (`/send-to-fire`); `both` → "Cadastrar no Fire + XLS" (`/send-to-fire`,
backend gera XLSX adicionalmente).

Quando o modo é alterado em `/configuracoes/diretorios`, a aba Pedidos atualiza
label/handler **sem reload** via `BroadcastChannel('app-config')` com payload
`{type:'config-changed', exportMode}`. Fallback p/ navegadores sem
BroadcastChannel: chave `app:config:bumped` em `localStorage` + `storage`
event listener.

## Armadilhas
- Não cachear bytes do arquivo original (vazamento de memória); só o `Order` parseado.
- Toda mudança de rota: atualizar este arquivo + a página relevante.
- `app_state.db` vive em `<repo_root>/data/` (override: `APP_DATA_DIR`). NÃO depende de `watch_dir` — mudar diretórios via `POST /api/config` não move sessões nem dados operacionais.
- Sidebar gating é client-side (escondemos o item para não-admin no shell.js); a fonte de verdade
  é o backend — `require_admin` em todos os writes. Nunca confie só na UI para gating.
- Páginas em `/configuracoes/*` carregam para qualquer usuário logado (estáticas). Acesso real é
  feito pelas APIs que cada página consome — daí ser admin-only no `POST` do Firebird.
- Assets estáticos não têm hash. Use `?v=1` em `<link>`/`<script>` ao mudar tokens/shell;
  caso contrário, hard-reload no browser.
