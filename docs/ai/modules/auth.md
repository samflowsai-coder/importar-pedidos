# Módulo: auth (sessão da UI)

## Responsabilidade
Autenticação por e-mail + senha (bcrypt) com sessão server-side em cookie
HttpOnly. Bloqueia rotas de mutação contra acesso anônimo.

**Escopo da Fase 4b:** apenas "logged-in vs anônimo". RBAC por
`role` é informacional (campo armazenado, não enforced ainda) — Fase 6.

## Arquivos críticos
- `app/security/passwords.py` — wrapper bcrypt (`hash_password`,
  `verify_password`, `hash_needs_rehash`). Rounds=12 (OWASP 2024+).
  Rejeita senhas <8 caracteres e >72 bytes (limite silencioso do bcrypt).
- `app/persistence/users_repo.py` — `User` dataclass + CRUD.
  `email UNIQUE COLLATE NOCASE`. Roles: admin | operator | viewer.
- `app/persistence/sessions_repo.py` — `Session` server-side. Token =
  `secrets.token_urlsafe(32)` (256-bit). TTL 24h default. `get_active`
  faz lazy-delete de sessions expiradas.
- `app/web/auth.py` — dependências FastAPI:
  - `Depends(current_user)` → `Optional[User]` (None se anônimo).
  - `Depends(require_user)` → `User` ou 401.
  - `set_session_cookie` / `clear_session_cookie`.
- `app/web/server.py` rotas auth:
  - `POST /api/auth/login` — credentials → cookie.
  - `POST /api/auth/logout` — apaga sessão.
  - `GET /api/auth/me` — whoami para a SPA decidir login screen vs main.
  - `GET /login` — serve `static/login.html`.
- `app/web/static/login.html` — formulário mínimo, dark-first.
- `tools/create_user.py` — CLI bootstrap (sem signup pela UI por design).

## Cookie
```
Name      = portal_session
Value     = secrets.token_urlsafe(32)  (server-side row em sessions)
HttpOnly  = True              (XSS containment)
SameSite  = Strict            (CSRF containment para browsers)
Secure    = True em prod      (PORTAL_COOKIE_SECURE=1, default)
Max-Age   = 24h               (SESSION_TTL_HOURS configurável)
Path      = /
```

`PORTAL_COOKIE_SECURE=0` em dev/testes (HTTP). Default fail-closed:
ausência da env var resulta em Secure=True, então esquecer não vira
cookie sobre plaintext.

## Rotas protegidas (Fase 4b)
Todas as **POST de mutação** exigem `Depends(require_user)`:
- `/api/config`, `/api/import`, `/api/reimport`
- `/api/preview`, `/api/preview-pending`, `/api/commit`
- `/api/imported/{id}/send-to-fire`, `/api/batch/send-to-fire`
- `/api/imported/{id}/post-to-gestor`
- `/api/imported/{id}/cancel`
- `/api/imported/{id}/override-cliente`
- `/api/process` (legacy)
- `/api/auth/logout`

**GETs continuam abertos** nesta fase. Lockdown de leitura é Fase 6.

**`/api/webhooks/gestor`** usa HMAC, não session — não toca em auth.

## Defesas

- **Generic 401 message no login.** "email ou senha inválidos" para
  qualquer falha — evita enumeração de e-mails registrados.
- **Constant-time verify mesmo quando user não existe.** `verify_password`
  roda contra um dummy hash de mesma estrutura, eliminando timing leak.
- **Opportunistic rehash on login.** Se `rounds` atual < default,
  `update_password_hash` é chamado silenciosamente — usuários
  ganham hashes mais fortes sem ação manual.
- **Inactive user rejected.** `users.active=0` recusa login com 401.
- **Cascade delete em sessions.** Apagar `users` row apaga todas as
  sessões via FK ON DELETE CASCADE.
- **Senha no body, body na resposta nunca.** Logs do FastAPI não capturam
  request bodies por default.

## Bootstrap

```bash
.venv/bin/python tools/create_user.py admin@portal.local --role admin
# prompts senha 2x

# Não-interativo (CI / Dockerfile init):
echo "supersecret" | .venv/bin/python tools/create_user.py bot@portal.local

# Reset de senha existente:
.venv/bin/python tools/create_user.py admin@portal.local --reset
```

## Testes

- `tests/test_passwords.py` — 10 testes do bcrypt wrapper.
- `tests/test_users_repo.py` — 11 testes do CRUD.
- `tests/test_sessions_repo.py` — 10 testes do server-side store.
- `tests/test_auth_routes.py` — 16 testes do flow real (login/logout/me +
  enforcement + webhook coexistence).

`.venv/bin/pytest tests/test_passwords.py tests/test_users_repo.py
tests/test_sessions_repo.py tests/test_auth_routes.py -v`

## Auth bypass para testes legados

`tests/conftest.py` setta `TEST_AUTH_BYPASS=1` por default. Quando ligado,
`require_user` retorna um User sintético (admin) sem cookie. Os 41+ testes
web pré-existentes continuam passando sem fixture de login.

Tests que querem o flow real usam a fixture `real_auth` (em
`conftest.py`), que `delenv("TEST_AUTH_BYPASS")` para o teste.

## Roadmap (Fase 6)

- Rate-limit em `/api/auth/login` (token bucket SQLite, 10/15min/IP).
- Reset de senha por e-mail (Fase 6+ — exige stack de e-mail).
- 2FA (TOTP) para roles admin.
- Lockdown de GET routes (filtro por user/cliente).
- RBAC: `require_role("admin")` para operações destrutivas.

## Armadilhas

- **Não logar `password_hash`** nem o token de sessão. Logs de exception
  não devem capturar `request.cookies`.
- **`User.password_hash` está exposto no dataclass** porque o flow de
  login precisa dele. NUNCA serializar pra JSON / response.
- **Bcrypt trunca silenciosamente em 72 bytes.** `hash_password` rejeita
  loud. Se alguém bypassar por outra rota, ataque potencial — sempre use
  o helper.
- **Cookie SameSite=Strict bloqueia CORS legítimos.** Se vamos integrar
  outro front-end domain, mudar pra Lax + adicionar CSRF tokens.
