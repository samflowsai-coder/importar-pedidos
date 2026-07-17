# Design — Carga de clientes ativos Fire → Flow

**Data:** 2026-07-17
**Domínio:** integrations/flowpcp + erp + persistence + web
**Status:** aprovado (brainstorming) — pronto para plano de implementação

---

## Contexto e problema

Hoje **não existe** ponte de cadastro de clientes entre Fire → Portal de Pedidos → Flow.
O que existe:

1. **Sync de catálogo/produtos** — `app/integrations/flowpcp/catalogo_sync.py`, Fire → Flow.
2. **Envio de pedido** — `app/integrations/flowpcp/mapper.py:45` embute o cliente no pedido
   (`ClienteRecebimento{nome, cnpj}`); o Flow auto-cria/casa o cliente **por CNPJ** no
   recebimento (`resolver-cliente.ts`: `if (!cnpj) return null`).

Consequência: um cliente só "existe" no Flow como efeito colateral de um pedido chegar. Não
há carteira pré-populada, nome canônico, agrupamento por marca, nem visão de clientes ativos.

**Objetivo (todos os 4 confirmados pelo dono do produto):** carregar no Flow, de forma
proativa, o cadastro **curado** dos clientes **ativos** — para (a) cadastro curado (nome
canônico + grupo/marca), (b) visão da carteira, (c) menos atrito operacional, (d) evitar
duplicidade.

Ver memória: `project_flow_cliente_cnpj.md` (Flow casa cliente só por CNPJ),
`project_flowpcp_fatia_g_status.md` (infra da ponte), `project_firebird_schema_reality.md`.

---

## Decisões de produto (fechadas no brainstorming)

| Decisão | Escolha | Razão |
|---|---|---|
| **Identidade** | 1 CNPJ = 1 cliente | Casa 1:1 com o CNPJ que vem no pedido (runtime). A marca (`CODGRUPO`) viaja como campo `grupoCodigo` só para agrupar em relatório — não funde identidades. Rollup por marca como identidade fica para o futuro. |
| **Escopo / "ativo"** | Tem pedido no Fire nos últimos **12 meses** | Janela fixa (sem campo de config — YAGNI). Pega sazonalidade inteira. |
| **Cadência** | Sob demanda + gate, começando manual | Espelha o `catalogo_sync`. Agendamento é follow-on trivial. |
| **Direção** | Importador (este repo) empurra ao Flow | Reusa a infra `FlowPCPClient` + gate por ambiente já provada no catálogo. |

---

## Arquitetura — espelho do `catalogo_sync`, arquivo por arquivo

O design é deliberadamente o gêmeo do sync de catálogo. Não introduz padrão novo.

| Catálogo (existe) | Clientes (novo) |
|---|---|
| `app/erp/catalog_extract.py` | `app/erp/cliente_extract.py` → `ClienteFireDTO` + `extract_clientes_ativos()` |
| `queries.LIST_PRODUTOS_CATALOGO` | `queries.LIST_CLIENTES_ATIVOS` (janela 12m) |
| `app/persistence/catalogo_fire_repo.py` | `app/persistence/clientes_fire_repo.py` (tabela `clientes_fire`) |
| `app/persistence/schema_env.py` (DDL `catalogo_fire`) | `schema_env.py` (DDL `clientes_fire`) |
| `app/integrations/flowpcp/catalogo_schema.py` | `app/integrations/flowpcp/clientes_schema.py` |
| `app/integrations/flowpcp/catalogo_mapper.py` | `app/integrations/flowpcp/clientes_mapper.py` |
| `client.send_catalogo()` + `_CATALOGO_PATH` | `client.send_clientes()` + `_CLIENTES_PATH` |
| `app/integrations/flowpcp/catalogo_sync.py` | `app/integrations/flowpcp/clientes_sync.py` → `run_clientes_sync(slug)` |
| gate `flowpcp_catalogo_push` (config.py + coluna env) | gate `flowpcp_clientes_push` (config.py + coluna env) |
| botão em `routes_environments.py` | botão "Sincronizar clientes" em `routes_environments.py` |

**Assinatura do orquestrador** (paralela a `run_catalogo_sync`):

```python
def run_clientes_sync(
    slug: str, *, dry_run: bool = True, full_sync: bool = True,
    now_iso: str | None = None,
    _client=None, _fire_conn=None, _env_conn=None,   # injeção de teste
) -> ClientesReconciliacaoResponse | ClientesLocalResult | None: ...
```

