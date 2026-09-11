# environments — Multi-empresa (MM, Nasmar, e além)

## O que é

O Portal opera N empresas em paralelo. Cada **ambiente** (`environment`) tem:
- Pastas próprias (`watch_dir`, `output_dir`)
- Banco Firebird próprio (`fb_path`, `fb_host`, `fb_port`, `fb_user`, senha cifrada)
- Slug imutável usado como chave: `mm`, `nasmar`, etc.
- `cnpj` (dígitos, opcional) — CNPJ da empresa que o ambiente representa.
  Chave do degrau 1 do roteamento intercompany (documento): o `supplier_cnpj`
  detectado no pedido casa aqui. `NULL` = ambiente fora do roteamento
  automático. Índice único **parcial**, escopado a `is_active = 1`
  (`idx_environments_cnpj`) — dois ambientes ATIVOS com o mesmo CNPJ
  fariam `find_by_cnpj` devolver o que o `LIMIT 1` pegasse (pedido pra
  empresa errada, em silêncio); um ambiente desativado não segura o CNPJ
  pra sempre, senão desativar e recadastrar trava com 409 sem motivo real.
  Detalhe completo do roteamento em [`modules/routing.md`](routing.md).

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

### De-para de cliente intercompany

`intercompany_cnpj` (CNPJ que dispara) + `intercompany_env_slug` (ambiente cujo
Firebird tem o vínculo). Qualquer um vazio = desligado. O ambiente da produção
lê o Firebird do ambiente da revenda pela config **já cifrada** dela — não
existe credencial nova nem host no código. Configurável em `/admin/ambientes`
(`PUT /api/admin/environments/{env_id}/intercompany`).

### Cookie `portal_env`: gate em `desligado`/`observando`, filtro em `ligado`

Fora de `ligado` (roteamento intercompany, ver `modules/routing.md`) o
cookie funciona como sempre: sem ele, `/` redireciona para
`/selecionar-ambiente` e `current_environment` (dependency) levanta 412 —
é GATE, uma empresa é pré-requisito pra navegar.

Com `roteamento_modo == 'ligado'` o ambiente do pedido é decidido pelo
documento, não escolhido na sessão — o gate deixa de fazer sentido:
- `/` não redireciona mais para `/selecionar-ambiente` mesmo sem cookie.
- `GET /api/imported` (a listagem), sem cookie, soma as empresas em vez de
  exigir uma — `repo.list_imports_all_envs` / `count_imports_all_envs` /
  `count_by_portal_status_all_envs`, cada linha com o selo da empresa.
- Com cookie presente (mesmo em `ligado`), ele volta a ser **filtro**: a
  listagem mostra só aquela empresa.
- `POST /api/env/select` continua setando o cookie (agora "mostre só esta
  empresa"); `POST /api/env/clear` (novo) remove — o cookie é `HttpOnly`,
  então o JS não apaga sozinho. Sem essa rota não haveria como voltar a
  "todas as empresas" sem deslogar.
- `current_environment` muda a mensagem do 412 de "Selecione um ambiente
  para continuar" para "Esta ação é de uma empresa específica — abra o
  pedido para agir nele" — mais honesta em todos os modos, porque nem toda
  ação tem uma tela de seleção como próximo passo.

### Watcher de pasta

`scan_environments` (APScheduler, 30s). Para cada env ativo:
- Lista arquivos `.pdf|.xls|.xlsx` em `watch_dir`
- Sha256 já presente → skip + move. Em `desligado` a checagem é só dentro do
  próprio ambiente (`(environment_id, sha256)`, igual antes desta feature);
  fora de `desligado` é global (Portal inteiro) — o roteamento pode mandar o
  arquivo pra outra empresa. Detalhe em [`modules/routing.md`](routing.md).
- Pipeline.process(); falha → status='error' + arquivo em `Pedidos importados/com_erro/`
- Roteamento (fora de `desligado`) decide o ambiente que grava — pode
  divergir do ambiente varrido; sem resposta em `ligado`, retém (não
  importa) e vira pendência.
- Sucesso → status='success', `portal_status='parsed'` esperando review

## Testes

```bash
.venv/bin/pytest tests/test_environments_repo.py tests/test_persistence_context.py \
  tests/test_persistence_router.py tests/test_env_select_routes.py \
  tests/test_admin_environments_routes.py tests/test_scan_environments.py -v
```

Cookie gate→filtro e cross-env de `/api/imported`: `tests/test_routing_modo.py`.
Watcher + roteamento (dedupe por modo, throttle de pendência, audit):
`tests/test_scan_environments_roteamento.py` — ver `modules/routing.md`.

## Variáveis de ambiente

Removidas do escopo singleton (mas ainda lidas por `connection.py` em modo legado):
- `FB_DATABASE`, `FB_HOST`, `FB_PORT`, `FB_USER`, `FB_PASSWORD`, `FB_CHARSET`
- `INPUT_DIR`, `OUTPUT_DIR`

Mantidas globais:
- `APP_DATA_DIR` — onde ficam os SQLite (`app_shared.db` + `app_state_<slug>.db`)
- `EXPORT_MODE` — `xlsx | db | both`
- `RETENTION_DAYS`, `BACKUP_DIR`
- `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (o SDK da Anthropic foi descontinuado — não há `ANTHROPIC_API_KEY` no código)
- `PORTAL_COOKIE_SECURE`, `SESSION_TTL_HOURS`
