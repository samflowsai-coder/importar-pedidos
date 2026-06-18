# Validação de preço pedido vs Fire

**Data:** 2026-05-08
**Domínio:** `erp` + `web` + `persistence` + `observability`
**Status:** Design aprovado — aguarda implementation plan

---

## Problema

Hoje o portal compara o pedido recebido com o cadastro do Fire só por **identificação de produto** (EAN ou `CODPROD_ALTERN`). O `product_check.py` já lê `PRODUTOS.PRECO_VENDA` no momento do match, mas o valor é descartado: a UI não mostra, o servidor não compara, e o pedido é importado independentemente de divergência de preço.

Regra de negócio confirmada com a operação:
- Tabela de preço fica no cadastro do produto no Fire (sem tabelas por cliente).
- Preço de referência = `PRODUTOS.PRECO_VENDA`.
- Divergência → **não importa**; usuário ajusta cadastro ou planilha e reimporta (regra do Rafa).

## Objetivo

Fechar o ciclo de validação: comparar `OrderItem.unit_price` com `PRODUTOS.PRECO_VENDA`, sinalizar divergência na UI e bloquear o botão primário do preview (tanto "Cadastrar no Fire" quanto "Gerar XLS") até que o operador resolva.

Tratamento especial para o estado de transição em que muitos produtos ainda não têm preço cadastrado: sinaliza warning, exige confirmação explícita do operador (gravada em audit), e libera.

## Não-objetivos (YAGNI)

- Não muda algoritmo de identificação do produto (EAN → `CODPROD_ALTERN` permanece).
- Não introduz tabela de preço por cliente (não existe no Fire ainda).
- Não introduz tolerância configurável (igualdade exata por decisão).
- Não bloqueia produto sem match de identificação (mantém o comportamento atual de warning).

## Regras

| Estado do item | Resultado |
|---|---|
| Preço Fire = preço pedido (igualdade exata) | ✓ libera |
| Preço Fire ≠ preço pedido | ✗ bloqueia hard (sem ack possível) |
| Pedido sem `unit_price` (parser não pegou) | ✗ bloqueia hard (sem ack possível) |
| Produto achado, sem preço no Fire (`NULL` ou `0`) | ⚠ exige ack do operador, depois libera |
| Produto sem match no Fire | — (warning, comportamento atual; sem novo bloqueio) |

Comparação é em centavos: `round(valor * 100)` → int → comparação direta. Evita drift de float.

Ack é **por pedido**, não por item: um clique cobre todos os itens sem preço daquele pedido. Persistido em sidecar de `imports`. Re-import gera novo `import_id`, então começa sem ack — comportamento natural, sem código de cleanup.

## Componentes

### 1. `app/erp/product_check.py`

Estende o report por item com:

```python
{
    # ... campos existentes (product_code, ean, match, match_source,
    # fire_product_id, fire_description, fire_preco_venda)
    "unit_price_order": float | None,   # espelho de OrderItem.unit_price
    "price_status": str,                 # vide enum abaixo
    "price_diff": float | None,          # fire_preco_venda - unit_price_order, em reais
}
```

Enum de `price_status`:
- `"no_product_match"` — produto não achado; pular validação de preço.
- `"no_price_in_fire"` — produto achado, `PRECO_VENDA` é NULL ou 0.
- `"no_order_price"` — pedido não trouxe `unit_price`.
- `"match"` — preços iguais (cents).
- `"mismatch"` — preços diferentes (cents).

Adiciona ao `summary`:
```python
"price_summary": {
    "items_match": int,
    "items_mismatch": int,
    "items_no_price_in_fire": int,
    "items_no_order_price": int,
}
```

Nova função pública:

```python
def is_blocking(check: dict, ack_items: list[dict] | None = None) -> tuple[bool, dict]:
    """Retorna (bloqueia, detalhe).

    detalhe = {
        "items_mismatch": [{ean, product_code, order_price, fire_price}],
        "items_no_order_price": [{ean, product_code}],
        "items_no_price_unacked": [{ean, product_code}],
    }

    Bloqueia se houver qualquer item com price_status='mismatch' ou
    'no_order_price', OU se houver 'no_price_in_fire' não coberto por ack_items.

    `ack_items` vem de imports.sem_preco_ack_items (lista [{ean, product_code}]);
    item é considerado coberto se EAN bate (quando presente) OU product_code bate.
    """
```

### 2. UI — `app/web/static/index.html`

**Coluna "Fire"** (renomeia a coluna "Match" atual; consolida match de produto + match de preço):

| price_status | Render |
|---|---|
| `match` (e match=true) | `<span class="ok">✓</span>` (sutil) |
| `mismatch` | `<span class="err">✗ R$ 89,90</span>` (vermelho, mostra preço do Fire) |
| `no_price_in_fire` | `<span class="warn">⚠ sem preço</span>` (amarelo) |
| `no_order_price` | `<span class="err">✗ pedido sem preço</span>` (vermelho) |
| `no_product_match` (match=false) | `<span class="err">✗</span>` (mantém atual) |
| `check.available=false` | `—` (cinza, mantém atual) |

