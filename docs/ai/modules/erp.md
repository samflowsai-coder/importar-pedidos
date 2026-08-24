# Módulo: erp (Firebird / Fire Sistemas)

## Responsabilidade
Conectar no Firebird (embedded ou TCP) e inserir pedidos preservando o schema legado. **Idempotência por `PEDIDO_CLIENTE + CLIENTE`.**

## Arquivos críticos
- `app/erp/connection.py` — abre/fecha conexão (firebird-driver), modo embedded vs TCP.
- `app/erp/queries.py` — SQL parametrizado (CHECK_ORDER_EXISTS, INSERT_ORDER_HEADER, INSERT_ORDER_ITEM, lookup de cliente/produto).
- `app/erp/mapper.py` — mapeia `Order` → linhas do schema real (nomes de colunas).
- `app/erp/product_check.py` — checagem de existência de produto antes de inserir.
- `app/erp/exceptions.py` — exceções de domínio.
- `app/exporters/firebird_exporter.py` — orquestrador, chamado pelo pipeline com `EXPORT_MODE=db|both`.
- `tools/explore_firebird.py` — gera schema_report a partir de `.fdb` (rodar SEMPRE em cópia, nunca em produção).

## Padrões reais de produção (não inventar)
- `STATUS = 'PEDIDO'` (string, não enum).
- Flags booleanas como string: `'Sim' | 'Nao'`.
- Charset `WIN1252`.
- Idempotência: antes de inserir, `CHECK_ORDER_EXISTS` por `PEDIDO_CLIENTE + CLIENTE`.

## Variáveis de ambiente
```
EXPORT_MODE=xlsx|db|both
FB_DATABASE=/path/emp.fdb
FB_HOST=192.168.1.10  # omitir = embedded
FB_PORT=3050
FB_USER=SYSDBA
FB_PASSWORD=masterkey
```

## Configuração via UI (`/configuracoes/banco`)
A partir do redesign do shell, o admin pode editar `FB_*` pela página
`/configuracoes/banco` sem reiniciar o app:

- Persistência: `app/firebird_config.py` lê/grava `app/firebird.json` (campos plaintext +
  `password_enc`). Senha cifrada via `app/security/secret_store.py` (Fernet, chave em
  `app/.secret.key`).
- Aplicação imediata: `firebird_config.apply_to_env()` é chamada no startup do FastAPI **e**
  ao final de `POST /api/firebird/config`. Como `connection.py:_get_env` lê `os.environ` a
  cada conexão (sem cache), a próxima operação de import já usa a credencial nova.
- **Precedência**: config da UI > `.env`. Quando `firebird.json` tem um campo preenchido,
  ele sobrescreve `os.environ`. Campos vazios em `firebird.json` deixam o que já estava no
  ambiente intacto.
- API: `GET /api/firebird/config` (any user, sem senha), `POST /api/firebird/config` (admin),
  `POST /api/firebird/test` (admin, retorna `traceId` em erros).

## Testes
**Sem testes isolados.** Validar com `.fdb` de cópia + sample real, `EXPORT_MODE=both`.

## Armadilhas
- Nunca rodar `explore_firebird.py` no .fdb de produção.
- Nunca commitar `.fdb`, `jaybird.jar`, `bkp Fire/`, `bkp Fire Novo/`, `backup Fire/`.
- Charset errado quebra acentos silenciosamente.
- **`FB_CLIENT_LIBRARY` em `/tmp/...`**: reboot do macOS limpa `/tmp` e derruba o symlink pra dylib do Firebird. Erro: `"The location of Firebird Client Library could not be determined."` — confunde porque o `.env` aparenta correto. Apontar sempre pra `/Library/Frameworks/Firebird.framework/...` (macOS) ou path estável equivalente em Linux.
- **`fb_path` com aspas literais salvas no banco**: Finder ("Copy as Pathname") e cmd do Windows embrulham paths em aspas. `environments_repo._clean_path()` faz strip defensivo em `create`/`update`/`to_fb_config`. Sintoma no log antes do fix: `"io error: file not found"` mesmo com o arquivo no disco.

