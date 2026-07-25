# De-para de cliente intercompany (Nasmar → cliente real) — design

**Data:** 2026-07-25
**Domínio:** `erp` + `gestor`/flowpcp (push de pedido)
**Status:** aprovado (Samuel, 2026-07-25)

---

## 1. Problema

A MM opera dois Fire distintos:

- **`.7` Americanense** (`192.168.15.7` / `C:\FireAdmMM_Ame\MM_AMERICANENSE.FDB`) — a **produção**.
  É o `fb_path` do ambiente `mm` (o único com `flowpcp_enabled=1`). É onde mora o produto a
  produzir, com a marca (STZ, ACT VITTA, KOLOSH).
- **`.4` Nasmar/Confecção** (`192.168.15.4` / `C:\FireAdmMM\MM_CONFECCAO.FDB`) — a **revenda**.
  É o `fb_path` do ambiente `nasmar` (`flowpcp_enabled=0`). Jogada fiscal: a Nasmar fatura, mas
  quem produz é a Americanense e quem recebe é outro cliente.

Parte dos pedidos que a Americanense conhece sai **no nome da Nasmar**. Hoje esses pedidos sobem
pro Flow com a Nasmar como cliente — e o chão de fábrica precisa do cliente **real** (Studio Z,
Beira Rio, Dakota, Authentic Feet), porque é ele que define a marca.

**O que muda:** só o cliente, e só no payload que vai pro Flow.
**O que não muda:** o produto vem sempre do `.7`. O `.4` serve exclusivamente pra descobrir o
cliente. As descrições de produto do `.4` são genéricas ("KIT 3", "MEIA COMUM") e não entram em
lugar nenhum.

---

## 2. Evidência (Fire viva, 2026-07-25, leitura read-only via VPN)

Nasmar no `.7` = `CADASTRO.CODIGO = 2`, `CPF_CNPJ = 34.513.679/0001-34`.

**Pedidos com `CLIENTE = Nasmar` no `.7`: 939** (`FATURADO` 789, `PEDIDO` 148, `CANCELADO` 2).

Sobre os **148 abertos** (`STATUS='PEDIDO'`) — é o que interessa pro Flow:

| resultado do de-para | qtd | % |
|---|---:|---:|
| resolvido (match exato) | 103 | 69,6% |
| não achado no `.4` | 38 | 25,7% |
| sem `PEDIDO_CLIENTE` | 6 | 4,1% |
| resolvido só normalizando sufixo | 1 | 0,7% |
| **ambíguo (CNPJs diferentes)** | **0** | **0%** |

**Zero ambiguidade em 939 pedidos.** Quando a chave casa, ela é confiável. É comum vários pedidos
no `.4` dividirem o mesmo `PEDIDO_CLIENTE` (2 a 4 linhas) — mas todos apontam pro mesmo CNPJ. Por
isso a regra é **"CNPJ distinto único entre os hits"**, nunca "linha única".

**Os 38 não achados não são falha de match.** São `PULMÃO`, `PULMÃO-AW`, `PULMÃO-MF`, `AF`, `AW`,
`mf 2`, `NBA0638-JN`, `GFNASMAR`: reposição de estoque e apelido digitado à mão na Fire. Nesses o
fallback pra Nasmar **é a resposta certa**.

**Validação ponta a ponta com o dado do Portal em produção.** O ambiente `mm` tem 230 imports, dos
quais **14 com cliente Nasmar** (7 `order_number` distintos). Resolvendo contra o `.4`:

| `order_number` | cliente real | CNPJ |
|---|---|---|
| AF066 | AUTHENTIC FEET | 10.772.208/0001-82 |
| AF086 | BARBARA ARTIGOS ESPORTIVOS | 23.793.783/0001-03 |
| AF190 | PINHAIS ARTIGOS ESPORTIVOS | 57.832.331/0001-05 |
| AF199 | KAF ARTIGOS ESPORTIVOS | 60.783.489/0001-47 |
| AW108 | PINHAIS ARTIGOS ESPORTIVOS | 57.832.331/0001-05 |
| AW123 | LAW ARTIGOS ESPORTIVOS | 60.898.840/0001-45 |
| AF112 | — não achado → fallback Nasmar | — |

**6 de 7.**

---

## 3. Onde isso roda (e por que não em outro lugar)

O push pro Flow nasce em `push_new_order()` (`app/integrations/flowpcp/hook.py`), chamado no
`export-xlsx` (`app/web/server.py:1889`) e no `send-to-fire` (`server.py:1736`). O `Order` vem do
**arquivo parseado**, e o `imports.fire_codigo` está NULL em 230/230 no cliente — ou seja, o Fire
segue manual/XLS e o Portal hoje só gera XLS e empurra o pedido pro Flow.