Fluxo idêntico ao catálogo:
1. `flowpcp_config_for_slug(slug)` — `None` se ambiente sem FlowPCP → skip.
2. Conecta no Fire do ambiente; `extract_clientes_ativos(fire_conn, desde_data=...)`.
3. **Sempre** grava cópia local: `clientes_fire_repo.replace_all(env_conn, dtos, extraido_em=...)`.
4. Se `clientes_push` OFF → retorna `ClientesLocalResult(itens, extraido_em)` (só cópia local).
5. Se ON → `build_clientes_request(...)` + `client.send_clientes(request)` → devolve relatório.

---

## Extração do Fire (a parte com lógica nova)

### Query — `queries.LIST_CLIENTES_ATIVOS`

Ancorada nas tabelas já usadas pelo importador (`CADASTRO`, `CAB_VENDAS`):

```sql
SELECT C.CODIGO, C.RAZAO_SOCIAL, C.CPF_CNPJ, C.CODGRUPO
FROM CADASTRO C
WHERE C.RELAC_CLIENTE = 'Sim'
  AND EXISTS (
      SELECT 1 FROM CAB_VENDAS V
      WHERE V.CLIENTE = C.CODIGO
        AND V.DATA_PEDIDO >= ?      -- data de corte = hoje − 12 meses
  )
ORDER BY C.CODIGO
```

- **Data de corte calculada no Python** e passada como bind param (testável; evita depender
  de aritmética de data do Firebird). `datetime.now(UTC).date()` menos ~365 dias.
- `RELAC_CLIENTE = 'Sim'` — mesmo filtro que `FIND_CLIENT_BY_CNPJ`/`SEARCH_CLIENTS` já usam.

### DTO — `ClienteFireDTO` (espelha `ProdutoFireDTO`)

```python
@dataclass(frozen=True)
class ClienteFireDTO:
    fire_cliente_id: str   # str(CADASTRO.CODIGO) — PK durável imutável
    cnpj: str              # CPF_CNPJ normalizado (só dígitos) — chave de match no Flow
    nome: str              # RAZAO_SOCIAL
    grupo_codigo: str | None  # str(CODGRUPO) — a marca, para rollup em relatório
    ativo: bool            # True (todos vêm da janela ativa); reservado p/ inativação futura
```

### Duas regras de higiene (decorrem de "Flow casa só por CNPJ")

1. **Descartar cliente sem CNPJ utilizável.** Se `CPF_CNPJ` normalizado for vazio/curto
   demais para ser CNPJ, o Flow não conseguiria casar (`resolver-cliente.ts` retorna null).
   **`logger.info` com a contagem de descartados** — nunca cortar em silêncio.
2. **Dedup por CNPJ normalizado** antes de enviar (invariante 1 CNPJ = 1 cliente). Se duas
   linhas de `CADASTRO` colidem no mesmo CNPJ, mantém a de **maior `CODIGO`** (cadastro mais
   recente) — critério determinístico e computável direto do resultado da query.
   **`logger.info` com a contagem de colisões resolvidas.** (Se no futuro quisermos desempatar
   por recência de pedido, a query passa a expor `MAX(V.DATA_PEDIDO)` por cliente.)

Normalização de CNPJ: mesma limpeza do `FIND_CLIENT_BY_CNPJ`
(`REPLACE` de `.`, `/`, `-`, espaço). Reusar helper — não duplicar.

---

## Payload enviado ao Flow (o "cadastro curado")

### Schema — `clientes_schema.py` (espelha `catalogo_schema.py`)

```python
class ClienteItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    fireClienteId: str        # CADASTRO.CODIGO (PK durável)
    cnpj: str                 # normalizado — chave de match
    nome: str                 # RAZAO_SOCIAL
    grupoCodigo: str | None = None   # CODGRUPO (marca)
    ativo: bool = True

class ClientesRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    schema_: str = Field(default="clientes.v1", alias="schema")
    dryRun: bool
    fullSync: bool
    itens: list[ClienteItem]
    origem: ClientesOrigem     # {importadorVersao, extraidoEm} — igual ao catálogo

class ClientesReconciliacaoResponse(BaseModel):
    # extra="allow" — o Flow é dono do contrato de resposta (contagens/amostras)
    ...
```

### Método no client — `client.send_clientes()`

Espelha `send_catalogo`: `POST _CLIENTES_PATH` (`/api/portal-pedidos/clientes`),
`idempotency_key = f"clientes-{int(dryRun)}-{len(itens)}"`, mesma política de retry
(`idempotent_post_policy`), devolve `ClientesReconciliacaoResponse`.

---

## Persistência local — tabela `clientes_fire`

`clientes_fire_repo.py` espelha `catalogo_fire_repo.py` (`replace_all` = delete+insert
snapshot, `list_all`, `count`). DDL nova em `schema_env.py` (db do ambiente):