## Cliente override (CLIENT_NOT_FOUND recovery)
Quando o CNPJ parseado não bate com `CADASTRO`, o usuário pode escolher
manualmente o cliente via picker no portal. O override é metadado sidecar
em `imports.cliente_override_*` (ver `persistence.md`); aqui mora o suporte
SQL e a integração com o exporter.

- `SEARCH_CLIENTS` — busca por razão social (`UPPER LIKE`) ou CNPJ digits-only
  (`%LIKE%`), filtrada por `RELAC_CLIENTE='Sim'`, ordenada por `RAZAO_SOCIAL`,
  `FIRST 50` baked-in (Firebird não gosta de `ROWS ?` parametrizado em todas
  as versões).
- `FIND_CLIENT_BY_CODIGO` — exact lookup por `CADASTRO.CODIGO`. Usada para
  validar o override antes do INSERT (cliente pode ter sido inativado entre
  seleção no portal e clique em "Cadastrar no Fire").
- `FirebirdExporter.export(order, *, override_client_id=None)` — kwarg
  opcional. Quando setado, pula `FIND_CLIENT_BY_CNPJ` e usa
  `_validate_client_id`. Falha com a mesma `FirebirdClientNotFoundError`
  do caminho clássico se o codigo for inválido.
- O usuário que aplicou o override é gravado em `audit_log`
  (`user_email`, `user_id`) e em `imports.cliente_override_by` (email),
  vindo de `require_user` na rota `/api/imported/{id}/override-cliente`.

## Validação de preço (pedido vs Fire)

`product_check.check_order` agora popula por item:
- `unit_price_order`, `fire_preco_venda` (já existente), `price_diff`
- `price_status ∈ {match, mismatch, no_price_in_fire, no_order_price, no_product_match}` — comparação em centavos.

E `summary.price_summary` agrega contagens.

`product_check.is_blocking(check, ack_items=None)` decide se o estado bloqueia
envio. Bloqueia em `mismatch`, `no_order_price`, ou `no_price_in_fire` não
coberto por `ack_items`. `available=False` → não bloqueia (best-effort).

Os guards vivem em `_send_one_to_fire` e `_export_one_xlsx` (web). Audit
events: `send_to_fire_blocked`, `xlsx_export_blocked`, `sem_preco_acknowledged`.
Métricas: `portal_price_check_blocks_total{reason}`, `portal_price_check_acks_total`.

## De-para de cliente intercompany (Nasmar → cliente real)

`app/erp/depara_cliente.py` — `resolver_cliente_real(chave, *, revenda_slug)`.
Só lê o Firebird da revenda e devolve o cliente: não conhece `Order`, não
conhece Flow e não decide quando deve ser usado — isso é
`app/integrations/flowpcp/intercompany.py::resolucao_para(order, *, slug)`, a
camada de política, que casa o CNPJ do pedido contra
`environments.intercompany_cnpj` e, se bater, chama o resolver passando
`order.header.order_number` como chave.

Pedido no nome da revenda (ela fatura, a produção é nossa) sobe pro Flow com o
cliente REAL. A chave é o `PEDIDO_CLIENTE` (= `order.header.order_number`),
buscada na `CAB_VENDAS` do Firebird do ambiente da revenda; o `CADASTRO` de lá
dá o CNPJ.

- Resolve só com **um CNPJ distinto** entre os hits. Várias linhas com o mesmo
  CNPJ é normal e resolve; CNPJs diferentes = `ambiguo` → mantém a revenda.
- `motivo` ∈ `ok | sem_chave | nao_encontrado | ambiguo | sem_cnpj | config_invalida | erro_conexao`.
  `sem_cnpj`: CNPJ resolvido não tem 11 (CPF) ou 14 (CNPJ) dígitos — `CADASTRO.CPF_CNPJ`
  legado às vezes guarda `"ISENTO"`, `"0"` ou cadastro incompleto. Aceitar qualquer
  string não-vazia derrubaria o push (o Flow rejeita `cnpj` fora de 11–18 chars com
  400) — pior que subir como revenda.