A resolução entra **entre o hook e o `build_recebimento_payload`**. Consequências desejadas:

- **Não muta o `Order`.** O `Order` é a verdade do arquivo; mutar quebraria XLS, preview e Fire.
- **XLS e Fire continuam com a Nasmar.** Fiscalmente é a Nasmar que compra da Americanense — isso
  está correto e não pode mudar.
- A troca de cliente existe **só no payload do Flow**.

**Rejeitado:** resolver no `OrderNormalizer`/pipeline (contamina preview, XLS e Fire) e resolver no
`drain_outbox` (o payload do outbox deixaria de ser o contrato imutável enfileirado, e a auditoria
não veria a decisão no momento do envio).

---

## 4. Componentes

### 4.1 `app/erp/depara_cliente.py` (novo)

```python
@dataclass(frozen=True)
class ResolucaoCliente:
    resolvido: bool
    cnpj: str | None          # dígitos do cliente real
    nome: str | None          # RAZAO_SOCIAL ou NOME do CADASTRO do .4
    motivo: str               # "ok" | "sem_chave" | "nao_encontrado" | "ambiguo" | "erro_conexao"
    pedidos_no_4: list[dict]  # [{codigo, status, codnf}] — radar da demanda fantasma

def resolver_cliente_real(chave: str | None, *, revenda_slug: str) -> ResolucaoCliente
```

Responsabilidade única: dada a chave (`PEDIDO_CLIENTE`), devolver o cliente real do `.4`. Não
conhece `Order`, não conhece payload do Flow, não decide se deve rodar.

### 4.2 Query (`app/erp/queries.py`)

```sql
FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE = """
    SELECT V.CODIGO, V.STATUS, V.CODNF,
           C.CODIGO, TRIM(C.NOME), TRIM(C.RAZAO_SOCIAL), TRIM(C.CPF_CNPJ)
    FROM CAB_VENDAS V
    JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
    WHERE TRIM(V.PEDIDO_CLIENTE) = ?
"""
```

`TRIM` nos dois lados — mesmo padrão simétrico fixado em `d505f8c`. O bind vem `.strip()`ado.

### 4.3 A chave

A chave é `order.header.order_number` (`.strip()`ado). É o mesmo valor que o `ERPMapper` grava em
`CAB_VENDAS.PEDIDO_CLIENTE` (`app/erp/mapper.py:64`, truncado em 20 chars lá) e é o que o `.4` usa
pra guardar o pedido de compra do cliente final. Nos 7 `order_number` Nasmar do Portal em produção
(AF066…AW123) nenhum passa de 20 chars, então a truncagem do Fire não afeta o match hoje; a chave do
de-para usa o valor **inteiro**, sem truncar.

### 4.4 Regra de decisão

1. Chave vazia/nula → `sem_chave` → **fallback**.
2. Zero hits → `nao_encontrado` → **fallback**.
3. Hits com **mais de um CNPJ distinto** → `ambiguo` → **fallback**. (Nunca escolher: sem chute.)
4. Hits com **um CNPJ distinto** → `ok`, resolve. Nome = `RAZAO_SOCIAL` ou, se vazio, `NOME`.

**Sem normalização de sufixo.** Medida em 939 pedidos, ela recuperava 6 (1 aberto): 0,7% não paga
o risco de casar o sufixo com o pedido errado.

### 4.5 Gatilho e configuração (sem hardcode)

Duas colunas novas em `environments` (`COLUMN_MIGRATIONS` em `app/persistence/schema_shared.py`):

| coluna | tipo | uso |
|---|---|---|
| `intercompany_cnpj` | TEXT | CNPJ que dispara o de-para (a Nasmar) |
| `intercompany_env_slug` | TEXT | slug do ambiente cujo Firebird tem o de-para (`nasmar`) |

Qualquer uma vazia = **feature desligada** (no-op). O slug reusa `environments_repo.to_fb_config()`
do ambiente Nasmar: **nenhuma credencial nova, nenhum IP no código, senha segue cifrada**.

Comparação por dígitos via `app.erp.cnpj.cnpj_digits` (helper já existente).

### 4.6 Payload do Flow (`app/integrations/flowpcp/mapper.py`)

Com resolução `ok`:

- `cliente.nome` / `cliente.cnpj` = **cliente real**. É isso que faz o Flow resolver cliente e marca
  sozinho — `resolverClienteId` (pcp-app `src/app/api/portal-pedidos/recebimento/route.ts:78`) casa
  e cria por CNPJ.
- `faturadoPor: {nome, cnpj}` = **Nasmar**, pra não perder a parte fiscal.
- `fornecedor` e todos os itens: **inalterados** (produto é do `.7`).