**Banner `#pvCheckBanner`** — prioridade (mais grave primeiro):
1. Se `items_mismatch > 0` → vermelho: "N item(s) com preço divergente do Fire — ajuste o cadastro ou a planilha e reimporte."
2. Senão se `items_no_order_price > 0` → vermelho: "N item(s) sem preço no pedido — corrija a planilha e reimporte."
3. Senão se `items_no_price_unacked > 0` → amarelo + botão **"Confirmar e prosseguir"**: "N item(s) sem preço cadastrado no Fire."
4. Senão se ack registrado e ainda há `no_price_in_fire` → cinza discreto: "Confirmado por <email> em <data>: N item(s) sem preço cadastrado serão importados sem validação."
5. Mantém o banner atual de cliente / itens-sem-match quando aplicável.

**Botão primário (`#pvCommitBtn`)** — desabilitado quando o front detecta bloqueio (mesma lógica de `is_blocking`, replicada em JS pra resposta imediata; servidor é fonte de verdade). Tooltip explica o motivo:
- "N divergência(s) de preço — corrija antes de enviar"
- "Pedido tem item(ns) sem preço — corrija a planilha"
- "Confirme os itens sem preço cadastrado antes de prosseguir"

Vale para `cfg.exportMode` em `xlsx`, `db` e `both`.

**Modal de confirmação** (clica "Confirmar e prosseguir"):

> Você está confirmando que **N produto(s) sem preço cadastrado no Fire** podem ser importados sem validação. Esta ação será registrada com seu email e horário.
>
> Lista compacta dos itens (descrição + EAN/código).
>
> [Cancelar] [Confirmar]

POST → `/api/imported/{id}/ack-sem-preco`. Sucesso → re-render do banner (vira cinza) + botão libera.

### 3. Endpoint + persistência

**Rota nova:**

```
POST /api/imported/{import_id}/ack-sem-preco
  auth:  require_user
  body:  {} (vazio)
  pre:   portal_status == "parsed"
  ação:  re-roda check_order; coleta itens com price_status="no_price_in_fire"
         (cada um identificado por {ean, product_code, fire_product_id});
         grava sidecar + audit_log; retorna check atualizado
  resp:  {ack_by, ack_at, items_acked: [...], check}
  erros: 404 not_found, 409 wrong_status, 503 fb_unavailable (se check falhar)
```

**Migration de schema** — duas mudanças em `app/persistence/schema_env.py`:

1. Adicionar as colunas no `TABLES_SQL` (`CREATE TABLE IF NOT EXISTS imports (...)`) — vale para DBs novos.
2. Adicionar entradas em `COLUMN_MIGRATIONS` (hoje vazio, `tuple[tuple[str, str, str], ...] = ()`) — vale para DBs existentes. O runner `_apply_column_migrations` em `app/persistence/router.py` já é idempotente via `PRAGMA table_info`.

Colunas:

```sql
sem_preco_ack_by    TEXT          -- email do operador
sem_preco_ack_at    TEXT          -- ISO timestamp UTC
sem_preco_ack_items TEXT          -- JSON: [{ean, product_code, fire_product_id}]
```

**`app/persistence/repo.py`** ganha:
- `set_sem_preco_ack(import_id, by_email, items: list[dict]) -> None`
- `get_import` retorna `sem_preco_ack_by`, `sem_preco_ack_at`, `sem_preco_ack_items` (parsed JSON) no dict.

(Sem `clear_sem_preco_ack`: re-import cria novo `import_id`; cancel/cleanup não tem cenário de uso real para o ack.)

