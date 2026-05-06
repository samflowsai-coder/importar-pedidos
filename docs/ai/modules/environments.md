# environments — Multi-empresa (MM, Nasmar, e além)

## O que é

O Portal opera N empresas em paralelo. Cada **ambiente** (`environment`) tem:
- Pastas próprias (`watch_dir`, `output_dir`)
- Banco Firebird próprio (`fb_path`, `fb_host`, `fb_port`, `fb_user`, senha cifrada)
- Slug imutável usado como chave: `mm`, `nasmar`, etc.

Pedidos de cada empresa vivem em SQLite separado (`app_state_<slug>.db`).
Auth, sessões, idempotência e o registry de ambientes vivem no
SQLite compartilhado (`app_shared.db`).

## Arquivos críticos

- [app/persistence/environments_repo.py](../../../app/persistence/environments_repo.py) — CRUD; senha cifrada via `secret_store`
- [app/persistence/router.py](../../../app/persistence/router.py) — `shared_connect()` / `env_connect(slug)` + `list_env_slugs()`
- [app/persistence/context.py](../../../app/persistence/context.py) — ContextVar de ambiente ativo (`active_env`, `current_env_id`, `current_env_slug`)
- [app/persistence/db.py](../../../app/persistence/db.py) — shim: `connect()` roteia via contextvar; `connect_shared()` é explícito
- [app/web/middleware/environment.py](../../../app/web/middleware/environment.py) — lê cookie `portal_env`, ativa env no contexto
- [app/web/dependencies/environment.py](../../../app/web/dependencies/environment.py) — `current_environment` (412 se ausente), `current_env_db`
- [app/web/routes_environments.py](../../../app/web/routes_environments.py) — `/api/admin/environments/*` (admin-only)
- [app/web/routes_env_select.py](../../../app/web/routes_env_select.py) — `/api/env/list`, `/api/env/select`
- [app/worker/jobs/scan_environments.py](../../../app/worker/jobs/scan_environments.py) — watcher multi-pasta (a cada 30s)

## Fluxo de uso

1. **Admin** abre `/admin/ambientes` → cria "MM" e "Nasmar" preenchendo pastas e config FB
2. Botão **"Testar conexão"** em cada um valida pastas + tenta conexão Firebird
3. **Operador** loga → `/` redireciona para `/selecionar-ambiente` → escolhe MM
4. Cookie `portal_env` setado; toda navegação dele é MM até trocar
5. **Watcher** já estava ingerindo arquivos da pasta da MM e da Nasmar em paralelo (independente da seleção da UI)
6. Operador revisa pedido → "Enviar pra Fire" → FirebirdExporter conecta com creds **da MM**
7. Para trocar de empresa, dropdown no header → `/selecionar-ambiente` novamente

## Padrões importantes

### Bind imutável

Toda linha em `imports`, `audit_log`, `order_lifecycle_events`, `outbox` tem
`environment_id NOT NULL` populado no INSERT a partir do contextvar. UPDATE
jamais altera esse campo.

### ContextVar (não precisa passar Connection)

Repos por-ambiente (`repo.py`, `outbox_repo.py`, `state/events.py`) chamam
`db.connect()` sem parâmetros. O shim lê `env_context.current_env_slug()` e
abre a DB certa. Workers fazem `with active_env(env_id, slug): ...` ao
redor de cada iteração de empresa.

### connect() vs connect_shared()

Regra:
- Tabelas operacionais (`imports`, `audit_log`, `lifecycle_events`, `outbox`) → `db.connect()` (env)
- Tabelas transversais (`users`, `sessions`, `user_invites`, `inbound_idempotency`,
  `rate_limit_buckets`, `environments`) → `db.connect_shared()`

### Senha do Firebird

Cifrada via `app/security/secret_store.py` (Fernet). Nunca volta no GET (rotas
admin retornam `public_view` sem `fb_password_enc`). PATCH com:
- `fb_password=None` → mantém valor atual (default em edits parciais)
- `fb_password=""` → limpa
- `fb_password="..."` → substitui

### Slug imutável

Slug é validado contra `^[a-z0-9][a-z0-9-]{0,30}$`. Define o nome do arquivo
`app_state_<slug>.db`. Por isso: imutável após `create()`. UPDATE ignora
qualquer tentativa de mudar slug.

### Watcher de pasta

`scan_environments` (APScheduler, 30s). Para cada env ativo:
- Lista arquivos `.pdf|.xls|.xlsx` em `watch_dir`
- Sha256 já presente em `imports.file_sha256` → skip + move
- Pipeline.process(); falha → status='error' + arquivo em `Pedidos importados/com_erro/`
- Sucesso → status='success', `portal_status='parsed'` esperando review

## Testes

```bash
.venv/bin/pytest tests/test_environments_repo.py tests/test_persistence_context.py \
  tests/test_persistence_router.py tests/test_env_select_routes.py \
  tests/test_admin_environments_routes.py tests/test_scan_environments.py -v
```

## Variáveis de ambiente

Removidas do escopo singleton (mas ainda lidas por `connection.py` em modo legado):
- `FB_DATABASE`, `FB_HOST`, `FB_PORT`, `FB_USER`, `FB_PASSWORD`, `FB_CHARSET`
- `INPUT_DIR`, `OUTPUT_DIR`

Mantidas globais:
- `APP_DATA_DIR` — onde ficam os SQLite (`app_shared.db` + `app_state_<slug>.db`)
- `EXPORT_MODE` — `xlsx | db | both`
- `RETENTION_DAYS`, `BACKUP_DIR`
- `OPENROUTER_*`, `ANTHROPIC_API_KEY`
- `PORTAL_COOKIE_SECURE`, `SESSION_TTL_HOURS`
