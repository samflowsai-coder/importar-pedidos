# Design — Carga de clientes ativos Fire → Flow

**Data:** 2026-07-17
**Domínio:** integrations/flowpcp + erp + persistence + web
**Status:** aprovado (brainstorming) + revisão adversarial incorporada (2026-07-17) — pronto para plano de implementação

> **Changelog 2026-07-17 (pós-review):** incorporados 1 BLOCKER + 6 achados IMPORTANTES da
> revisão adversarial. Principais: (B1) normalização canônica de CNPJ compartilhada entre a
> carga e o envio de pedido em runtime — `mapper.py:45` hoje manda CNPJ **não** normalizado,
> então a carga fragmentaria em vez de deduplicar; (I2) regra explícita CPF×CNPJ; (I4) trava
> de extração vazia; (I5) idempotency key com hash de conteúdo; (I6) contadores de
> descarte/dedup na resposta; (I7) semântica de órfãos/`fullSync`. Detalhes inline abaixo.

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

**Fora do espelho — trabalho adicional trazido pelo review (B1):** um normalizador canônico de
CNPJ compartilhado (`_cnpj_digits`) e a correção de `app/integrations/flowpcp/mapper.py:45` para
normalizar o `customer_cnpj` no envio de pedido em runtime. Sem isso, carga e runtime alimentam o
Flow com CNPJ em formatos diferentes e o mesmo cliente fragmenta (ver seção de normalização).

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
3. **Trava de extração vazia (I4):** se `extract_clientes_ativos` retornar 0 clientes, **não**
   sobrescreve o snapshot local nem empurra ao Flow — retorna `ClientesLocalResult(itens=0, ...)`
   com `skipped_empty=True` e loga em nível `warning`. 0 numa query com janela + `EXISTS` +
   coluna nova é plausível por engano (coluna errada, `DATA_PEDIDO` NULL em massa); apagar o
   snapshot e mandar `fullSync` de 0 itens poderia ser lido pelo Flow como "inative todos".
   Zerar de verdade exige flag explícito (`permitir_vazio=True`).
4. **Sempre** grava cópia local: `clientes_fire_repo.replace_all(env_conn, dtos, extraido_em=...)`.
5. Se `clientes_push` OFF → retorna `ClientesLocalResult(itens, extraido_em, descartados, colisoes)`
   (só cópia local — ver I6 sobre os contadores).
6. Se ON → `build_clientes_request(...)` + `client.send_clientes(request)` → devolve relatório
   (com os contadores de descarte/dedup anexados, ver I6).

`ClientesLocalResult` (espelha `CatalogoLocalResult`, mas mais rico por causa de I4/I6):

```python
@dataclass(frozen=True)
class ClientesLocalResult:
    itens: int
    extraido_em: str
    descartados_cpf: int          # I2/I6
    descartados_invalidos: int    # I2/I6
    colisoes_dedup: int           # I6
    skipped_empty: bool = False   # I4 — extração vazia, nada foi gravado/enviado
```

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
  de aritmética de data do Firebird). Base = "hoje" no fuso `America/Sao_Paulo` (a config já
  tem `timezone`) menos ~365 dias — não `UTC` cru (N1: desvio de ±1 dia na fronteira, imaterial
  numa janela anual, mas usar o fuso da config é de graça e mais correto).
- `RELAC_CLIENTE = 'Sim'` — mesmo filtro que `FIND_CLIENT_BY_CNPJ`/`SEARCH_CLIENTS` já usam.
  **Atenção (I3):** isso diz "é um cliente", não "é vendável hoje". O extractor de catálogo
  filtra `BLOQUEADO` do produto; o de clientes precisa do análogo — **confirmar na Fire viva
  se `CADASTRO` tem flag de bloqueio/inativação de cliente** e, se tiver, adicioná-lo ao `WHERE`.
  Sem isso, um cliente bloqueado com pedido recente entra como `ativo=true`.
- **Índice (M3):** o `EXISTS` correlacionado roda por linha de `CADASTRO`. Confirmar índice em
  `CAB_VENDAS(CLIENTE)` (idealmente `(CLIENTE, DATA_PEDIDO)`) via `tools/explore_firebird.py`;
  provável que exista por FK, mas não assumir.

### DTO — `ClienteFireDTO` (espelha `ProdutoFireDTO`)

