# Módulo: security (HMAC, futuro: passwords/rate-limit)

## Responsabilidade
Primitivas de segurança usadas pelas integrações inbound e (Fase 4b) pela
auth da UI. Hoje cobre apenas verificação HMAC de webhooks.

## Arquivos críticos
- `app/security/hmac_verify.py` — `verify_hmac_request(body, signature_header,
  timestamp_header, secrets, max_skew_seconds=300)`. Lança:
  - `SignatureRequiredError` (401) — header ausente ou nenhum secret configurado.
  - `InvalidSignatureError` (403) — assinatura não bate.
  - `ReplayedRequestError` (403) — timestamp fora da janela de 5 min.
  Aceita lista de secrets (suporte a rotação). `compute_signature` exposta
  para testes/CLIs gerarem assinaturas válidas.

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
`tests/test_hmac_verify.py` — 11 testes: aceita válida, rejeita inválida,
header ausente, sem secret configurado, timestamp velho/futuro, garbage
timestamp, rotação aceita previous, prefixo opcional `sha256=`,
determinismo de `compute_signature`.

`.venv/bin/pytest tests/test_hmac_verify.py -v`

## Roadmap

- **Fase 4b** (próximo): `app/security/passwords.py` (bcrypt via passlib),
  `app/web/auth.py` (cookie httpOnly + SameSite + sessions).
- **Fase 6**: `app/security/rate_limit.py` (token bucket SQLite),
  `app/security/secrets.py` (vault-pluggable).

## Armadilhas

- **Não logar a assinatura nem o secret.** Body OK, headers OK, mas
  `X-Signature` mascarar em produção.
- **Toda rota nova de webhook precisa chamar `verify_hmac_request` ANTES
  de qualquer side effect.** Padrão em `app/web/webhooks.py`: verify →
  parse → idempotency → state.
