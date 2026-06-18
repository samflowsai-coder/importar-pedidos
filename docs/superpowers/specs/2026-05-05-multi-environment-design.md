# Multi-ambiente — MM e Nasmar (e além)

**Data:** 2026-05-05
**Autor:** design colaborativo (Sam + Claude)
**Status:** aprovado para implementação

## Contexto

O cliente (grupo MM/Nasmar) opera duas empresas que usam o ERP Fire Sistemas em bancos Firebird **separados**, com pastas e estruturas operacionais distintas. O Portal de Pedidos hoje é singleton: uma config de pastas (`config.json`), uma config Firebird (`firebird.json`), uma SQLite (`app_state.db`). Precisamos suportar N empresas, cada uma com sua config, sem misturar dados entre elas — nem por bug, nem por race.

A arquitetura aqui é extensível: hoje 2 empresas, amanhã 3 ou 4 sem release.

## Decisões fechadas

| Decisão | Escolha | Razão |
|---|---|---|
| Modelo de profile | Extensível (N ambientes nomeados) | Adicionar 3ª empresa vira config, não release |
| Isolamento de dados | Híbrido SQLite — DB por ambiente para pedidos/lifecycle/outbox; DB compartilhada para users/sessions/idempotency | Dados financeiro-operacionais nunca se misturam mesmo com query mal-escrita |
| Escopo de seleção | Sessão (cookie do usuário) | Multi-operador sem race; padrão SaaS multi-tenant |
| Bind de pedido | Imutável no upload | Auditoria e integridade do histórico |
| Pastas | Independentes por ambiente | Cada empresa pode ter estrutura de rede diferente |
| Auto-import | Watcher monitora todas as pastas configuradas | Pedidos aparecem disponíveis no portal sem upload manual |
| Migração de dados existentes | Reset limpo — operador configura no cliente | Evita atribuir dados a empresa errada por inferência |

## Modelo de dados

### Tabela `environments` (em `app_shared.db`)

```sql
CREATE TABLE environments (
    id              TEXT PRIMARY KEY,           -- uuid4
    slug            TEXT UNIQUE NOT NULL,       -- [a-z0-9-]+, imutável após criar
    name            TEXT NOT NULL,              -- editável: "MM Calçados", "Nasmar"
    watch_dir       TEXT NOT NULL,              -- absoluto
    output_dir      TEXT NOT NULL,              -- absoluto
    fb_path         TEXT NOT NULL,              -- caminho .fdb
    fb_host         TEXT,                       -- NULL = embedded
    fb_port         TEXT,
    fb_user         TEXT NOT NULL DEFAULT 'SYSDBA',
    fb_charset      TEXT NOT NULL DEFAULT 'WIN1252',
    fb_password_enc TEXT,                       -- cifrado via secret_store (Fernet)
    is_active       INTEGER NOT NULL DEFAULT 1, -- soft-delete (preserva FKs)
    created_at      TEXT NOT NULL,              -- ISO 8601
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX idx_environments_slug ON environments(slug);
CREATE INDEX idx_environments_active ON environments(is_active);
```

**Regras:**
- `slug` é validado contra `^[a-z0-9][a-z0-9-]{1,30}$` e é imutável depois da criação (vira parte do nome do arquivo `app_state_<slug>.db`)
- Nome é editável livremente
- `is_active=0` esconde da UI mas preserva integridade referencial — pedidos antigos ainda apontam pra ele
- Senha sempre cifrada; chave reside em `app/.secret.key` (já existe via `secret_store.py`)

### Coluna `environment_id` em tabelas por-ambiente

Adicionar `environment_id TEXT NOT NULL` em todas as tabelas das DBs por-ambiente:
- `imports`
- `audit_log`
- `order_lifecycle_events`
- `outbox`
- `rate_limit_buckets`

Redundante (a DB já é específica de um ambiente), mas é uma defesa em profundidade — qualquer linha sem `environment_id` correto é detectável e auditável. Validador no repo rejeita INSERT sem `environment_id` ou UPDATE que tente alterá-lo.

## Arquitetura de persistência

### Layout de arquivos

```
data/
├── app_shared.db                    # users, sessions, environments, invites, inbound_idempotency
├── app_state_mm.db                  # pedidos da MM
├── app_state_nasmar.db              # pedidos da Nasmar
└── ... (1 por ambiente)
```

### Roteamento de conexões

Novo módulo `app/persistence/router.py`:

