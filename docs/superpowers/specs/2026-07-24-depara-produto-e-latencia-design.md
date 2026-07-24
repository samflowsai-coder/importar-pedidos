# De-para de produto + Latência do preview/export — Design

> Data: 2026-07-24
> Origem: feedback da Grazi (usuária principal do Importador), formulário de 24/07/2026.
> Domínios: `erp` (product_check, queries), `exporters`, `web`, `persistence`, `llm`(não), `worker`(outbox).

## Problema

Dois pontos, ambos ditos pela usuária, ambos recorrentes:

1. **"Quando não puxa as referências corretas, preciso alterar manualmente."**
   O match de produto (`app/erp/product_check.py`) é literal e sem memória:
   `EAN exato → CODPROD_ALTERN exato → desiste`. O varejista usa a referência
   dele; quando ela não existe igual no cadastro do Fire, o item dá ✗ e a Grazi
   procura o produto certo no Fire na mão — **toda vez, no mesmo item, para
   sempre**. Não existe hoje nenhuma forma de corrigir o *produto* dentro do
   portal (só existe override de *cliente*).

2. **"O site poderia ser um pouco mais rápido na hora de gerar o XLS."**
   A geração do XLSX em si é barata. O custo está em duas coisas no caminho
   crítico de `POST /api/imported/{id}/export-xlsx`:
   - `push_new_order` faz um **POST HTTP síncrono pro Flow (Fly)** com
     `request_timeout_s = 30.0` antes de responder. Flow lento = até 30 s de
     espera olhando o botão.
   - `check_order` roda **2N+1 round-trips no Firebird** (1 cliente + até 2 por
     item) e é executado **duas vezes** (no preview e de novo no export).

## Objetivo

- Item que não casa vira uma decisão **feita uma vez** e **lembrada para sempre**,
  por cliente. Da próxima vez casa sozinho.
- Ranking assistido no picker (sugere os prováveis, **nunca aplica sozinho**).
- Preview e export perceptivelmente mais rápidos; a cauda de 30 s eliminada.

Fora de escopo (registrado como futuro): escrever a referência do varejista no
`CODPROD_ALTERN` do Fire (opção B do brainstorm).

---

## Peça 1 — De-para de produto (memória por cliente)

### 1.1 Tabela `produto_depara` (DB do ambiente)

Nova tabela em `app/persistence/schema_env.py`, ao lado de `catalogo_fire`
(é dado **por empresa**, não compartilhado):

```sql
CREATE TABLE IF NOT EXISTS produto_depara (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    cliente_cnpj  TEXT NOT NULL,   -- digits-only, do header do pedido
    chave_tipo    TEXT NOT NULL,   -- 'codigo' | 'ean'
    chave_valor   TEXT NOT NULL,   -- referência do varejista, NORMALIZADA
    fire_produto_id TEXT NOT NULL, -- str(SEQ) — PK durável do produto no Fire
    fire_codigo   TEXT NOT NULL,   -- str(SEQ), o que vai no XLS
    fire_ean      TEXT,
    fire_nome     TEXT NOT NULL,
    criado_em     TEXT NOT NULL,
    criado_por    TEXT,            -- email do require_user
    UNIQUE (cliente_cnpj, chave_tipo, chave_valor)
);
```

**Por que chaveado por `cliente_cnpj` e não global:** a referência `1234` é um
produto na Riachuelo e outro na Centauro. De-para global colide entre
varejistas — é o bug clássico dessa feature. O escopo por CNPJ do cliente é a
decisão central, não um detalhe.

**Normalização da chave (`_norm_key`)** — precisa ser IDÊNTICA na gravação e na
leitura, senão o vínculo "some" e a usuária perde a confiança no recurso:
- `chave_tipo='codigo'`: `strip()` + `upper()`.
- `chave_tipo='ean'`: só dígitos (`re.sub(r"\D", "", v)`).
- `cliente_cnpj`: só dígitos (reusar `_cnpj_digits` do `product_check`).

Um item pode gerar **duas** entradas de chave (tem code E ean). Gravamos o que o
item trouxer: se tem ean, grava linha `ean`; se tem product_code, grava linha
`codigo`. Assim o próximo pedido casa por qualquer um dos dois.

Repo novo: `app/persistence/produto_depara_repo.py` — `upsert(...)`,
`lookup(cliente_cnpj, *, codigos: list, eans: list) -> dict[(tipo,valor)->row]`
(batelado, para o check em lote), `delete(id)`, `list_for_client(cnpj)`.

### 1.2 Terceiro degrau no match (`product_check.check_order`)

Ordem passa a ser: **EAN → CODPROD_ALTERN → de-para**.

