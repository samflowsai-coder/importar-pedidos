# Spec — Poll de comandos + sync de catálogo on-demand (lado Importador)

- **Data:** 2026-07-04
- **Status:** Design aprovado. **Não** aprovado para implementação — construir depois ("vou tocar isso depois").
- **Tipo:** Spec de feature (lado Python/on-prem). Implementa contra o **contrato canônico do Flow**: `../../../../GestorProduction/pcp-app/docs/superpowers/specs/2026-07-04-forcar-sync-produtos-fire-design.md`.
- **Origem:** brainstorming com o fundador (2026-07-03/04). O Flow define o contrato; este projeto implementa o cliente — mesmo padrão da Fatia G / catálogo.

---

## 1. Objetivo

O planejador clica um botão **no Flow** e o catálogo do Fire sincroniza. Como este serviço é **on-prem atrás do firewall**, o Flow não nos chama — nós **perguntamos** a ele. Já perguntamos decisões de pedido a cada 30s; vamos perguntar **comandos** também, e ao ver `sync_catalogo`, disparar o promote **que já existe** aqui.

Chave do design: **o trabalho novo aqui é mínimo** porque a máquina já existe:
- Poll com cursor + APScheduler já roda (`poll_decisoes`).
- O promote de catálogo já roda (`run_catalogo_sync(dry_run=False)`).
- Falta só: um segundo poll (`comandos`) + um handler que chama o promote com o `comandoId`.

## 2. Estado atual (verificado no código)

| Peça | Estado | Onde |
|---|---|---|
| Worker APScheduler + poll a cada 30s | ✅ existe | `app/worker/scheduler.py:32` (`_FLOWPCP_INTERVAL_S = 30`), `:80` (`add_job(run_poll_flowpcp, "interval", seconds=30)`) |
| Poll de decisões (cursor, idempotente) | ✅ existe (padrão a espelhar) | `app/worker/jobs/poll_flowpcp.py:45`, `app/integrations/flowpcp/poll_decisoes.py:124` |
| Cliente HTTP Flow (`list_decisoes`, `send_catalogo`) | ✅ existe | `app/integrations/flowpcp/client.py` |
| **Promote de catálogo** `run_catalogo_sync(slug, dry_run, full_sync)` | ✅ existe | `app/integrations/flowpcp/catalogo_sync.py:27-71` |
| Extract do Firebird | ✅ existe | `app/erp/catalog_extract.py` (`extract_produtos`) |
| Map p/ request do contrato | ✅ existe | `app/integrations/flowpcp/catalogo_mapper.py:2-26` (`build_catalogo_request`) |
| CLI manual `--apply` (dryRun=false) | ✅ existe | `tools/sync_catalogo_fire.py:22-40` |
| Config por ambiente (base_url, token, tenant, poll_interval_s) | ✅ existe | `app/integrations/flowpcp/config.py:10-33` |
| **Poll de comandos** | ❌ não existe | — |
| **Handler `sync_catalogo` → promote** | ❌ não existe | — |
| `comandoId` propagado no request de catálogo | ❌ não existe | — |

> Nota de dependência: o promote só **grava** quando o **Flow** destravar o `422` (Fatia 1 do spec do Flow). Até lá, `--apply`/handler recebem `422` e o comando fica `pendente` (re-lido, idempotente). Construir este lado **em paralelo** é seguro; **ativar** o handler só faz sentido depois da Fatia 1 do Flow.

## 3. Contrato que consumimos (definido pelo Flow)

- **`GET /api/portal-pedidos/comandos?cursor=&limit=`** — headers `X-Service-Token` + `X-Tenant-Id`.
  Resposta: `{ comandos: [{ id, tipo, payload, solicitado_em }], proximo_cursor }`. Só devolve `status='pendente'`.
- **`POST /api/portal-pedidos/catalogo`** — já usamos; ganha campo **opcional** `comandoId` no corpo `catalogo.produtos.v1`. Ao concluir o promote com `comandoId`, o Flow marca o comando `concluido` + grava o `resultado`. **Nós não precisamos de endpoint de ack** — o próprio `POST /catalogo` fecha o loop.

## 4. Mudanças (4 pontos pequenos)

### 4.1 Cliente — `list_comandos`
`app/integrations/flowpcp/client.py`: novo método espelhando `list_decisoes`:
```
def list_comandos(self, cursor: str | None, limit: int = 50) -> ComandosResponse
    # GET /api/portal-pedidos/comandos?cursor=&limit=
```

