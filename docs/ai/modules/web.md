# Módulo: web (FastAPI)

## Responsabilidade
Interface humana de upload → preview → commit. Uvicorn em `PORTAL_HOST:PORTAL_PORT`, default `127.0.0.1:3636` (`ui.py`).

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
- `POST /api/imported/{id}/export-xlsx` → gera XLSX do pedido `parsed` **sem** tocar Firebird (`require_user`). Mantém `portal_status='parsed'`. Retorna `{entry_id, output_files, portal_status}`. Usado quando `EXPORT_MODE='xlsx'`. **Também dispara `push_new_order` pro FlowPCP** (gated por `flowpcp_enabled` do ambiente; best-effort; o Flow deduplica por `externalId` — re-export não duplica; audita `flowpcp_push {ok}`).
- `POST /api/batch/export-xlsx` → versão lote do anterior (mesmo limite 1..100).
- `GET /api/download?path=` → download xlsx (whitelisted, path traversal bloqueado).
- `GET /api/imported/{id}/arquivo-original` → cópia exata do arquivo recebido, antes do
  parse (`require_user`). Serve só de dentro de `<APP_DATA_DIR>/recebidos/` (403 fora);
  404 amigável em pedido anterior à guarda. Nome do download = `source_filename`.
- `GET /api/fs?path=` → listagem de pastas (usado pelo browser de `/configuracoes/diretorios`).
- `GET /api/clientes/search?q=&limit=` → busca em `CADASTRO` (razão social ou
  CNPJ). Min 2 chars; clamp `limit` em [1, 50]. 503 se Fire não configurado.
  Requer auth (`require_user`). Usado pelo picker manual de cliente
  (CLIENT_NOT_FOUND recovery).
- `POST /api/imported/{id}/override-cliente` body `{cliente_codigo, reason?}` →
  aplica seleção manual ao pedido (sidecar em `imports.cliente_override_*`).
  Requer auth. Só permitido em `portal_status='parsed'`. Logs em `audit_log`
  (`cliente_override_selected`) com `user_email`/`user_id` do autenticado.
- `POST /api/imported/{id}/ack-sem-preco` → operador confirma itens sem preço cadastrado no Fire (`require_user`).
  Body vazio. Pre: `portal_status='parsed'`. Re-roda check, persiste lista
  em `imports.sem_preco_ack_*`, audit `sem_preco_acknowledged`. 503 se Fire offline.
- `GET /api/produtos/search?q=&desc=&code=&ean_item=&limit=` → busca na cópia
  local do catálogo (`catalogo_fire`, zero Firebird — funciona offline). Requer
  auth; clamp `limit` em [1, 50]. `q` é **opcional**: com `q` ≥ 2 chars popula
  `results` (busca por nome/código/EAN). As dicas do item do pedido (`desc`,
  `code`, `ean_item`) — quando ao menos uma vier — populam `suggestions` (top 5
  ranqueados por `app/erp/product_ranking.rank_candidates`: EAN parcial > Jaccard
  descrição×nome > código contido). O picker de de-para abre chamando só com as
  dicas (sem `q`) pra já mostrar candidatos. 400 só quando `q` < 2 **e** sem
  dicas. Itens de `results`/`suggestions`: `{fire_produto_id, fire_codigo,
  fire_ean, fire_nome, score?}`.
- `POST /api/imported/{id}/vincular-produto` body `{item_index, fire_produto_id}`
  → grava o vínculo de-para (código e/ou EAN do item → produto do Fire) em
  `produto_depara`, audita (`produto_vinculo_criado`) e re-roda `check_order`.
  Só permitido em `portal_status='parsed'`. 422 se produto não existir no
  catálogo local ou item sem código nem EAN.
- `DELETE /api/produtos/depara/{depara_id}` → desfaz um vínculo. Log direto
  via `logger.info` (não `audit_log` — `import_id` é `NOT NULL` com FK pra
  `imports`, e o undo não tem um import associado).
- Guards de preço em `_send_one_to_fire` / `_export_one_xlsx`: re-roda
  `check_order` + `is_blocking`; bloqueia 409 com audit `send_to_fire_blocked` /
  `xlsx_export_blocked` quando há mismatch / no_order_price / no_price_unacked.
  Fire offline = best-effort, segue.