- **`ResolucaoCliente.revenda_slug`** vem preenchido em **todo** caminho, inclusive nos
  de falha. É a única forma de auditar depois "quais pedidos foram resolvidos sob uma
  config errada" se `intercompany_env_slug` for corrigido mais tarde. Campos completos:
  `resolvido`, `cnpj`, `nome`, `motivo`, `revenda_slug` e `pedidos_no_4` (STATUS/CODNF
  dos pedidos casados no `.4` — radar da demanda fantasma).
- **Cool-down de 45s por `revenda_slug` depois de um `erro_conexao`.** O `fdb` não expõe
  timeout: um host inalcançável trava a conexão por até ~180s. Sem o cool-down, CADA
  preview/refresh/export durante uma queda do Firebird da revenda paga o stall inteiro,
  por clique (handlers síncronos em threadpool limitado). Uma conexão bem-sucedida limpa
  o cool-down do slug. Clock injetável (`_clock`) para o teste não dormir de verdade.
  **Armadilha conhecida:** o cool-down arma em volta do bloco inteiro (`to_fb_config` +
  `connect` + `execute` + `fetch`), então um erro que não é de conexão (linha ruim,
  charset) suprime o de-para do ambiente por 45s — ver `docs/BACKLOG.md` §1.4.
- Cache de processo só para resolução **positiva** (`limpar_cache()` nos testes). Negativo
  nunca entra: o pedido pode ser criado na revenda depois, e o servidor web fica de pé
  por dias.
- **Produto nunca vem da revenda** — só o cliente. O `.7` segue sendo a fonte
  de produto/preço.
- Nunca levanta. Config em `environments.intercompany_cnpj` + `intercompany_env_slug`.

Testes: `tests/test_depara_cliente.py`.
## De-para de produto (memória de match por cliente)

A referência do varejista que não casa no Fire vira um vínculo persistente:
consertado uma vez, lembrado pra sempre. Motivado por dado real de produção —
111 itens sem match eram só 31 referências distintas, dominadas pela Riachuelo
(mesma ref repetindo em dezenas de pedidos).

- **Tabela `produto_depara`** (db do ambiente, `schema_env.py`): `client_key`,
  `chave_tipo` (`'codigo'|'ean'`), `chave_valor` (normalizado), `fire_produto_id`,
  `fire_codigo`, `fire_ean`, `fire_nome`, `criado_em/por`. `UNIQUE (client_key,
  chave_tipo, chave_valor)`. Repo: `app/persistence/produto_depara_repo.py`.
- **`client_key(cnpj, name)`** — CNPJ (só dígitos) quando existe; **senão o nome
  normalizado** (UPPER, whitespace-collapsed). Varejistas como a Riachuelo vêm sem
  CNPJ no header (o CNPJ real é por loja no `delivery_cnpj`) → chavear pelo nome é
  a granularidade certa: uma ref → um produto Fire, através de todas as lojas.
  Escrita (rota `vincular-produto`) e leitura (`check_order`) derivam a chave com
  a MESMA chamada — se divergirem, o vínculo criado na UI não é achado.
- **`_norm_key(tipo, valor)`** normaliza a chave da referência (código: strip+upper;
  ean: só dígitos), idêntico na gravação e na leitura.

### 3º degrau de match no `check_order`

Ordem: **EAN → CODPROD_ALTERN → de-para**. Quando EAN e código falham, olha o
de-para do `client_key` do pedido (SQLite local, rápido) e resolve o
`fire_produto_id` no Firebird via `FIND_PRODUCT_BY_SEQ` — trazendo DESCRICAO +
PRECO_VENDA, então a validação de preço segue funcionando. Novo
`match_source='depara'`. De-para órfão (SEQ sumiu do Fire) → `match=False`.
Best-effort: sem ambiente ativo / SQLite off, pula o degrau sem quebrar o check.

