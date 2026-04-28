# Módulo: security (HMAC, passwords, rate-limit, secrets)

## Responsabilidade
Primitivas de segurança: verificação HMAC de webhooks, hash de senhas,
rate-limit de login e abstração de secrets.

## Arquivos críticos

### HMAC (webhooks)
- `app/security/hmac_verify.py` — `verify_hmac_request(body, signature_header,
  timestamp_header, secrets, max_skew_seconds=300)`. Lança:
  - `SignatureRequiredError` (401) — header ausente ou nenhum secret configurado.
  - `InvalidSignatureError` (403) — assinatura não bate.
  - `ReplayedRequestError` (403) — timestamp fora da janela de 5 min.
  Aceita lista de secrets (suporte a rotação). `compute_signature` exposta
  para testes/CLIs gerarem assinaturas válidas.

### Passwords
- `app/security/passwords.py` — bcrypt rounds=12. `hash_password`, `verify_password`,
  `hash_needs_rehash`. Rejeita <8 chars ou >72 bytes UTF-8. Rehash oportunístico
  no login bem-sucedido se rounds < 12.

### Rate-limit (token bucket)
- `app/web/middleware/rate_limit.py` — `check_and_consume(key, capacity, refill_rate, cost=1.0) -> bool`.
  Persiste estado em SQLite (`rate_limit_buckets`). Transação DEFERRED — safe para
  requisições concorrentes sem lock externo.
  - Env `RATE_LIMIT_ENABLED=false` bypassa completamente (dev/test).
  - Aplicado como `Depends(_login_rate_limit)` em `POST /api/auth/login`:
    capacity=10, refill_rate=10/900 (10 req/15 min/IP), 429 + `Retry-After: 900`.

### Secrets
- `app/security/secrets.py` — `get_secret(name, default=None)`. Lê de `os.environ`
  hoje; troca de backend sem mudar chamadores (Protocol `SecretsBackend`).
  `_set_backend(backend)` para testes. **Read-only**: usar para tokens / API keys
  que continuam vindo de env.

### Secret store (UI-editable, cifrado em disco)
- `app/security/secret_store.py` — Fernet (cryptography) para secrets que o admin
  edita via UI (hoje: senha do Firebird).
  - `encrypt(plaintext: str) -> str`, `decrypt(token: str) -> str | None`.
  - Chave gerada lazy em `app/.secret.key` (chmod 600, gitignored). Perda da chave
    invalida ciphertexts → `decrypt()` retorna `None`, log de erro, admin re-salva.
  - Consumido por `app/firebird_config.py:save/get_password`.
  - **Por que separado de `secrets.py`?** `secrets.py` é leitura abstrata para
    tokens/secrets injetados pelo deploy. `secret_store.py` é storage local
    cifrado para config editável pelo usuário em runtime — escopos diferentes,
    superfícies de teste diferentes.
- Tests: `tests/test_secret_store.py`.

## Wire format esperado (PLACEHOLDER — confirmar com Gestor)

```
X-Signature: sha256=<hex>      ← HMAC-SHA256 sobre f"{timestamp}.{body}"
X-Timestamp: <unix seconds>    ← anti-replay (default skew 5min)
```

Body assinado: bytes brutos do request, prefixados pelo timestamp +
`.`. **Ler o body raw ANTES de parsear JSON** — qualquer normalização
quebra a assinatura.

## Rotação de secret

Dois envs aceitos simultaneamente para janela de transição:

```
WEBHOOK_SECRET_GESTOR=novo-secret               # current
WEBHOOK_SECRET_GESTOR_PREVIOUS=secret-anterior  # opcional, removível
```

Procedimento de rotação:
1. Gerar novo secret. Configurar `WEBHOOK_SECRET_GESTOR=novo` e
   `WEBHOOK_SECRET_GESTOR_PREVIOUS=antigo`. Deploy.
2. Atualizar Gestor para começar a assinar com o novo. Confirmar tráfego
   chegando com novo (ver logs).
3. Após confirmação, remover `WEBHOOK_SECRET_GESTOR_PREVIOUS`. Deploy.

## Decisões de design

- **Fail-closed na ausência de secret.** Se ambas as envs estão vazias,
  `verify_hmac_request` lança `SignatureRequiredError` em vez de aceitar
  qualquer coisa. Misconfiguração não vira hole.
- **Constant-time compare (`hmac.compare_digest`).** Anti-timing attack.
- **Timestamp + idempotency são independentes.** O timestamp limita
  janela de replay; o idempotency_key (em `inbound_idempotency`) garante
  que mesmo dentro da janela, processamento ocorre uma única vez.
- **Body bruto, não JSON normalizado.** Provider e receptor precisam
  byte-igualdade na assinatura.

## Testes
- `tests/test_hmac_verify.py` — HMAC: válida, inválida, header ausente, sem secret,
  timestamp velho/futuro, garbage, rotação, prefixo sha256=, determinismo.
- `tests/test_passwords.py` — bcrypt: hash, verify, rehash detection, limites.
- `tests/test_rate_limit.py` — token bucket: first request, esgotamento, isolamento
  por IP, refill por tempo, bypass, 429 via TestClient.
- `tests/test_secrets.py` — env var read, default, None, custom backend, Protocol.

```bash
.venv/bin/pytest tests/test_hmac_verify.py tests/test_passwords.py \
  tests/test_rate_limit.py tests/test_secrets.py -v
```

## Armadilhas

- **Não logar a assinatura nem o secret.** Body OK, headers OK, mas
  `X-Signature` mascarar em produção.
- **Toda rota nova de webhook precisa chamar `verify_hmac_request` ANTES
  de qualquer side effect.** Padrão em `app/web/webhooks.py`: verify →
  parse → idempotency → state.