- **Ponte FlowPCP (Modelo B/OVERLAY):** após `SEND_TO_FIRE_SUCCEEDED` (dentro do
  `with_trace_id`), `_send_one_to_fire` chama `push_new_order(order, import_id,
  slug)` (`app/integrations/flowpcp/hook.py`) — notifica o FlowPCP do pedido novo
  em paralelo ao Fire. Gated por `flowpcp_config_for_slug(slug)` (config
  per-ambiente em `environments`, `flowpcp_enabled=1`); sem env ativo → no-op.
  Best-effort: erro nunca derruba o send-to-fire (falha vira outbox `target=flowpcp`
  + retry no worker). Vale para single e batch (ambos passam por `_send_one_to_fire`).
  CLI `main.py` fica fora (batch legado, sem contexto multi-ambiente).
  - **Push é enqueue-only (sem HTTP no request path):** `push_new_order` →
    `FlowPCPExporter.enqueue` **enfileira** direto no outbox (`target=flowpcp`) e
    retorna; o `drain_outbox` entrega e faz retry. Não há mais tentativa HTTP
    inline (antes esperava até 30s no timeout do Flow, segurando a UI). Idempotente
    por `send-{import_id}` (`OutboxDuplicateError` = no-op).
  - **Identidade de-para no XLS (e no push):** antes de `ERPExporter().export`,
    `_export_one_xlsx` chama `app/erp/depara_apply.apply(order, conn)` que reescreve
    o item vinculado com a identidade do Fire. Como o `push_new_order` compartilha o
    mesmo `order`, o FlowPCP também recebe a identidade Fire dos itens vinculados
    (desejável: o catálogo do Flow é Fire-synced). Só afeta itens com vínculo.

## Segurança (não relaxar)
- Whitelist de extensão: `.pdf`, `.xls`, `.xlsx`.
- Limite de upload: 50 MB.
- `/api/download` aceita SOMENTE `.xlsx` e bloqueia `..`.
- `POST /api/auth/login` — rate-limit 10 req/15 min/IP via token bucket SQLite.
  Retorna 429 + `Retry-After: 900` quando esgotado.
  Env `RATE_LIMIT_ENABLED=false` desativa (dev/test).

## Testes
- `tests/test_web_server.py` — inclui o push FlowPCP no send-to-fire
  (`test_send_to_fire_pushes_to_flowpcp_for_env_with_slug` / `_skips_flowpcp_without_env`)
  e as rotas de de-para de produto (`test_produtos_search_local`,
  `test_vincular_produto_persiste`, `test_vincular_rejects_wrong_status`,
  `test_delete_depara_desfaz`).
- `tests/test_flowpcp_hook.py` — `push_new_order` (gating MM + best-effort).
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

## Arquivo original: guarda de 100% do que entra

Todo recebimento — `POST /api/preview` (upload), `POST /api/preview-pending` (pasta
vigiada), `POST /api/import` (lote) e o legado `POST /api/process` — chama
`_guardar_original(raw, nome)` **antes do parse**. A cópia vai para
`<APP_DATA_DIR>/recebidos/<slug>/<AAAA>/<MM>/<AAAAMMDD-HHMMSS>_<sha12>_<nome>.<ext>`
(`app/ingestion/arquivo_recebido.py`), nunca é sobrescrita e a retenção não a toca.
Parse que falha (422), preview descartado ou expirado: a cópia já existe.

- **Falha na gravação bloqueia a operação** (HTTP 500 com mensagem clara). A garantia
  pedida é 100%; best-effort silencioso viraria 97% sem ninguém saber.
- O caminho + sha viajam no `PreviewEntry` (`original_path`, `file_sha256`) e o
  `commit` grava em `imports.original_path` / `imports.file_sha256`.
- O `move` para `<watch_dir>/Pedidos importados` continua igual — é a cópia do cliente;
  a nossa é a segunda, imutável.
- `GET /api/imported/{id}/preview` devolve `arquivo_original: {disponivel, nome}`; o
  modal mostra **Baixar arquivo original** quando disponível.
- Motivação: caso AF127/AF017 (H2S4, 27/07/2026) — dois arquivos com o mesmo nome no
  mesmo dia, o segundo já errado, e nenhuma cópia nossa pra provar de onde veio.

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
- `_export_one_xlsx` re-roda `check_order` SEM passar `request_env` (caminho
  legado). Em deploy multi-ambiente isso usa env vars `FB_*`. Follow-up:
  passar env do request quando essa rota também adotar `getattr(request.state, "environment")`.