```python
@dataclass(frozen=True)
class ClienteFireDTO:
    fire_cliente_id: str   # str(CADASTRO.CODIGO) — PK durável imutável
    cnpj: str              # CPF_CNPJ normalizado (só dígitos) — chave de match no Flow
    nome: str              # RAZAO_SOCIAL
    grupo_codigo: str | None  # str(CODGRUPO) se a coluna existir (I1); senão None → payload sem grupo
    ativo: bool            # True (todos vêm da janela ativa). Campo hoje é constante — só
                           # ganha significado quando a inativação existir (ver I7).
```

> **I1 — `CODGRUPO` não é verificável no repo.** Nenhuma query real usa `CODGRUPO` (só
> `RAZAO_SOCIAL`/`CPF_CNPJ`/`CODIGO`/`RELAC_CLIENTE` estão confirmados). **Confirmar a coluna
> na Fire viva antes de codar.** Se não existir com esse nome, `grupo_codigo=None` e o payload
> vai sem `grupoCodigo` — a carga não quebra, só perde o enriquecimento de marca.

### Normalização de CNPJ — invariante crítica (B1 + M2)

**O problema:** o Flow casa cliente por igualdade de CNPJ. Existem **dois** alimentadores neste
repo e eles não normalizam igual:

- **Envio de pedido em runtime** — `app/integrations/flowpcp/mapper.py:45` manda
  `cnpj=h.customer_cnpj or None` **sem normalizar**. Os parsers produzem CNPJ **formatado**
  (`kallan_xls_parser.py:56` monta `XX.XXX.XXX/XXXX-XX`; Beira-Rio/Kolosh capturam `[\d./-]+`).
- **Carga (novo)** — normalizaria para só dígitos.

Resultado se nada mudar: a carga manda `06347409029651`, o pedido manda `06.347.409/0296-51` →
**o mesmo cliente fragmenta em dois no Flow** — o oposto do objetivo (d).

**Decisão:**
1. **Eleger UM normalizador canônico** — `re.sub(r"\D", "", cnpj)` — hoje há cópias espalhadas
   (`erp/product_check.py:_cnpj_digits`, `erp/mapper.py:_digits_only`, `fire_update.py:27`,
   `server.py:2217`, `firebird_exporter.py:208`). Centralizar num único helper compartilhado.
2. **Usá-lo na carga E no runtime** — corrigir `flowpcp/mapper.py:45` para normalizar o
   `customer_cnpj` antes de enviar. **Isso expande o escopo para tocar o caminho de runtime**,
   mas é a correção certa: dígitos-only é a forma canônica inequívoca, e os dois alimentadores
   precisam concordar.
3. **Gate de verificação:** confirmar no pcp-app que `resolver-cliente.ts` normaliza para
   dígitos antes de casar. Se já normaliza, documentar como dependência de contrato; se não/
   incerto, o passo 2 acima garante a consistência do nosso lado de qualquer forma.

### Regras de higiene da extração

1. **Regra explícita CPF × CNPJ (I2).** `CADASTRO.CPF_CNPJ` guarda CPF (11 díg.) e CNPJ (14).
   Após normalizar para dígitos:
   - `len == 14` → **CNPJ válido**, mantém.
   - `len == 11` → **CPF** (pessoa física). O Flow só casa por CNPJ, então um CPF no campo
     `cnpj` casaria por chave inválida → **descartar da carga** (v1) e contar à parte.
   - qualquer outro (vazio, curto, lixo) → **descartar**, contar à parte.
2. **Dedup por CNPJ normalizado** (invariante 1 CNPJ = 1 cliente). Colisão → mantém a de
   **maior `CODIGO`** (determinístico, computável da query). Ver caveat M1 abaixo.
3. **Contadores visíveis, não só em log (I6).** Descarte e dedup acontecem **dentro** do
   importador, antes do envio — então **não** aparecem no relatório do Flow. Carregar
   `{descartados_cpf, descartados_invalidos, colisoes_dedup}` no `ClientesLocalResult` **e** no
   dict de resposta da rota, para o operador ver no dry-run (não caçar em log). Log **quebrado
   por motivo**, não um agregado.

