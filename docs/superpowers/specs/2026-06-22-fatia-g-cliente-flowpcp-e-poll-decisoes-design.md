# Importador — Fatia G: Cliente FlowPCP + poll de decisões + reconciliação Fire

**Data:** 2026-06-22
**Domínio:** `integrations/flowpcp` + `erp` (Fire) + `web` (UI config) + `persistence` (outbox + SQLite mapping) + `observability`
**Status:** Aprovado — Modelo B (OVERLAY) confirmado por Samuel em 2026-06-26. Ver Addendum.
**Spec irmã (FlowPCP):** `pcp-app/docs/superpowers/specs/2026-06-22-fatia-g-importador-ponte-flowpcp-design.md`

---

## Addendum de implementação (2026-06-26)

Samuel confirmou o **Modelo B (OVERLAY)**: o pedido vai pro FIRE de imediato (fluxo XLS de hoje) **e** pro FlowPCP em paralelo; o FlowPCP só **reconcilia a DATA** (`prazo_pactuado`) de volta no FIRE após renegociação. (O Modelo A "gate" — gravar no FIRE só após aprovação — foi descartado.)

Três reconciliações desta sessão, que **prevalecem** sobre o corpo do draft onde divergirem:

1. **Auth = `X-Service-Token`** (header), NÃO `Authorization: Bearer`. Os endpoints F.5 **implementados** usam `X-Service-Token` + `timingSafeEqual`; os endpoints novos da G (`/decisoes`, `/confirmar-reconciliacao`) seguirão o mesmo. Onde o corpo desta spec disser "Bearer", leia `X-Service-Token`.

2. **SQL do FIRE contra o schema REAL.** O `fire_update.update_data_entrega` será construído sobre o que `app/erp/` (queries.py / mapper.py / connection.py) já conhece do Firebird da MM — não sobre o `UPDATE CAB_VENDAS` chutado no corpo. Validar nomes reais de tabela/coluna antes de cravar.

3. **Dependência: endpoints `/decisoes` e `/confirmar-reconciliacao` NÃO existem no FlowPCP ainda.** Esta fatia (lado Importador) é construída e testada contra o **contrato travado** (HTTP mockado, TDD). A integração viva (E2E) fica pendente do lado Flow ser implementado — frente separada. A "Migration 0083 — colunas de reconciliação" proposta na spec irmã **colide** com a `0083_clientes_unique_cnpj` já aplicada no dev (2026-06-26); o lado Flow renumera pra 0084+.

Contrato canônico dos 2 endpoints: §5.1 e §5.2 da spec irmã.

---

## Problema

A **Fatia F.5** entregou o endpoint `POST /api/portal-pedidos/recebimento` no FlowPCP para receber pedidos do Importador. **A pasta `app/integrations/flowpcp/` no Importador está vazia** (PRD v0.3 §5.4) — não existe cliente HTTP capaz de empurrar pedidos pra lá. Pedido novo chega no Importador, vai pro Fire via XLS, mas nunca aparece no Flow.

Além disso, o operador no Flow pode **renegociar a data** de um pedido (Fatia F.5 já implementa `prazo_pactuado`). Quando isso acontece, **o Fire continua com a data velha** e a NF do Rafael sai errada. Precisa de uma ponte reversa Flow → Fire — e essa ponte **só pode ser o Importador** (princípio fundador ADR-003: Flow nunca conecta direto no Firebird).

A **Fatia G** fecha esse ciclo construindo o cliente Python completo + job de poll + lógica de UPDATE no Fire.

## Objetivo

Implementar 3 capacidades no Importador:

1. **Push de pedido novo:** ao terminar parse de XLS/PDF de fornecedor, enviar o `Order` pro FlowPCP em paralelo ao fluxo XLS-para-Fire existente.
2. **Pull de decisões:** job APScheduler 30s consulta `GET /api/portal-pedidos/decisoes` e processa cada decisão pendente.
3. **Reconciliação Fire:** quando decisão indica nova data, executar `UPDATE CAB_VENDAS SET DATA_ENTREGA=? WHERE NUM_PEDIDO=? AND CNPJ_CLIENTE=?` em transação curta, depois confirmar de volta no Flow via `POST /api/portal-pedidos/decisoes/{id}/confirmar-reconciliacao`.

Operador deve poder ligar/desligar Gateway FlowPCP via UI de configuração existente.