### Gap conhecido: de-para só no caminho XLS

Enriquecimento de-para no XLS só ocorre no caminho `EXPORT_MODE=xlsx`
(`_export_one_xlsx`, via `app.erp.depara_apply`); o caminho de inserção direta
no Firebird (`_send_one_to_fire`, modo `db`/`both`) ainda NÃO aplica de-para —
um item casado só por vínculo de-para entra no Fire sem FK de produto.
Fast-follow se o cliente migrar de `xlsx`.

### Batelamento (perf)

`check_order` batela os lookups de produto: de `2N+1` round-trips no Firebird
(1 cliente + até 2 por item) para **~4** — uma query `IN (...)` por tipo de chave
(`find_products_by_eans_sql` / `_codes_sql` / `_seqs_sql`), chunk de 200, dedup
antes. Contrato de saída inalterado. Caveat: `TRIM(CODPROD_ALTERN) IN (...)` não
usa índice — se pesar no volume real, medir antes de otimizar.

## Reconciliação com o Fire (pedido cadastrado à mão)

`app/erp/fire_reconcile.py` — leitura pura do Firebird para achar, entre os
pedidos `parsed` do portal, quais já foram cadastrados manualmente no Fire.
Motivo de existir: o cliente roda `EXPORT_MODE=xlsx`, a operadora exporta o
XLS e cadastra à mão no Fire, e o portal nunca fica sabendo — o pedido fica
`parsed` pra sempre. O módulo não conhece `Order`, não decide estado, não
escreve nada; devolve um dict `import_id → Achado` para
`app/reconcile/runner.py` decidir e gravar (ver `state.md`, seção
`found_in_fire`).

### Chave dupla, três caminhos

`_decidir_candidato` (`fire_reconcile.py:305`) tenta, na ordem, o primeiro
caminho cuja âncora de cliente está preenchida no `Candidato` — sem
fallback entre eles, e nenhum casa só pelo número:

1. **`cliente_override_codigo`** (override manual do picker, ver "Cliente
   override" acima) — casa por `CAB_VENDAS.CLIENTE` igual ao código
   (`fire_reconcile.py:322-326`).
2. **`customer_cnpj` do header** — casa por CNPJ do `CADASTRO` da linha,
   comparado em dígitos (`fire_reconcile.py:328-335`).
3. **Sem CNPJ no header** (Riachuelo, NBA — o CNPJ real é por loja, em
   `delivery_cnpj` de cada item) — compara o conjunto de `delivery_cnpj`
   distintos do pedido contra o Fire (`fire_reconcile.py:337-356`).

### Regra "todas as lojas"

O caminho 3 só marca quando **cada** CNPJ de entrega do pedido tem pelo
menos uma linha casada no Fire — um pedido com 3 lojas e só 2 cadastradas
continua `parsed` (`fire_reconcile.py:350-356`). Meio-cadastrado é
trabalho pendente, não confirmado; silêncio é melhor que marca errada.

A mesma checagem fecha uma armadilha: um `delivery_cnpj` sem dígito (texto
livre tipo `"A COMBINAR"` ou `"N/A"`) normaliza para `""`, e
`CADASTRO.CPF_CNPJ` nulo/vazio no Fire normaliza para o MESMO `""`. Sem o
guard `"" in alvo_cnpjs` (`fire_reconcile.py:350`), os dois vazios se
encontrariam e o pedido casaria sem nenhuma âncora de cliente real. A
regra que vale: **um CNPJ de entrega não-verificável bloqueia o pedido
inteiro** — não é descartado do conjunto — porque com uma loja que não dá
para provar, não dá para provar "todas as lojas".

### Guarda temporal de 90 dias

Números curtos e sequenciais — `K01` (Kallan), `MF048` (Magic Feet),
`AF198` (Authentic) — se repetem entre anos no MESMO cliente. Um `K01`
novo casaria com o `K01` do ano passado: mesmo número, mesmo CNPJ, chave
dupla fechada e errada. `_JANELA_DIAS = 90` (`fire_reconcile.py:43`)
descarta qualquer linha do Fire cuja `DATA_PEDIDO` seja anterior a "data
do candidato − 90 dias" (`_dentro_da_janela`, `fire_reconcile.py:279-302`).
Candidato sem `data_pedido` não aplica a guarda (passa tudo); candidato ou
linha do Fire com data ilegível descarta por segurança, não abre exceção.

### Variantes do número

`app/erp/numero_pedido.py::variantes(numero)` gera as formas aceitas como
"mesmo número", da mais específica pra menos: exata, sem sufixo `-NNNN`
(hífen seguido de exatamente 4 dígitos no fim), sem zeros à esquerda.
Nunca substituem a segunda perna da chave — só ampliam o que conta como
"mesmo número" antes de checar a âncora de cliente.

**Caso real medido:** Sam's Club guarda `06654993-0000` no portal e
`06654993` no Fire (`numero_pedido.py:3-5`) — sem cortar o sufixo, 100% de
falso negativo em Sam's.

### Query em lote

`FIND_ORDERS_BY_PEDIDO_CLIENTE(n)` (`queries.py:317-344`) generaliza
`FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE` (`queries.py:307`) para receber N
números de uma vez — lotes de `_BLOCO = 200` (`fire_reconcile.py:36`), 308
pedidos pendentes viram ~2 idas ao banco. As colunas duplicadas **não têm
alias** no SQL: índice 1 = `V.CODIGO` (pedido no Fire), índice 4 =
`C.CODIGO` (cliente no Fire) — quem lê por nome de coluna quebra; a
posição é o que distingue.

**Sem filtro de `STATUS`.** Medido no backup real do cliente
(`bkp Fire Novo/MM_AMERICANENSE.fdb`) em 2026-08-24: `FATURADO` 3365,
`PEDIDO` 322, `CANCELADO` 22, `EM ANÁLISE` 1. `found_in_fire` significa
"existe no Fire" — um pedido `CANCELADO` existe (foi cadastrado e depois
cancelado). Filtrar deixaria ele preso na fila de revisão pra sempre sem
a operadora entender por quê. O status vem junto (`Achado.fire_status`,
gravado em `imports.fire_status_last_seen`) e a UI mostra: "Cadastrado no
Fire (CANCELADO)".

### Degradação — nunca levanta

`buscar_no_fire` (wrapper público) / `_buscar_no_fire_detalhado`
(implementação, com o sinal de conexão) nunca levantam — qualquer falha
vira `{}` + log. Cool-down de 45s por ambiente (`_COOLDOWN_S`,
`fire_reconcile.py:51`), armado **só** em volta do `connect_with_config` —
a mesma lição do cool-down largo demais do `depara_cliente`
(`docs/BACKLOG.md` §1.4), feita certo desde o início aqui: erro de
leitura/dado depois da conexão (SQL malformado, linha suja) não arma
cool-down, só loga.

`_buscar_no_fire_detalhado` devolve `(achados, erro_conexao)` — o booleano
é o que permite `app/reconcile/runner.py` distinguir, no
`Resultado.status` (`ok | erro_conexao | em_execucao | trava_ativa`,
`app/reconcile/runner.py:95`) que `POST /api/imported/reconciliar-fire`
devolve (`app/web/server.py:1270`), "consultei e não achei nada" de "não
consegui consultar o Fire". Sem essa distinção, `casaram=0` nos dois casos
faria a operadora concluir, errado, que a feature quebrou.

### Testes
`tests/test_fire_reconcile.py` — os 3 caminhos, "todas as lojas", variante
sem sufixo (caso Sam's), zeros à esquerda, guarda temporal, CNPJ
divergente, Fire fora devolve vazio sem levantar, cool-down só em erro de
conexão, lote > 200.