```python
def shared_db_path() -> Path: ...
def env_db_path(slug: str) -> Path: ...
def shared_connect() -> sqlite3.Connection: ...
def env_connect(slug: str) -> sqlite3.Connection: ...
def list_env_slugs() -> list[str]: ...   # query environments table; usado por workers
```

**Pool de conexões:** mantido como hoje (sqlite3.connect direto, sem pool); cada request abre/fecha. Performance medida; se virar gargalo, viramos pool por DB.

### Repos — assinatura

Repos atuais abrem conexão internamente via `db.connect()`. Migram pra **receber `Connection` injetada**:

```python
# antes
def get_import(import_id: str) -> dict: ...

# depois
def get_import(conn: Connection, import_id: str) -> dict: ...
```

**Quem injeta:**
- Rotas FastAPI usam dependency `current_env_db(request) -> Connection` que lê `request.state.environment` e abre/fecha a conexão por request
- Repos compartilhados (`users_repo`, `sessions_repo`, `idempotency_repo`, `invites_repo`) usam dependency `shared_db() -> Connection`
- Workers iteram explicitamente sobre `list_env_slugs()` e abrem cada conexão

**Por quê injetar em vez de manter `db.connect()` global:** força o caller a saber em que ambiente está operando — esquecimento vira erro de tipo, não vazamento de dado.

### Schema/Migrations

Hoje `db.py` tem `_SCHEMA_TABLES` + `_SCHEMA_INDEXES` + `_COLUMN_MIGRATIONS` aplicados inline em cada `connect()`. Mantemos esse approach, mas dividido:

- `app/persistence/schema_shared.py` — DDL para `app_shared.db` (users, sessions, environments, invites, idempotency)
- `app/persistence/schema_env.py` — DDL para DB por ambiente (imports, audit_log, lifecycle, outbox, rate_limit)
- `_apply_migrations()` continua existindo, mas separado pra cada schema

**Inicialização:**
1. Boot do FastAPI / worker
2. Garantir `app_shared.db` existe + schema aplicado
3. Query `environments` ativos
4. Para cada slug: garantir `app_state_<slug>.db` existe + schema aplicado

### APScheduler jobstore

Hoje aponta pra `app_state.db`. Move pra `app_shared.db` (jobstore é metadata operacional do worker, não dado de empresa).

### Backup/retention

Worker `retention.py` itera sobre todas DBs (`shared` + uma por ambiente). Backup nomeado `<slug>_<timestamp>.db` ou `shared_<timestamp>.db`. Retention de `audit_log`/`order_lifecycle_events` aplica em cada DB de ambiente.

## Sessão e seleção de ambiente

### Cookie

Novo cookie `portal_env=<environment_id>`, HttpOnly, SameSite=Strict, Secure (mesmo perfil do `portal_session`). Lifetime = mesmo da sessão; trocar ambiente sobrescreve. Logout limpa ambos.

### Middleware

Novo `app/web/middleware/environment.py`:

```python
async def resolve_environment(request: Request, call_next):
    env_id = request.cookies.get("portal_env")
    if env_id:
        env = environments_repo.get(shared_db(), env_id)
        if env and env["is_active"]:
            request.state.environment = env
    return await call_next(request)
```

Dependencies novas:
- `current_environment(request) -> dict` — 401/redirect se ausente
- `current_env_db(request) -> Connection` — abre conexão pro DB do env atual

### Fluxo de UX

```
Login → /api/auth/login OK
     → tem cookie portal_env válido?
        ├─ sim: /  (dashboard)
        └─ não: /selecionar-ambiente
                 → escolheu MM
                 → SET-COOKIE portal_env=<id>
                 → redirect /
```

Header (`shell.js`) ganha:
- Badge mostrando nome do ambiente atual
- Dropdown com opção "Trocar ambiente" (volta para `/selecionar-ambiente`)
- Para admins: link "Gerenciar ambientes" → `/admin/ambientes`

Rotas que **não** exigem ambiente selecionado:
- `/login`, `/api/auth/*`
- `/selecionar-ambiente`
- `/admin/ambientes` e `/api/admin/environments/*` (admin-only)
- `/admin/usuarios` (gestão de operadores)

### Bind imutável

No upload (UI ou watcher):
1. Resolve `environment_id` do contexto (sessão pra UI, contexto do watcher pro auto-import)
2. INSERT em `imports` com `environment_id NOT NULL`
3. Repo valida que UPDATE jamais altera `environment_id` (constraint via trigger SQLite ou validação no repo)