Query nova em `app/erp/queries.py` (traz DESCRICAO + PRECO_VENDA, para a
validação de preço continuar funcionando no item resolvido por de-para):

```sql
FIND_PRODUCT_BY_SEQ = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS
    WHERE SEQ = ?
    ROWS 1
"""
```

Fluxo por item, quando EAN e CODPROD_ALTERN falham:
1. Olha o de-para do `cliente_cnpj` do pedido pela chave do item (ean e/ou code).
2. Se achou, pega `fire_produto_id` e resolve via `FIND_PRODUCT_BY_SEQ` para
   obter descrição e preço atuais do Fire.
3. `entry.match = True`, `match_source = 'depara'`, price_status classificado
   normalmente. Se o produto sumiu do Fire (SEQ não existe mais), trata como
   sem match e sinaliza o de-para órfão (candidato a limpeza; não bloqueia).

O de-para é lido do **SQLite local** (rápido), mas a resolução final do SEQ vai
ao Firebird — de propósito: preço e descrição precisam estar atuais para a
validação de preço não usar dado velho.

### 1.3 Preview mostra a origem do acerto

`app/web/static/index.html`, coluna de match: quando `match_source === 'depara'`,
✓ com selo próprio (ex.: "✓ vínculo") e tooltip "casado por vínculo que você
criou". **Ela precisa ver** que o acerto veio de uma decisão dela — confiança
(hoje 4/5) se constrói mostrando a origem, não escondendo.

### 1.4 Rotas (espelham `/api/clientes/search` + `override-cliente`)

- `GET /api/produtos/search?q=&limit=` — busca no **`catalogo_fire` local**
  (SQLite). Instantâneo, zero Firebird, funciona com a Fire offline. Busca por
  `codigo`, `ean` (dígitos) e `nome` (LIKE upper). `require_user`. Clamp limit
  [1,50].
- `POST /api/imported/{id}/vincular-produto` — body
  `{item_index, fire_produto_id}`. Grava o(s) vínculo(s) do item (code e/ou ean
  → SEQ), audita `produto_vinculo_criado` com `user_email`, re-roda `check_order`
  e devolve o check atualizado. Pre: `portal_status='parsed'`. `require_user`.
- `DELETE /api/produtos/depara/{id}` — desfaz um vínculo. O de-para é permanente
  por design; erro humano precisa ter volta. Audita `produto_vinculo_removido`.
  `require_user`.

### 1.5 Ranking assistido (o "C" — dentro do picker)

Ao abrir o picker para um item ✗, além da busca livre, sugere os 3–5 candidatos
mais prováveis do `catalogo_fire`, ranqueados. Heurística (Python puro sobre
~3,4k linhas em SQLite — trivial):
1. EAN parcial (sufixo/prefixo do EAN do item) — peso alto.
2. Sobreposição de tokens entre `item.description` e `catalogo_fire.nome`
   (set de palavras normalizadas, Jaccard).
3. Código contido (item.product_code aparece em codigo/nome).

**Pré-seleciona o topo, nunca aplica sozinho.** Falso positivo silencioso é
exatamente o que derruba a confiança da usuária. A gravação é sempre um clique
explícito dela.

### 1.6 Exporter usa a identidade do Fire

`app/exporters/erp_exporter.py` (via o `check` já disponível no fluxo de
export): item resolvido por de-para sai com:
- `CODIGO_PRODUTO = fire_codigo` (o SEQ),
- `EAN = fire_ean` quando existir,
- referência original do varejista anexada ao `OBS` (rastreabilidade).

Item **sem** de-para sai idêntico a hoje. Isso torna o resultado robusto ao
campo que a rotina de import do Fire lê (code **ou** ean) — não travamos essa
decisão no design.

**Suposição explícita a validar no cliente:** a rotina de importação de planilha
do Fire casa o produto por `CODPROD_ALTERN` (código) e/ou EAN. A validação
(§Testes) roda um pedido real com item resolvido pela rotina de import do Fire e
confirma por evidência qual campo ela lê. Se for só EAN, ou só código, o ajuste
é de uma linha no exporter — mas decidimos por teste, não por palpite.

---

## Peça 2 — Latência

### 2.1 Push pro Flow sempre via outbox (tira a cauda de 30 s)

`app/integrations/flowpcp/exporter.py::export` hoje tenta `send_order` inline e
só enfileira no outbox **em caso de falha** (após esperar o timeout). Inverte-se:
**enfileira direto** no `FLOWPCP_TARGET_NAME` e retorna. O `drain_outbox` já
consome esse target (`app/worker/jobs/drain_outbox.py:60`, com backoff/dead).