## Não-objetivos (YAGNI)

- ❌ Cancelamento automático no Fire (`UPDATE` de status / `DELETE`). Operador faz manual; Importador apenas alerta.
- ❌ Update de outros campos do Fire além de `DATA_ENTREGA` (preço, quantidade, qualquer outro).
- ❌ Reconciliação bidirecional além desse 1 caso (Flow → Fire para `DATA_ENTREGA`). Sync arbitrário fica para fatias futuras.
- ❌ Re-parse automático quando decisão chega (decisão atualiza Fire; não re-importa pedido).
- ❌ Modo offline (rede caiu) com fila persistente além do outbox que já existe.
- ❌ Multi-tenant no Importador — cada instalação atende 1 tenant.

## Regras

| Situação | Comportamento |
|---|---|
| Pedido novo parseado com sucesso | Tenta `send_order` para FlowPCP em paralelo ao XLS pro Fire. Falha de rede → outbox retenta. |
| Modo `flowpcp.enabled=false` no `config.json` | Não envia, não polla. Fluxo XLS-Fire segue normal. |
| Modo `flowpcp.dry_run=true` | Polla decisões, simula UPDATE no Fire (log apenas), confirma de volta no Flow com `acao="data_atualizada"` mas **não toca no Fire**. Útil 1ª semana. |
| Decisão `acao=data_atualizada` chegou | `UPDATE CAB_VENDAS SET DATA_ENTREGA=? WHERE NUM_PEDIDO=? AND CNPJ_CLIENTE=?`. Se OK → confirma `data_atualizada`. Se SQL falhou (timeout/lock) → retenta 3x, depois confirma `pedido_nao_encontrado_no_fire` e alerta. |
| Decisão `status=rejeitado` chegou | NÃO toca no Fire. Loga + alerta humano. Confirma de volta `acao=cancelamento_pendente_manual`. |
| Decisão `prazo_pactuado == prazo_entrega_original` (nada mudou) | Confirma `acao=sem_acao_necessaria` sem executar SQL. |
| Pedido no Flow não localizado no Fire (SELECT zero rows) | Incrementa contador local; confirma `acao=pedido_nao_encontrado_no_fire`. Após 5 polls retentando, marca como erro permanente e alerta humano. |
| Idempotência | Toda chamada usa `Idempotency-Key` header (UUID derivado de `pedido_id + acao`). Re-tentativa não duplica side-effect. |
| Auth | `Authorization: Bearer <flowpcp.service_token>` em todas as chamadas. Token vem do `config.json` (encrypted via secret_store existente). |

Todas as datas em ISO 8601 com timezone (`prazo_entrega` e `prazo_pactuado` vêm do Flow como UTC; converter para timezone local antes do UPDATE no Fire). O Fire armazena DATE (sem timezone). Política: usar a data calendar do timezone configurado em `config.json` (default `America/Sao_Paulo`).

## Componentes

### 1. `app/integrations/flowpcp/client.py` — HTTP client base

```python
# Pseudo-código — implementador refina contra padrão usado em sync_products
# (se existir; senão, espelhar app/integrations/apontae se houver)

from dataclasses import dataclass
from typing import Optional, Iterator
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

@dataclass(frozen=True)
class FlowPCPConfig:
    base_url: str           # ex: "https://flowpcp.fly.dev"
    service_token: str      # Bearer token
    tenant_id: str          # UUID
    timezone: str = "America/Sao_Paulo"
    dry_run: bool = False
    request_timeout_s: float = 30.0

class FlowPCPClient:
    def __init__(self, config: FlowPCPConfig):
        self._config = config
        self._http = httpx.Client(
            base_url=config.base_url,
            timeout=httpx.Timeout(config.request_timeout_s),
            headers={
                "Authorization": f"Bearer {config.service_token}",
                "X-Tenant-Id": config.tenant_id,
                "Content-Type": "application/json",
            },
        )

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10))
    def send_order(self, payload: dict, idempotency_key: str) -> dict:
        """Empurra pedido novo. POST /api/portal-pedidos/recebimento.
        Schema do payload definido no spec da Fatia F."""
        r = self._http.post(
            "/api/portal-pedidos/recebimento",
            json=payload,
            headers={"Idempotency-Key": idempotency_key},
        )
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10))
    def list_decisoes(self, cursor: Optional[str] = None, limit: int = 50) -> dict:
        """GET /api/portal-pedidos/decisoes?cursor=...&limit=...
        Devolve { decisoes: [...], proximo_cursor: str | null }."""
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        r = self._http.get("/api/portal-pedidos/decisoes", params=params)
        r.raise_for_status()
        return r.json()

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=1, min=2, max=10))
    def confirmar_reconciliacao(
        self,
        decisao_id: str,
        acao: str,
        fire_id_externo: Optional[str] = None,
        observacoes: Optional[str] = None,
    ) -> dict:
        """POST /api/portal-pedidos/decisoes/{id}/confirmar-reconciliacao."""
        body = {"acao": acao}
        if fire_id_externo:
            body["fire_id_externo"] = fire_id_externo
        if observacoes:
            body["observacoes"] = observacoes
        r = self._http.post(
            f"/api/portal-pedidos/decisoes/{decisao_id}/confirmar-reconciliacao",
            json=body,
            headers={"Idempotency-Key": f"reconciliar-{decisao_id}-{acao}"},
        )
        # 409 (já reconciliado com ação divergente) é tratado pelo caller
        if r.status_code == 409:
            return {"conflict": True, "details": r.json()}
        r.raise_for_status()
        return r.json()

    def close(self):
        self._http.close()
```