```sql
CREATE TABLE IF NOT EXISTS clientes_fire (
    fire_cliente_id TEXT PRIMARY KEY,
    cnpj            TEXT NOT NULL,
    nome            TEXT NOT NULL,
    grupo_codigo    TEXT,
    ativo           INTEGER NOT NULL,
    extraido_em     TEXT NOT NULL
);
```

"Manter no importador" independe do envio ao Flow — a cópia local é sempre atualizada,
igual ao catálogo.

---

## Config e disparo

### Gate — `config.py`

Adicionar a `FlowPCPConfig`:

```python
clientes_push: bool = False   # OFF = sync só atualiza a cópia local
```

Mapear de `flowpcp_clientes_push` em `flowpcp_config_from_env`. Coluna nova
`flowpcp_clientes_push` na tabela `environments` (`schema_shared.py` +
`environments_repo`), default 0.

Janela de 12 meses: **hardcoded** (constante no módulo), sem coluna de config — YAGNI.

### Rota — `routes_environments.py`

Botão "Sincronizar clientes" ao lado do de catálogo, mesma rota family. Só ambientes com
FlowPCP habilitado (a MM liga; Nasmar fica fora, como o catálogo).

---

## Lado Flow (pcp-app) — dependência crítica, fora deste repo

Precisa de `POST /api/portal-pedidos/clientes`: bulk **upsert por CNPJ**, idempotente, com
`dryRun`/`fullSync` e relatório de reconciliação — análogo a `/catalogo`.

**Este repo entrega tudo do lado Fire/Importador. Esse endpoint é o gargalo e depende do
outro time (histórico: PRs do pcp-app atrasam).** Enquanto o endpoint não existe, o sync roda
com `clientes_push=OFF` (só cópia local) sem quebrar nada.

---

## Testes

Espelham a suíte do catálogo:
- `extract_clientes_ativos` — com Fire fake: janela filtra certo, descarte sem-CNPJ conta,
  dedup por CNPJ resolve colisão pela regra (maior CODIGO).
- `clientes_mapper.build_clientes_request` — DTO → schema, alias camelCase.
- `clientes_fire_repo` — replace_all/list_all/count (snapshot substitutivo).
- `run_clientes_sync` — com `_client`/`_fire_conn`/`_env_conn` injetados: gate OFF devolve
  `ClientesLocalResult`; gate ON chama `send_clientes`; ambiente sem FlowPCP devolve `None`.
- `client.send_clientes` — status ok, erro HTTP → `FlowPCPClientError`.

Rodar direcionado: `.venv/bin/pytest tests/<arquivos novos> -v`, depois suíte completa.

---

## Fora de escopo (YAGNI — follow-on natural)

- **Agendamento noturno** — pendura no mesmo worker do catálogo depois.
- **Inativação** de quem sai da janela de 12 meses (downgrade `ativo=false`). Requer decisão
  do lado Flow sobre o que fazer com cliente que parou de comprar.
- **Rollup por marca como identidade** (`CODGRUPO` como cliente). Hoje só como campo `grupoCodigo`.
- **Janela configurável** por ambiente.
- **Campos extras** (fantasia/cidade/uf/contato) — ver "a confirmar" abaixo.

---

## A confirmar na Fire viva (VPN) no momento de implementar

Não bloqueiam o design; registrados por honestidade:

1. **`CODGRUPO` → nome da marca:** existe tabela de lookup para mandar o rótulo do grupo
   junto do código? Se sim, adicionar `grupoNome` ao payload. Se não, mandar só `grupoCodigo`.
2. **Colunas `FANTASIA` / `CIDADE` / `UF` em `CADASTRO`:** há um TODO em
   `queries.SEARCH_CLIENTS` dizendo que nunca foram reconfirmadas. Se existirem e o dono quiser,
   entram no payload curado (e no DTO/schema/tabela local).
3. **`CAB_VENDAS.DATA_PEDIDO`** é a coluna de data correta para a janela (vs. `DTHORA_PEDIDO`).
   Confirmar qual reflete melhor "quando o cliente comprou".

---

## Contrato de wire (resumo para o time do Flow)

```
POST /api/portal-pedidos/clientes
Headers: X-Service-Token, X-Tenant-Id, Content-Type: application/json
Idempotency-Key: clientes-<0|1>-<n_itens>

{
  "schema": "clientes.v1",
  "dryRun": true,
  "fullSync": true,
  "itens": [
    { "fireClienteId": "498", "cnpj": "06347409029651",
      "nome": "SBF COMERCIO DE PRODUTOS ESPORTIVOS S.A",
      "grupoCodigo": "12", "ativo": true }
  ],
  "origem": { "importadorVersao": "1.0.0", "extraidoEm": "2026-07-17T12:00:00Z" }
}

→ upsert idempotente por cnpj; resposta = relatório de reconciliação
  (contagens/amostras), formato do Flow.
```