## Watcher multi-pasta

### Estado atual

Hoje `/api/files` faz scan manual do `watch_dir` único, retorna lista pra UI. Não há watcher automático.

### Estado novo

Job APScheduler novo: `scan_environments` (a cada 30s):

1. Para cada ambiente ativo:
   - Lista arquivos em `watch_dir` (filtra extensões: pdf, xls, xlsx)
   - Para cada arquivo novo (não existe em `imports` com mesmo `original_path` + `environment_id`):
     - Roda pipeline (parse + normalize + validate)
     - Insere em `imports` com `environment_id` desse ambiente, status `PARSED` (aguardando review do operador)
     - Move arquivo pra `<watch_dir>/Pedidos importados/` (mesma convenção que `app/config.py:imported_dir` já usa hoje)

2. Operador entra no portal → seleciona ambiente → vê lista de pedidos pré-parseados → revisa → commita (gera xlsx + insere no Firebird conforme `EXPORT_MODE`)

### Conflito com `/api/files` (scan manual)

Mantemos `/api/files` como fallback/refresh manual. Em vez de listar do disco, lista da tabela `imports` filtrada por `environment_id`. UI mostra status (`PARSED`, `VALIDATED`, `EXPORTED`, `ERROR`).

### Idempotência

Chave de dedup: `(environment_id, sha256(arquivo))`. Mesmo arquivo recolocado na pasta não duplica.

## CRUD de ambientes (admin-only)

### Rotas

```
GET  /admin/ambientes                        # HTML — lista
GET  /admin/ambientes/novo                   # HTML — form criar
GET  /admin/ambientes/{id}                   # HTML — form editar

GET    /api/admin/environments               # JSON — list
POST   /api/admin/environments               # criar
GET    /api/admin/environments/{id}          # detalhe (sem senha)
PATCH  /api/admin/environments/{id}          # atualizar (senha opcional)
POST   /api/admin/environments/{id}/test     # testar conexão FB + acesso a pastas
DELETE /api/admin/environments/{id}          # soft-delete (is_active=0)
```

### Validações

- `slug` único, regex, imutável após criar
- `watch_dir` e `output_dir` devem existir e ser graváveis (testar em `POST` e `PATCH`)
- `fb_path` testado via tentativa de conexão (`/test`)
- Não permite soft-delete de ambiente com pedidos em status não-terminal (`PARSED`, `VALIDATED`)

### UI

- `admin-ambientes.html` — lista em cards (nome, slug, watch_dir, status conexão FB)
- `admin-ambiente-edit.html` — form com seções: Identificação, Pastas, Firebird (com botão "Testar")
- Reuso do shell + tokens.css

## Migração e deploy

### Estado de saída

`config.json`, `firebird.json`, `app_state.db` em produção/pré-prod.

### Plano de deploy

1. **Backup full** do diretório atual antes de qualquer ação
2. **Deploy do código novo** com flag `MULTI_ENV_BOOTSTRAP=1`
3. Na primeira subida com a flag:
   - Cria `data/app_shared.db` com schema novo
   - **Não migra** dados antigos (decisão: reset limpo)
   - Loga que `app_state.db` antigo está intacto em `data/app_state.db.legacy` (renomeado pra evitar confusão)
4. **Bootstrap manual** com o operador:
   - Login com admin existente (users continuam em `app_shared.db` — esses sim migrados de `app_state.db`)
   - Criar ambiente "MM" com config completa
   - Criar ambiente "Nasmar" com config completa
   - Testar conexão FB + acesso a pastas em cada um
5. **Sanity check**: subir um pedido em cada ambiente, verificar split correto

### Migração de `users`/`sessions`/`invites`

Esses sim migram do `app_state.db` antigo pro `app_shared.db` novo, porque são metadata transversal (operadores não mudam por causa de multi-empresa). Script `tools/migrate_to_multi_env.py`:

```python
# uma vez, no deploy:
# - copia tabelas users, user_invites, sessions de app_state.db -> app_shared.db
# - sessões antigas continuam válidas (cookie portal_session ainda funciona)
# - usuário precisa selecionar ambiente no próximo request
```

### Variáveis de ambiente

Removidas (não fazem mais sentido como singleton):
- `INPUT_DIR`, `OUTPUT_DIR` (agora por ambiente, em DB)
- `FB_DATABASE`, `FB_HOST`, `FB_PORT`, `FB_USER`, `FB_PASSWORD`, `FB_CHARSET` (idem)