**Decisões de implementação:**

- Token, base_url, tenant_id vêm do `config.json` (encrypted via `secret_store.py`).
- Retry exponencial com 3 tentativas para falhas transitórias (rede, 5xx). 4xx não retentam.
- 409 (conflito de idempotência em `confirmar_reconciliacao`) é caso de negócio — logar e alertar, não exception.
- `Idempotency-Key` em todas as escritas — Flow já valida (F.5).

### 2. `app/integrations/flowpcp/exporter.py` — push de pedido novo

Chamado no fim do pipeline de parsing (depois que `Order` está montado e validado).

**Lógica:**

1. Verifica `config.flowpcp.enabled` — se false, no-op.
2. Monta payload conforme contrato F.5 (campos: `tenant_id`, `pedido_erp`, `produto_match` ou `produto_codigo`, `cliente_cnpj`, `cliente_nome`, `quantidade`, `prazo_entrega`, `prioridade`, `observacoes`, `fonte`, `source_id_externo`).
3. Gera `idempotency_key = f"send-{pedido_erp}-{source_id_externo}"`.
4. Chama `client.send_order(payload, idempotency_key)`.
5. Se exception (rede caiu, retries esgotados) → grava em `outbox` table para `drain_outbox` retentar.

**Outbox:**

A tabela `outbox` já existe (PRD §5.4). Acrescentar campo `kind` (TEXT) se ainda não existe, com valores `"send_order"` e `"confirmar_reconciliacao"`. Worker `drain_outbox` (15s, já existe) processa retries.

### 3. `app/integrations/flowpcp/poll_decisoes.py` — job APScheduler

Novo job rodando a cada 30 segundos.