### 4.2 Job — `poll_comandos`
Novo job espelhando `poll_decisoes` (mesmo padrão cursor/watermark; ver `poll_decisoes.py:124`):
- `app/worker/jobs/poll_comandos.py` → `run_poll_comandos()`.
- `app/integrations/flowpcp/poll_comandos.py` → `poll_comandos_once(client, handler, config)`:
  1. `list_comandos(cursor)`.
  2. Para cada comando `tipo == "sync_catalogo"` → chamar o handler (§4.3).
  3. Avança o cursor por `solicitado_em` (padrão de `poll_decisoes`).
- **Não** confirmar/consumir explicitamente: o comando sai do conjunto quando o Flow o marca `concluido` (via o `POST /catalogo` com `comandoId`). Se o promote falhar/`422`, o comando segue `pendente` → re-tentado no próximo poll (idempotente).

### 4.3 Handler — `sync_catalogo` → promote
Reusa `run_catalogo_sync`, passando o `comandoId` adiante:
```
def handle_sync_catalogo(comando, config):
    run_catalogo_sync(config.slug, dry_run=False, full_sync=True, comando_id=comando.id)
```
- `run_catalogo_sync` (`catalogo_sync.py:27`) ganha param `comando_id: str | None = None`.
- Repassa a `build_catalogo_request(..., comando_id=comando_id)` (`catalogo_mapper.py`) → inclui `comandoId` no corpo.
- Serialização: **um promote por vez** (o scheduler já é serial por job). Evitar rodar dois promotes concorrentes — se um poll pegar o comando enquanto outro roda, o segundo é no-op idempotente no Flow, mas idealmente um lock local simples (flag em memória do worker) evita trabalho duplo.

### 4.4 Scheduler — registrar o job
`app/worker/scheduler.py` (perto do `:80`): `add_job(run_poll_comandos, "interval", seconds=<intervalo>)`.
- **Intervalo:** 60–120s (comando é raro/on-demand; não precisa dos 30s do decisões). Sugestão 120s.
- Tornar configurável via env (`COMANDOS_POLL_INTERVAL_S`), no mesmo espírito de expor `_FLOWPCP_INTERVAL_S` — hoje hardcoded (`scheduler.py:32`).

## 5. Config
- Reusa `app/integrations/flowpcp/config.py` (base_url, `service_token`, `tenant_id`, `slug`). Sem credencial nova.
- Novo (opcional): `COMANDOS_POLL_INTERVAL_S` (default 120).
- Gate real do promote é do lado Flow (`catalogoFireAtivo` + destravar o `422`). Aqui, se o comando não vier, nada roda.

## 6. Resiliência & idempotência
- Comando `pendente` até o `POST /catalogo` com `comandoId` concluir no Flow → blip de rede re-tenta, `ON CONFLICT` no Flow torna o promote idempotente.
- `full_sync=True` sempre (catálogo inteiro; ~3421 é lote único ou paginado como o `--apply` já faz).
- Falha de extract do Firebird → logar, **não** derrubar o worker; comando segue pendente pro próximo ciclo.

## 7. Testes (pytest, padrão do projeto)
- `test_flowpcp_catalogo_client` (existente) — estender: `list_comandos` parseia resposta + cursor.
- Novo `test_poll_comandos` — comando `sync_catalogo` dispara o handler; tipo desconhecido é ignorado; cursor avança.
- `test_flowpcp_catalogo_mapper` (existente) — estender: `comandoId` entra no request quando presente; ausente ⇒ campo omitido (retrocompat).
- Handler — `run_catalogo_sync` chamado com `dry_run=False, full_sync=True, comando_id`.

## 8. Fora de escopo
- Cron local de catálogo (o gatilho é o comando do Flow; recorrência, se quisermos, é do lado Flow enfileirando comando).
- Ack intermediário "em andamento" (só se a UI do Flow precisar; v1 usa pendente→concluido).
- Composição de kit / incremental (Fase 2 do spec-mãe do Flow).

## 9. Ordem de construção
1. Flow: Fatia 1 (destravar promote com bootstrap) — pré-requisito real.
2. Aqui: 4.1 → 4.3 (cliente + mapper + handler, testáveis isolados sem o Flow via mock).
3. Aqui: 4.2 + 4.4 (job + scheduler) — liga o poll.
4. Flow: Fatia 2 (tabela de comando + `GET /comandos` + botão) — fecha a ponta.

Passos 2 dá pra fazer com mock antes do Flow existir; **ligar** (3/4) só depois da Fatia 1 do Flow.