**`_build_preview_payload`** ([app/web/server.py:182](app/web/server.py#L182)) propaga ack pra UI:
```python
"check": {
    ...,
    "sem_preco_ack": {
        "by": entry.sem_preco_ack_by,
        "at": entry.sem_preco_ack_at,
        "items": entry.sem_preco_ack_items,
    } if entry.sem_preco_ack_by else None,
}
```

### 4. Guards server-side (defesa em profundidade)

**`_send_one_to_fire`** ([app/web/server.py:1485](app/web/server.py#L1485)) — após validar `portal_status` e antes de `FirebirdExporter`:

```python
check = check_order(order, env=request_env)
ack_items = entry.get("sem_preco_ack_items") or []
blocked, detail = is_blocking(check, ack_items)
if blocked:
    repo.append_audit(import_id, "send_to_fire_blocked", detail)
    metrics.price_check_blocks_total.labels(reason=...).inc()
    return _FireSendOutcome(False, reason="price_check_failed", http_status=409,
                            detail="Pedido bloqueado: ...")
```

Se `check["available"]` é False (Fire offline / FB não configurado), **não bloqueia** — segue o caminho atual; aviso fica no preview e operador segue por sua conta.

**`_export_one_xlsx`** ([app/web/server.py:1645](app/web/server.py#L1645)) — mesma lógica, audit `xlsx_export_blocked`.

**Batch endpoints** (`/api/batch/send-to-fire`, `/api/batch/export-xlsx`) — bloqueio é por pedido; já agregam `_*Outcome` por item, então erro 409 vira linha de erro no resumo do batch sem afetar os outros pedidos.

### 5. Auditoria + métricas

**Eventos novos em `audit_log`:**

| Evento | Body |
|---|---|
| `sem_preco_acknowledged` | `{user_email, items: [{ean, product_code, fire_product_id}]}` |
| `send_to_fire_blocked` | `{reason, items_mismatch, items_no_price_unacked, items_no_order_price}` |
| `xlsx_export_blocked` | igual ao anterior |

**Métricas em `app/observability/metrics.py`:**

| Métrica | Tipo | Labels |
|---|---|---|
| `portal_price_check_blocks_total` | Counter | `reason` ∈ `price_mismatch`, `missing_order_price`, `no_price_unacked` |
| `portal_price_check_acks_total` | Counter | — |

### 6. Testes

**Novo:** `tests/test_product_check.py`
- `test_price_status_match_exact` — mesmos cents, status `match`.
- `test_price_status_mismatch_one_cent` — diferença de R$ 0,01 → mismatch.
- `test_price_status_mismatch_round_value` — diferença de R$ 1,00 → mismatch.
- `test_price_status_no_price_in_fire_null` — `PRECO_VENDA = None`.
- `test_price_status_no_price_in_fire_zero` — `PRECO_VENDA = 0`.
- `test_price_status_no_order_price` — `unit_price = None`.
- `test_price_status_no_product_match` — produto não achado; status preservado.
- `test_is_blocking_passes_match_only`
- `test_is_blocking_blocks_on_mismatch`
- `test_is_blocking_blocks_on_no_order_price`
- `test_is_blocking_blocks_on_no_price_unacked`
- `test_is_blocking_passes_with_ack_by_ean`
- `test_is_blocking_passes_with_ack_by_code`
- `test_is_blocking_partial_ack_still_blocks` — ack cobre só parte dos no_price.

**Estende:** `tests/test_web_server.py`
- `test_ack_sem_preco_happy` — 200 + persiste sidecar + audit.
- `test_ack_sem_preco_wrong_status` — 409.
- `test_ack_sem_preco_not_found` — 404.
- `test_send_to_fire_blocked_by_mismatch` — 409, audit gravado, FirebirdExporter não chamado.
- `test_send_to_fire_blocked_by_no_price_unacked` — 409.
- `test_send_to_fire_passes_with_ack` — 200.
- `test_send_to_fire_passes_when_check_unavailable` — Fire offline; segue (não bloqueia).
- `test_export_xlsx_blocked_by_mismatch` — 409.
- `test_export_xlsx_passes_with_ack` — 200.

**Estende:** `tests/test_persistence_repo.py`
- `test_set_sem_preco_ack_persists`.
- `test_get_import_returns_ack_fields`.
- `test_column_migration_idempotent` — abre uma DB pré-existente (sem as colunas), roda `_ensure_schema` duas vezes; segunda chamada não falha e colunas existem.

## Ordem de implementação sugerida

1. Schema + repo (colunas em `schema_env.py` `TABLES_SQL` + `COLUMN_MIGRATIONS`; `set_sem_preco_ack` e `get_import` extendido).
2. `product_check.py`: novos campos por item + `price_summary` + `is_blocking`.
3. Endpoint `POST /api/imported/{id}/ack-sem-preco` + propagação no `_build_preview_payload`.
4. Guards em `_send_one_to_fire` e `_export_one_xlsx`.
5. UI: coluna Fire, banner, modal, gating do botão.
6. Auditoria + métricas.
7. Atualiza `docs/ai/modules/erp.md`, `docs/ai/modules/web.md`, `docs/ai/modules/persistence.md` (apenas seções afetadas).

## Riscos e mitigações

- **Float drift na comparação** → mitigado por comparação em cents (`round(x * 100)` → int).
- **Bypass via DevTools** → mitigado por guard server-side (defesa em profundidade).
- **Cadastro corrigido entre commit e envio** → re-check no envio passa naturalmente (não precisa reimportar).
- **Fire offline no momento do envio** → não bloqueia (preserva comportamento atual; check é best-effort). Endpoint de ack, em contraste, devolve 503: precisa do check para registrar quais itens foram cobertos.
- **Ack de pedido reimportado é stale** → re-import gera novo `import_id`, então o problema não existe; o ack vive no entry antigo como audit histórico.