```python
from datetime import datetime
import sqlite3
from app.integrations.flowpcp.client import FlowPCPClient
from app.erp.fire_update import update_data_entrega
from app.persistence.flowpcp_mapping import (
    get_last_cursor, save_last_cursor,
    register_attempt, get_attempts_count
)

def job_poll_flowpcp_decisoes(client: FlowPCPClient, fire_conn, config):
    """Roda a cada 30s. Pula decisões do Flow e age conforme."""
    if not config.flowpcp.enabled:
        return

    cursor = get_last_cursor()
    response = client.list_decisoes(cursor=cursor, limit=50)
    decisoes = response.get("decisoes", [])

    for d in decisoes:
        try:
            processar_decisao(d, client, fire_conn, config)
        except Exception as e:
            # log + continua próxima decisão
            log_error(f"Erro processando decisão {d['id']}: {e}")

    # Salva cursor pra próxima rodada
    proximo = response.get("proximo_cursor")
    if proximo:
        save_last_cursor(proximo)

def processar_decisao(d: dict, client: FlowPCPClient, fire_conn, config):
    decisao_id = d["id"]
    status = d["status"]
    prazo_original = d["prazo_entrega_original"]
    prazo_pactuado = d.get("prazo_pactuado")
    pedido_erp = d["pedido_erp"]
    cliente_cnpj = d.get("cliente_cnpj")

    # Caso 1: rejeitado
    if status == "rejeitado":
        log_warn(f"Decisão {decisao_id} rejeitada — cancelamento manual necessário no Fire (pedido_erp={pedido_erp})")
        client.confirmar_reconciliacao(
            decisao_id,
            acao="cancelamento_pendente_manual",
            observacoes=d.get("motivo_decisao"),
        )
        return

    # Caso 2: aprovado, mas prazo não mudou
    if prazo_pactuado is None or prazo_pactuado == prazo_original:
        client.confirmar_reconciliacao(decisao_id, acao="sem_acao_necessaria")
        return

    # Caso 3: aprovado com data nova — UPDATE no Fire
    if config.flowpcp.dry_run:
        log_info(f"[DRY_RUN] UPDATE CAB_VENDAS SET DATA_ENTREGA={prazo_pactuado} WHERE NUM_PEDIDO={pedido_erp} AND CNPJ_CLIENTE={cliente_cnpj}")
        client.confirmar_reconciliacao(
            decisao_id, acao="data_atualizada",
            fire_id_externo=pedido_erp,
            observacoes="DRY_RUN (sem escrita real no Fire)",
        )
        return

    try:
        rows_affected = update_data_entrega(
            fire_conn, pedido_erp, cliente_cnpj,
            new_date=prazo_pactuado,
            timezone=config.flowpcp.timezone,
        )
    except Exception as e:
        # Pode ser timeout/lock — outbox tenta de novo no próximo poll
        log_error(f"UPDATE Fire falhou para decisao={decisao_id}: {e}")
        return  # NÃO confirma — Flow vai re-enviar na próxima

    if rows_affected == 0:
        attempts = get_attempts_count(decisao_id) + 1
        register_attempt(decisao_id, attempts)
        if attempts >= 5:
            log_critical(f"Pedido {pedido_erp} não localizado no Fire após {attempts} tentativas")
            client.confirmar_reconciliacao(
                decisao_id, acao="pedido_nao_encontrado_no_fire",
                observacoes=f"{attempts} tentativas",
            )
        # Senão deixa pendente — tenta de novo
        return

    client.confirmar_reconciliacao(
        decisao_id, acao="data_atualizada",
        fire_id_externo=pedido_erp,
        observacoes=f"UPDATE OK (rows={rows_affected})",
    )
```

### 4. `app/erp/fire_update.py` — UPDATE no Fire

```python
import firebird.driver
from datetime import datetime, date
from zoneinfo import ZoneInfo

def update_data_entrega(
    conn: firebird.driver.Connection,
    pedido_erp: str,
    cliente_cnpj: str | None,
    new_date: str,                  # ISO 8601 com TZ
    timezone: str = "America/Sao_Paulo",
) -> int:
    """Faz UPDATE CAB_VENDAS SET DATA_ENTREGA=?. Devolve rows affected."""
    tz = ZoneInfo(timezone)
    dt = datetime.fromisoformat(new_date.replace("Z", "+00:00"))
    fire_date: date = dt.astimezone(tz).date()

    cur = conn.cursor()
    try:
        # Limpar CNPJ pra match (Fire armazena com ou sem máscara — testar com dado real)
        cnpj_clean = re.sub(r"[^\d]", "", cliente_cnpj or "")
        cur.execute(
            """
            UPDATE CAB_VENDAS
               SET DATA_ENTREGA = ?
             WHERE NUM_PEDIDO = ?
               AND (CNPJ_CLIENTE = ? OR REPLACE(REPLACE(REPLACE(CNPJ_CLIENTE,'.',''),'/',''),'-','') = ?)
            """,
            (fire_date, pedido_erp, cliente_cnpj, cnpj_clean),
        )
        rows = cur.rowcount
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
```

**Decisões:**

- Match por `NUM_PEDIDO + CNPJ_CLIENTE` para evitar colisão de números entre clientes.
- Tolerância ao formato do CNPJ (com/sem máscara) usando OR + REPLACE no SQL (Firebird suporta).
- Commit imediato (transação curta) — minimiza risco de lock prolongado.
- Rollback em qualquer exception — não deixa transação aberta.
- O SQL exato precisa ser **validado com dump real do Fire da MM** antes de produção. Esta spec assume `CAB_VENDAS.NUM_PEDIDO + CNPJ_CLIENTE` mas o schema pode ser ligeiramente diferente (ver `app/erp/` no codebase atual). Confirmar na fase de plano.