> **M1 — caveat do dedup:** "maior CODIGO = mais recente" é determinístico, mas recência ≠
> qualidade do nome (o recadastro mais novo pode ter typo, o antigo ter o nome curado). Como o
> Flow casa por CNPJ (não por CODIGO), `fireClienteId`/`nome` são metadado — aceitável em v1,
> documentado.

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
    schema_: str = Field(default="cadastro.clientes.v1", alias="schema")  # M4: {domínio}.{entidade}.{versão}, simétrico a catalogo.produtos.v1
    dryRun: bool
    fullSync: bool
    itens: list[ClienteItem]
    origem: ClientesOrigem     # {importadorVersao, extraidoEm} — igual ao catálogo

class ClientesReconciliacaoResponse(BaseModel):
    # extra="allow" — o Flow é dono do contrato de resposta (contagens/amostras)
    ...
```

### Método no client — `client.send_clientes()`

Espelha `send_catalogo`: `POST _CLIENTES_PATH` (`/api/portal-pedidos/clientes`), mesma política
de retry (`idempotent_post_policy`), devolve `ClientesReconciliacaoResponse`.

> **I5 — idempotency key com hash de conteúdo, NÃO `{dryRun}-{len}`.** O catálogo usa
> `catalogo-{int(dryRun)}-{len(itens)}` porque seu conteúdo é estável. O cadastro **muda** (nome
> canônico, grupo) com a **contagem constante** (617 seguem 617) → dois applies no mesmo dia com
> nomes corrigidos colidiriam na key e o Flow trataria o segundo como replay (correção **não**
> aplicada). Usar:
> `f"clientes-{int(dryRun)}-{sha256(sorted(f'{cnpj}|{nome}|{grupo}' for item))[:16]}"`.
> Estável em retry (mesmo conteúdo → mesma key), único quando o conteúdo muda. **Não** usar
> `extraido_em` (quebraria a idempotência de retry).

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

**Atomicidade (verificado no review, OK):** `env_connect` abre transação `DEFERRED` com
`commit`/`rollback`, e a extração é totalmente materializada em memória **antes** do
`DELETE` — falha na extração nunca deixa a tabela pela metade. O único risco é extração
**vazia bem-sucedida**, coberto pela trava I4 no orquestrador (o `replace_all` só é chamado
com `dtos` não-vazio, salvo flag `permitir_vazio`).

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

**Contrato a fechar ANTES de codar o lado Flow (N3):** o wire abaixo é uma **proposta** — o
Flow é dono do formato. Alinhar com o time do pcp-app antes que eles implementem, senão
retrabalho. Dois pontos do contrato exigem decisão explícita:

- **B1 — normalização:** confirmar que o `resolver` casa por CNPJ **dígitos-only** (nós
  garantimos dígitos dos dois lados; ver seção de normalização).
- **I7 — órfãos / semântica de `fullSync`:** numa carga posterior, quem sai da janela de 12m
  some do `itens`. O snapshot local (substitutivo) **dropa** o cliente; se o Flow **não** poda
  os `flow_only`, a "carteira ativa" no Flow vira **aditiva** — nunca encolhe — e o campo
  `ativo` fica morto (sempre `true`) até a inativação existir. **Decidir agora:** ou (a) o Flow
  poda `flow_only` em `fullSync=true`, ou (b) **não** mandar `fullSync=true` (que sugere uma
  completude que o Flow não vai honrar) e assumir explicitamente que a carteira é aditiva no
  meio-tempo. Recomendação: (b) até a inativação existir — menos surpresa, sem exclusão
  acidental de cliente do lado Flow.

---

## Testes

Espelham a suíte do catálogo, mais os casos dos achados do review:
- `_cnpj_digits` canônico (B1/M2) — formatado e dígitos colapsam no mesmo valor; usado na carga
  **e** em `flowpcp/mapper.py` (teste de regressão do runtime).
- `extract_clientes_ativos` — com Fire fake: janela filtra certo; **CPF (11 díg.) descartado e
  contado à parte** (I2); CNPJ inválido descartado e contado; dedup por CNPJ mantém maior CODIGO;
  contadores retornados corretos (I6).
- **Trava de vazio (I4)** — extração 0 itens → `skipped_empty=True`, `replace_all` **não** é
  chamado, `send_clientes` **não** é chamado.
- **Idempotency key (I5)** — mesmo conteúdo → mesma key; conteúdo diferente com mesma contagem →
  key diferente.
- `clientes_mapper.build_clientes_request` — DTO → schema, alias camelCase, `schema=cadastro.clientes.v1`.
- `clientes_fire_repo` — replace_all/list_all/count (snapshot substitutivo).
- `run_clientes_sync` — com `_client`/`_fire_conn`/`_env_conn` injetados: gate OFF devolve
  `ClientesLocalResult` (com contadores); gate ON chama `send_clientes`; ambiente sem FlowPCP
  devolve `None`.
- `client.send_clientes` — status ok, erro HTTP → `FlowPCPClientError`.

Rodar direcionado: `.venv/bin/pytest tests/<arquivos novos> -v`, depois suíte completa.

---

## Fora de escopo (YAGNI — follow-on natural)

- **Agendamento noturno** — pendura no mesmo worker do catálogo depois.
- **Inativação** de quem sai da janela de 12 meses (downgrade `ativo=false`). Requer decisão
  do lado Flow — ver I7 abaixo, que exige **fechar a semântica de `fullSync` agora** ainda que
  a inativação em si fique para depois.
- **Rollup por marca como identidade** (`CODGRUPO` como cliente). Hoje só como campo `grupoCodigo`.
- **Janela configurável** por ambiente.
- **Campos extras** (fantasia/cidade/uf/contato) — ver "a confirmar" abaixo.

---

## A confirmar na Fire viva (VPN) no momento de implementar

Os dois primeiros são **gates de verificação** (bloqueiam a implementação da parte afetada até
serem confirmados na Fire viva); o resto é ajuste fino.

1. **`CODGRUPO` existe em `CADASTRO`? (I1 — gate)** Não é usado por nenhuma query atual do repo.
   Confirmar a coluna **antes** de codar a query. Se não existir: `grupo_codigo=None`, payload
   sem `grupoCodigo`, a carga não quebra. E, existindo, há tabela de lookup do **nome** da marca
   para mandar `grupoNome` junto? Se sim, adicionar; se não, só o código.
2. **Flag de bloqueio/inativação de cliente em `CADASTRO`? (I3 — gate)** Análogo ao `BLOQUEADO`
   dos produtos. Se existir, entra no `WHERE` da query (não mandar cliente bloqueado como ativo).
3. **Índice em `CAB_VENDAS(CLIENTE)` (M3):** confirmar via `tools/explore_firebird.py` para o
   `EXISTS` não virar full scan por cliente.
4. **`CAB_VENDAS.DATA_PEDIDO` vs `DTHORA_PEDIDO`:** qual reflete melhor "quando o cliente
   comprou" para a janela. (`DATA_PEDIDO` existe — verificado em `INSERT_CAB_VENDAS`; a dúvida é
   só semântica.)
5. **Colunas `FANTASIA` / `CIDADE` / `UF` em `CADASTRO`:** TODO em `queries.SEARCH_CLIENTS` diz
   que nunca foram reconfirmadas. Se existirem e o dono quiser, entram no payload curado.

---

## Contrato de wire (PROPOSTA — fechar com o time do Flow antes de eles codarem, N3)

```
POST /api/portal-pedidos/clientes
Headers: X-Service-Token, X-Tenant-Id, Content-Type: application/json
Idempotency-Key: clientes-<0|1>-<sha256(conteúdo)[:16]>     # I5, não {len}

{
  "schema": "cadastro.clientes.v1",
  "dryRun": true,
  "fullSync": false,                # I7: aditivo até a inativação existir (recomendação)
  "itens": [
    { "fireClienteId": "498", "cnpj": "06347409029651",   # dígitos-only sempre (B1)
      "nome": "SBF COMERCIO DE PRODUTOS ESPORTIVOS S.A",
      "grupoCodigo": "12", "ativo": true }
  ],
  "origem": { "importadorVersao": "1.0.0", "extraidoEm": "2026-07-17T12:00:00Z" }
}

→ upsert idempotente por cnpj (dígitos); resposta = relatório de reconciliação
  (contagens/amostras), formato do Flow.
```

**Pontos de contrato a confirmar com o pcp-app:** (1) `resolver` casa por CNPJ dígitos-only
(B1); (2) semântica de `fullSync` / poda de `flow_only` (I7); (3) formato do relatório de
resposta (nós toleramos com `extra="allow"`, mas alinhar os `contagens`).