Efeito: HTTP sai do caminho crítico da request; sucesso e falha passam a
percorrer o mesmo trilho (retry + rastro). O Flow já deduplica por `externalId`,
então re-export não duplica. `push_new_order` continua best-effort e o
`idempotency_key = f"send-{import_id}"` continua garantindo unicidade
(`OutboxDuplicateError` é tratado como no-op).

`hook.py` deixa de construir `FlowPCPClient` no request path (não há mais HTTP
inline). Ajustar `test_flowpcp_hook.py` e
`test_web_server.py::test_send_to_fire_pushes_to_flowpcp_*` para asseverar
**enqueue no outbox** em vez de chamada HTTP.

### 2.2 Batelar `check_order` (2N+1 → ~4 queries)

Uma query por tipo de chave usando `IN (...)`:
- `FIND_PRODUCTS_BY_EANS` — `WHERE CODIGO_EAN13 IN (?, ?, ...)`.
- `FIND_PRODUCTS_BY_CODES` — `WHERE TRIM(CODPROD_ALTERN) IN (?, ?, ...)`.
- 1 query de cliente (inalterada).
- de-para: 1 lookup batelado no SQLite local.

Constrói dicionários `ean->row` e `code->row` e resolve os itens em memória,
preservando a ordem de prioridade atual por item. Chunk de **200** valores por
statement (limite de parâmetros do Firebird); dedup dos valores antes de montar
o IN.

Caveat registrado: `TRIM(CODPROD_ALTERN) IN (...)` não usa índice. Se pesar no
volume real do cliente, medir e considerar normalizar `CODPROD_ALTERN` ou um
índice funcional — **não** otimizar preventivamente.

Contrato de saída de `check_order` **inalterado** (mesmos campos por item e
summary) — só muda a implementação interna. Isso mantém preview, export e
send-to-fire funcionando sem tocar nos 5 callsites.

### 2.3 Progresso no botão

Feedback textual por etapa no `#pvCommitBtn`/`#batchSendBtn` (ex.: "Gerando
XLS…") em vez de spinner genérico. Mudança só de `index.html`.

---

## Testes

Novos e alterados:

- `tests/test_produto_depara.py` (novo): normalização de chave (code vs ean),
  unicidade `(cnpj, tipo, valor)`, **colisão entre varejistas com a mesma
  referência resolve para produtos diferentes**, undo (delete), lookup batelado.
- `tests/test_product_check.py` (ou o de smoke existente): 3º degrau
  (`match_source='depara'`), de-para órfão (SEQ sumiu) → sem match, e um teste
  que **conta execuções de cursor** para o caminho batelado — é ele que impede a
  volta silenciosa do N+1.
- `tests/test_exporter_split.py`: item resolvido por de-para sai com
  `CODIGO_PRODUTO=fire_codigo`, `EAN=fire_ean`, ref original no OBS; item sem
  de-para inalterado.
- `tests/test_web_server.py`: rotas `produtos/search`, `vincular-produto`,
  `depara/{id}` DELETE; e o push FlowPCP agora **enfileira no outbox**.
- `tests/test_flowpcp_hook.py`: `push_new_order` enfileira (não faz HTTP inline).

Validação manual no cliente (não pulável): pedido real com item resolvido por
de-para → gerar XLS → rodar a rotina de import do Fire → confirmar match. Isso
responde por evidência qual campo a rotina lê (§1.6).

Comando dirigido:
`.venv/bin/pytest tests/test_produto_depara.py tests/test_product_check.py tests/test_exporter_split.py tests/test_web_server.py tests/test_flowpcp_hook.py -v`
Suíte completa antes do commit final.

---

## Ordem de implementação

1. **Latência 2.1 + 2.2 + 2.3** — menor diff, valor imediato, não depende de
   nenhuma decisão de produto. Contrato de `check_order` inalterado.
2. **De-para 1.1–1.4** — schema + repo + 3º degrau + rotas de busca/vínculo.
3. **1.5 ranking + 1.6 exporter + undo** — assistência e persistência do valor
   no XLS.

Cada passo é um diff pequeno, uma intenção, com seu teste dirigido.

---

## Riscos / decisões conscientes

- **Normalização divergente** entre gravar e ler = vínculo fantasma. Mitigado
  por uma única função `_norm_key` usada nos dois lados, com teste.
- **De-para por CNPJ do cliente** (não global) é o que evita colisão entre
  varejistas — testado explicitamente.
- **Resolução do SEQ vai ao Firebird** (não confia no snapshot local) para preço
  e descrição atuais — a validação de preço não pode usar dado velho.
- **Ranking sugere, nunca aplica** — protege a confiança da usuária contra falso
  positivo silencioso.
- **Campo que a rotina do Fire lê** fica resolvido por teste no cliente, não por
  suposição; o exporter já escreve code+ean para cobrir ambos.