### 5. `app/persistence/flowpcp_mapping.py` — SQLite local

Nova tabela em `data/imports.db` (SQLite local que o Importador já usa):

```sql
CREATE TABLE IF NOT EXISTS flowpcp_decisoes_mapping (
  decisao_id        TEXT PRIMARY KEY,
  pedido_erp        TEXT NOT NULL,
  cliente_cnpj      TEXT,
  acao_executada    TEXT,                 -- last action taken (data_atualizada, etc)
  attempts          INTEGER NOT NULL DEFAULT 0,
  reconciliado_em   TIMESTAMP,
  criado_em         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  atualizado_em     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS flowpcp_cursor_state (
  id                INTEGER PRIMARY KEY CHECK (id = 1),
  last_cursor       TEXT,
  atualizado_em     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

Funções:

```python
def get_last_cursor() -> str | None:
    """Devolve o último cursor processado, ou None na primeira execução."""

def save_last_cursor(cursor: str) -> None:
    """Atualiza cursor após processar batch de decisões."""

def register_attempt(decisao_id: str, attempts: int) -> None:
    """Incrementa contador de tentativas pra uma decisão."""

def get_attempts_count(decisao_id: str) -> int:
    """Devolve tentativas acumuladas (0 se nunca tentou)."""

def mark_reconciliada(decisao_id: str, acao: str) -> None:
    """Marca decisão como reconciliada no Importador (não impede re-tentativa pelo Flow)."""