Sem resolução: payload idêntico ao de hoje (Nasmar como cliente).

> ⚠️ **`faturadoPor` não sobrevive no Flow ainda.** O contrato `pedidoRecebimentoV1` (pcp-app
> `src/lib/services/portal-pedidos/contract.ts`) é `z.object` sem passthrough: campo desconhecido é
> **descartado em silêncio** (não dá 400 — verificado). O `dados_importador` é montado no servidor
> do Flow, não vem do Importador. Persistir `faturadoPor` exige PR no pcp-app (contrato + route +
> tipo `DadosImportador`). Decisão do Samuel: **manda agora, Flow depois** — o campo já viaja e o
> registro fiscal fica auditável no lado do Importador enquanto isso.

### 4.7 Auditoria e UI

`repo.append_audit(import_id, "depara_cliente", {...})` com: `chave`, `motivo`, `cnpj_real`,
`nome_real`, `revenda_slug` e `pedidos_no_4` (código, `STATUS`, `CODNF`).

Selo no preview, no mesmo padrão do selo de de-para de produto: "cliente real resolvido" ou
"cliente não resolvido — sobe como Nasmar".

### 4.8 Erro e latência

Só paga quem é Nasmar: **14 de 230 imports (6%)** no cliente hoje.

- Conexão read-only ao `.4`, timeout curto, cache por processo em `(revenda_slug, chave)`.
- **Qualquer** exceção (Firebird fora, timeout, credencial) → `erro_conexao` → fallback + audit.
  Nunca levanta: o hook é best-effort por contrato e o pedido já existe.

### 4.9 Radar: demanda fantasma

No Fire da Nasmar o pedido de demanda fica `STATUS='PEDIDO'` pra sempre; a entrega sai por outro
pedido de faturamento/remessa. Um pedido de demanda pode já ter sido entregue e continuar aberto.

Escopo aqui: **observar, não decidir.** O audit grava `STATUS` e `CODNF` de cada pedido casado no
`.4` (AF066, por exemplo, casa com 2 pedidos, ambos `FATURADO`). Nada é bloqueado nem filtrado —
Samuel trata do lado do Flow.

---

## 5. Fora de escopo

- **Resolver os 26% não casados por texto** (`STUDIO Z 2`, `AUTHENTIC FEET 175`, `ARTWALK 42`).
  Sobem como Nasmar com aviso. Se a fila incomodar, vira o de-para manual com memória (mesmo padrão
  do `produto_depara`), com dado real na mão.
- **Ler pedido direto do `.7`** pra subir ao Flow. Não existe esse caminho hoje e o pedido continua
  entrando por arquivo.
- **Mudar produto, preço ou item.** O `.4` só entrega cliente.
- **Ambiente `nasmar` subir pedido pro Flow.** Segue `flowpcp_enabled=0`.

---

## 6. Testes

`tests/test_depara_cliente.py` (novo), com conexão falsa:

- match único → resolve (nome = `RAZAO_SOCIAL`; cai pro `NOME` quando vazia)
- múltiplos hits, mesmo CNPJ → resolve (caso real: AF086 tem 3 linhas)
- múltiplos hits, CNPJs diferentes → `ambiguo`, sem escolher
- chave vazia / só espaço → `sem_chave`
- zero hits → `nao_encontrado`
- exceção na conexão → `erro_conexao`, não levanta
- `TRIM` simétrico: chave com espaço à direita casa

`tests/test_flowpcp_depara_cliente.py` (novo):

- gatilho: cliente ≠ CNPJ intercompany → resolver nem é chamado
- config vazia → no-op
- payload com resolução: `cliente` = real, `faturadoPor` = Nasmar, itens intactos
- payload sem resolução: idêntico ao de hoje
- audit `depara_cliente` gravado nos dois caminhos

Regressão: `tests/test_flowpcp_mapper_cnpj.py`, `tests/test_flowpcp_mapper_prazo.py`,
`tests/test_flowpcp_hook.py`, `tests/test_web_server.py`, suíte completa antes do
commit final.

---

## 7. Riscos

| risco | mitigação |
|---|---|
| `faturadoPor` descartado pelo Flow até o PR no pcp-app | decisão explícita; fica auditável no Importador |
| 26% dos abertos sobem como Nasmar | é o comportamento pedido ("prefiro Nasmar com aviso"); selo + audit dão visibilidade |
| `.4` indisponível trava o push | fallback silencioso; nunca levanta |
| Nova I/O Firebird no request path | só 6% dos pedidos, timeout curto, cache por processo |
| Config apontando pro ambiente errado | slug validado contra `environments`; ausente = desligado |