Mantidas:
- `APP_DATA_DIR` — onde ficam as DBs SQLite
- `EXPORT_MODE` — global (`xlsx`, `db`, `both`); semântica por ambiente seria overkill agora
- `OPENROUTER_*`, `ANTHROPIC_API_KEY` — globais
- `RETENTION_DAYS`, `BACKUP_DIR` — globais

`firebird_config.py` é **removido** junto com o deploy multi-ambiente. Não há shim de compatibilidade — todos os usos (rotas `/api/firebird/*`, chamadas `apply_to_env()` no startup, conexões ad-hoc do exporter Firebird) são atualizados pra usar `environments_repo` + contexto da request. O arquivo `firebird.json` em produção é renomeado pra `firebird.json.legacy` no deploy e ignorado.

## Testes

### Estratégia geral

- **Helpers de teste** novos em `tests/conftest.py`:
  - `tmp_shared_db` — fixture que cria `app_shared.db` em tmp + aplica schema
  - `tmp_env_db` — fixture que cria `app_state_<slug>.db` em tmp
  - `make_environment(slug, name, ...)` — factory pra criar env de teste
- Testes existentes que assumem `app_state.db` único migram para usar fixtures novas
- Testes novos:
  - `test_environments_repo.py` — CRUD, slug imutável, soft-delete, encrypt/decrypt senha
  - `test_environment_middleware.py` — cookie ausente → redirect; cookie inválido → 401; admin route não exige env
  - `test_persistence_router.py` — roteamento abre DB correta; isolamento entre slugs
  - `test_watcher_scan.py` — scan multi-pasta, idempotência por sha, bind imutável
  - `test_admin_environments_routes.py` — CRUD via API, validação, permissões
  - `test_migration_script.py` — script copia users/sessions/invites corretamente
- Atualizar testes de auth pra cobrir o novo fluxo `login → selecionar-ambiente → dashboard`

### Cobertura crítica

- **Isolamento**: pedido inserido na DB da MM nunca aparece em query da Nasmar (mesmo se filtro de sessão for bypassed)
- **Bind imutável**: tentativa de UPDATE em `environment_id` rejeitada
- **Concorrência**: dois operadores em ambientes diferentes simultaneamente — estado da sessão de um não afeta o outro

## Risco e plano de rollback

| Risco | Mitigação |
|---|---|
| Refactor grande do `db.py` quebra repos | Diff incremental; rodar suite completa após cada migração |
| Dados antigos perdidos | Backup antes do deploy; `app_state.db.legacy` preservado |
| Operador esquece de selecionar ambiente e tenta importar | Middleware bloqueia; UI redireciona pra `/selecionar-ambiente` |
| Senha FB de ambiente perdida (chave secret_store deletada) | Mesmo problema do estado atual; admin re-salva via UI |
| Watcher processa arquivo na pasta errada se admin troca `watch_dir` | Ao trocar `watch_dir`, sistema avisa que arquivos lá serão associados a esse ambiente; tornar mudança de `watch_dir` um log de audit_log |

**Rollback:** restaurar diretório do backup full + tag git anterior. Sessões antigas no `app_state.db.legacy` continuam intactas.

## Decisões adiadas (não bloqueiam v1)

- **Permissão por ambiente** (operador X só pode ver MM): hoje qualquer usuário autenticado pode trocar pra qualquer ambiente. Adicionar tabela `user_environments` se virar requisito.
- **Métricas Prometheus por ambiente**: `/metrics` retorna labels com `environment` se necessário; v1 mantém global.
- **Multi-tenancy real**: nada aqui ainda separa empresas como tenants distintos com URL própria. Continua sendo deploy único, multi-empresa interno.

## Pontos de implementação críticos

1. `app/persistence/db.py` — split em `schema_shared.py` + `schema_env.py` + `router.py`
2. Todos os repos passam a receber `Connection` (refactor mecânico extenso, mas seguro)
3. `app/firebird_config.py` — deprecate; rotas `/api/firebird/config` redirecionam pra `/api/admin/environments`
4. `app/web/server.py` — middleware novo + dependencies novas + rotas admin novas
5. `app/web/static/shell.js` — exibe badge do ambiente, fetch em `/api/auth/me` agora retorna também ambiente atual
6. `app/worker/scheduler.py` — jobstore aponta pra shared; jobs iteram sobre slugs
7. Job novo: `app/worker/jobs/scan_environments.py`
8. CLI `tools/migrate_to_multi_env.py` — migra users/sessions/invites