```

### 6. UI de configuração — toggle Gateway FlowPCP

A UI do Importador já tem `secret_store.py` + UI FastAPI para configurar Fire (PRD §5.4 — "Auth na config UI: Senha encrypted via cryptography v42+"). Adicionar nova seção:

```
┌── Gateway FlowPCP ──────────────────────────────────────────┐
│ [ ] Habilitar Gateway FlowPCP                               │
│                                                             │
│ URL base:    [ https://flowpcp.fly.dev                    ] │
│ Token:       [ ●●●●●●●●●●●●●●●●●●●●●●●●●● ] [Mostrar]      │
│ Tenant ID:   [ 1798c3c5-0fb6-4edb-a523-e13fb5bf52a0       ] │
│ Timezone:    [ America/Sao_Paulo                        ▼ ] │
│ [x] Modo dry-run (primeira semana — não escreve no Fire)    │
│                                                             │
│ [Testar conexão]  [Salvar]                                  │
└─────────────────────────────────────────────────────────────┘
```

- Token criptografado via `secret_store.py` (mesmo padrão da senha Fire).
- Endpoint "Testar conexão" faz GET `/api/portal-pedidos/decisoes?limit=1` e mostra OK / erro.
- Mudança de config via UI dispara reload do `FlowPCPClient` (sem precisar restart).

### 7. Integração com APScheduler existente

Adicionar job no scheduler que já roda os jobs existentes (`scan_environments`, `drain_outbox`, `poll_fire`, `retention`):

```python
# app/scheduler.py (alteração)

scheduler.add_job(
    job_poll_flowpcp_decisoes,
    "interval",
    seconds=30,
    args=[flowpcp_client, fire_conn, config],
    id="poll_flowpcp_decisoes",
    max_instances=1,         # nunca paralelo
    coalesce=True,           # se atrasou, processa só 1 vez
)
```

### 8. `outbox` integration para `send_order`

O outbox existente persiste retries. Acrescentar `kind` se ainda não existe:

```sql
ALTER TABLE outbox ADD COLUMN kind TEXT NOT NULL DEFAULT 'send_order';
-- valores possíveis: 'send_order', 'confirmar_reconciliacao'
```

Worker `drain_outbox` despacha por `kind`:

```python
def drain_outbox_handler(row):
    if row["kind"] == "send_order":
        flowpcp_client.send_order(json.loads(row["payload"]), row["idempotency_key"])
    elif row["kind"] == "confirmar_reconciliacao":
        body = json.loads(row["payload"])
        flowpcp_client.confirmar_reconciliacao(
            body["decisao_id"], body["acao"],
            fire_id_externo=body.get("fire_id_externo"),
            observacoes=body.get("observacoes"),
        )
```

## Ordem de implementação sugerida

1. **Migration SQLite local** — `flowpcp_decisoes_mapping` + `flowpcp_cursor_state` + ALTER `outbox.kind`. Smoke: SQL CRUD via pytest.
2. **`FlowPCPClient` base** — auth, retry, 3 métodos (send_order, list_decisoes, confirmar_reconciliacao). Smoke: contra um mock httpx + contrato Pydantic dos retornos.
3. **`exporter.py`** — push de pedido novo + outbox enqueue em falha. Smoke: chama em um Order real, vê chegando no Flow.
4. **`fire_update.py`** — UPDATE em CAB_VENDAS. Smoke: contra Firebird de teste (dump da MM), validar rows_affected.
5. **`poll_decisoes.py`** — orquestração + 5 casos (rejeitado, sem mudança, com data, dry_run, pedido_nao_encontrado). Smoke: vitest contra mock + integração com mapping table.
6. **UI de config** — toggle, secret_store, testar conexão.
7. **Wire no scheduler** — job 30s + drain_outbox handler.
8. **E2E manual** — `dry_run=true` por 1 semana, depois cutover.

## Testes

- **Unit:** `FlowPCPClient` métodos (mock httpx), `fire_update.update_data_entrega` (mock Firebird connection), `processar_decisao` (5 ramos + edge cases).
- **Integration:** Banco SQLite real + mock HTTP server respondendo conforme contrato F+G.
- **E2E manual:** Pedido fictício importado → vê em `/pedidos/pendentes` no Flow → operador renegocia data → Importador detecta em 30s → UPDATE rola no Firebird de homologação → confirma de volta → linha some de decisões pendentes.
- **Smoke do contrato:** Testar 1x contra dev real do Flow (`flowpcp.fly.dev`) com pedido descartável.

## Riscos e mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Token Bearer vazado nos logs | Média | Alto | `service_token` nunca aparece em log; redact no httpx client (hook `event_hooks`) |
| UPDATE no Fire bloqueia outra transação | Média | Médio | Transação curta + retry 3x; falha persistente → confirma `pedido_nao_encontrado` + alerta |
| Cursor perdido (SQLite corrompeu) | Baixa | Médio | Backup do `imports.db` no `retention` job; recuperação manual: refaz cursor com `null` e re-processa idempotentemente (Flow é idempotent) |
| Decisão processada 2x | Baixa | Baixo | Idempotency-Key + Flow valida (409 em ação divergente) |
| Schema `CAB_VENDAS` da MM diverge do esperado | Média | Alto | Validar SQL com dump real ANTES do cutover; pode haver tabela diferente (TRANSACAO_VENDA, etc) — confirmar com Samuel |
| Polling muito agressivo derruba rede on-prem | Baixa | Médio | 30s é conservador; se virar problema, aumentar pra 60s sem mudar lógica |
| Dry-run esquecido em produção | Baixa | Alto | UI mostra badge visível "DRY-RUN ATIVO" + log estruturado a cada poll |

## Dependências (cross-ref)

- **Spec irmã FlowPCP:** `pcp-app/docs/superpowers/specs/2026-06-22-fatia-g-importador-ponte-flowpcp-design.md`
  - Endpoint `POST /api/portal-pedidos/recebimento` (Fatia F.5, já existe)
  - Endpoint `GET /api/portal-pedidos/decisoes` (Fatia G, **novo**)
  - Endpoint `POST /api/portal-pedidos/decisoes/{id}/confirmar-reconciliacao` (Fatia G, **novo**)
  - Schemas Zod canônicos definidos nessa spec — Pydantic deste lado é refletido dela.
- **PRD v0.3 §6.G** — fonte de verdade arquitetural.
- **ADR-003** — Importador é única ponte com Fire (princípio fundador, nunca violar).

## Pré-requisitos antes da execução

1. **Fatia F.5 mergeada em main no FlowPCP** + endpoints novos da G implantados (idealmente nesta ordem: Flow merge primeiro, Importador implementa contra produção).
2. **Token Bearer rodado** e propagado pro Importador via UI de config.
3. **Dump do Fire da MM** disponível para validar SQL `UPDATE CAB_VENDAS`.
4. **Plano de execução** via `superpowers:writing-plans` — break em ≤ 6 tasks bite-sized.

---

_Fim da spec. Próximo passo: revisão Samuel → ajustes → criar plano de execução em sessão dedicada no Importador._
