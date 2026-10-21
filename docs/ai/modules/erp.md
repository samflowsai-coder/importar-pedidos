# Módulo: erp (Firebird / Fire Sistemas)

## Responsabilidade
Conectar no Firebird (embedded ou TCP) e inserir pedidos preservando o schema legado. **Idempotência por `PEDIDO_CLIENTE + CLIENTE`.**

## Arquivos críticos
- `app/erp/connection.py` — abre/fecha conexão (firebird-driver), modo embedded vs TCP.
- `app/erp/queries.py` — SQL parametrizado (CHECK_ORDER_EXISTS, INSERT_ORDER_HEADER, INSERT_ORDER_ITEM, lookup de cliente/produto).
- `app/erp/mapper.py` — mapeia `Order` → linhas do schema real (nomes de colunas).
- `app/erp/product_check.py` — checagem de existência de produto antes de inserir.
- `app/erp/exceptions.py` — exceções de domínio.
- `app/erp/fiscal.py` — `PerfilFiscal` + `perfil_para(env)`: constantes fiscais do
  pedido faturável, por ambiente (`CODFIGFISCAL` difere: 1 na MM, 5 na Nasmar).
- `app/exporters/firebird_exporter.py` — orquestrador, chamado pelo pipeline com `EXPORT_MODE=db|both`.
- `app/erp/produto_chave.py` (decisão pura por chave exata), `produto_escopo.py` (escopo rede/raiz do vínculo),
  `produto_trava.py` (portão por `produto_chave_modo` e mapa do produto decidido),
  `app/persistence/produto_vinculo_repo.py` (de-para v2) — ver "Produto por chave exata" e o runbook abaixo.
- `tools/migrar_produto_vinculo_v2.py`, `tools/regressao_chave_exata.py` — migração v1→v2 e regressão só leitura.
- `tools/explore_firebird.py` — gera schema_report a partir de `.fdb` (rodar SEMPRE em cópia, nunca em produção).

## Padrões reais de produção (não inventar)
- `STATUS = 'PEDIDO'` (string, não enum).
- Flags booleanas como string: `'Sim' | 'Nao'`.
- Charset `WIN1252`.
- Idempotência: antes de inserir, `CHECK_ORDER_EXISTS` por `PEDIDO_CLIENTE + CLIENTE`.

## Perfil fiscal por ambiente + inserção completa

`CAB_VENDAS` e `CORPO_VENDAS` eram inseridos com um subconjunto de colunas
até 2026-08; hoje cobrem o que a Fire viva realmente grava, medido contra
373 pedidos do `.7` (MM) e 90 do `.4` (Nasmar), 2026-06-01 em diante:

- **`INSERT_CAB_VENDAS`** (`queries.py`) — 23 colunas posicionais. As 14
  primeiras estão preenchidas em 100% dos pedidos medidos; `CLASSIF_FAT`,
  `CODFIGFISCAL`, `DT_BASE_FAT` e `MECANICO` em 82%–100%. `CODPED_PAI`
  (auto-referência = `CODIGO`) não entra no INSERT — só existe depois dele
  — vai por `UPDATE_CODPED_PAI` na mesma transação.
- **`INSERT_CORPO_VENDAS`** — completo (`ICMS_PORC`, `ICMS_BASE`, `REDUCAO`,
  `DESC_SOBRE_TOTAL`, `PESO_BRUTO`, `PESO_LIQUIDO` em 100% das 1.285 linhas
  medidas; `CFOP_PRINCIPAL` em 96%). `UNID` vem do cadastro do produto no
  Fire (`PRODUTOS.UNIDADE`) — até 2026-08 era cravado `"UN"`, o que estava
  errado para kits (`"KIT"` na produção real).
- **`PerfilFiscal`** (`app/erp/fiscal.py`) — as constantes fiscais que não
  variam por item: `tipo_cob`, `cod_class_finan`, `desc_class_finan`,
  `classif_fat`, `mecanico`, `icms_porc`, `reducao`, `cfop_principal`, e
  `codfigfiscal` (o único que difere por empresa: 1 na MM, 5 na Nasmar —
  por isso mora em `environments.fiscal_codfigfiscal`, não no dataclass).
  `perfil_para(env)` cai no default medido quando o campo está ausente/NULL.
- **`FirebirdExporter._find_product(cur, row)`** mudou de devolver só
  `product_seq: int | None` para **`tuple[int | None, str]`** — `(product_seq,
  unid)`. `unid` alimenta `CORPO_VENDAS.UNID`; `"UN"` é o fallback quando o
  produto não é encontrado ou a coluna vem vazia/NULL no Fire. Qualquer
  call-site que ainda desestruture o retorno antigo como um único `int`
  quebra silenciosamente (vira tupla, não `int`) — grep por `_find_product(`
  antes de mexer na assinatura de novo.
- **`item_total`/`parse_date`** (antes `_item_total`/`_parse_date`, ambos em
  `app/erp/mapper.py`) foram promovidos a nomes públicos — são importados
  por `app/exporters/firebird_exporter.py` através da fronteira de módulo
  (soma de `CAB_VENDAS.VALOR_TOTAL` e menor `DT_ENTREGA`), e um prefixo `_`
  cruzando módulo escondia que é API deliberada, não vazamento de detalhe
  interno.

Testes: `tests/test_erp_mapper_colunas.py`, `tests/test_smoke_erp_mapper.py`,
`tests/test_smoke_exporter.py`.

## Variáveis de ambiente
```
EXPORT_MODE=xlsx|db|both
FB_DATABASE=/path/emp.fdb
FB_HOST=192.168.1.10  # omitir = embedded
FB_PORT=3050
FB_USER=SYSDBA
FB_PASSWORD=masterkey
PORTAL_PRODUTO_CHAVE_CLI=   # ligado = trava de produto no main.py (CLI); ausente = comportamento de hoje
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
- `fire_unidade` — `PRODUTOS.UNIDADE` (strip; vazio → `None`), última coluna das 3 queries
  batch (`find_products_by_eans_sql`/`_codes_sql`/`_seqs_sql`). Usada pela trava de preço.
- `price_status ∈ {match, mismatch, no_price_in_fire, no_order_price, no_product_match}` — comparação em centavos.

E `summary.price_summary` agrega contagens.

`product_check.is_blocking(check, ack_items=None)` decide se o estado bloqueia
envio. Bloqueia em `mismatch`, `no_order_price`, ou `no_price_in_fire` não
coberto por `ack_items`. `available=False` → não bloqueia (best-effort).

**Ack de "sem preço" dispensado na planilha** (`preco_trava._regra_de_hoje`, predicado
`preco_trava.sem_preco_dispensado(env, planilha=)`; `is_blocking` não muda: a regra o chama
de novo tratando os itens sem ack como confirmados): vale só com a integração direta
desligada (`EXPORT_MODE=xlsx`, o `_export_mode()` configurado), só quando o chamador passa
`planilha=True` a `decidir_envio` (a rota de export; o **envio ao Fire nunca**, porque insere
no ERP mesmo com `EXPORT_MODE=xlsx`) e nunca em `ligado`. O item `no_price_in_fire` sem ack
deixa de bloquear e sai em `block_detail["items_sem_preco_dispensados"]` (chave que só existe
com a dispensa); o portão grava o audit `sem_preco_dispensado`
`{itens: [{ean, product_code}]}` e, em `observando`, `preco_observado.sem_preco_dispensados`
(quantos). `mismatch` e `no_order_price` seguem bloqueando. Com `db`/`both` o rigor volta.
Por quê: o Fire não guarda preço em `PRODUTOS` (`PRECO_VENDA` zerado) e a planilha é revisada
no importador do Fire; o ack por pedido travava o lote da operadora (21/10/2026).

### Produto por chave exata (`item["produto"]`, observado)

`check_order` grava em todo item `produto = {estado, fire_seq, fonte, motivo,
candidatos: [{seq, descricao, codprod_altern, bloqueado}]}` e
`summary.items_produto_pendente` (estado ≠ `ok`). A decisão é a função pura
`app/erp/produto_chave.decidir` (estados `ok | a_identificar | ambiguo |
produto_bloqueado | vinculo_invalido | chaves_discordam | chave_repetida`; fonte
`ean | depara | codprod_altern | escolha_pontual`). Candidatos vêm de `find_produtos_chave_por_{ean,codigo,seq}_sql`
(todas as linhas da chave + `BLOQUEADO`), numa passada batelada a mais no mesmo
cursor. EAN = só dígitos, igual exato; código = `strip().upper()`, igual exato.
O de-para é o v2 (`produto_vinculo_repo.lookup` no escopo de
`produto_escopo.cnpj_do_item`/`escopos_para`, cliente real do intercompany via
`resolucao_para`). Vínculo inválido (SEQ sumido/bloqueado) é final. Chave com
problema (`ambiguo`/`produto_bloqueado`) só deixa passar o SEQ que está entre os
candidatos dela; fora disso, ou vínculos do código e do EAN diferentes →
`chaves_discordam`. `ok` traz o candidato decidido em `candidatos`.
**Chave repetida no pedido (R18, `produto_chave.desambiguar`):** chave do cliente
(código normalizado, EAN) que aparece em 2+ itens do MESMO pedido com descrições
diferentes (`descricao_normalizada`: maiúsculas, sem acento, espaços colapsados;
`chaves_repetidas`) não discrimina. Item `ok` cujo SEQ só vinha dela (fonte `ean`,
`codprod_altern`, ou `depara` cujo vínculo é dessa chave) vira `chave_repetida`,
`fire_seq` None, `candidatos` = o produto que a chave achava, motivo "O código X
aparece em N linhas com descrições diferentes; escolha o produto de cada linha."
(ou "O EAN X …"). Outra chave do item concordante e não repetida mantém o `ok` por
ela. Mesma chave e mesma descrição (ex.: uma linha por loja) segue igual.
**Escolha pontual:** `check_order(order, env=..., escolhas={posição: SEQ})` —
`escolhas_do_import(entry)` lê `imports.produto_escolhido_json`. Item com escolha
decide antes de qualquer chave (`decidir_escolha`): SEQ ativo → `ok` com fonte
`escolha_pontual`; sumido/bloqueado → `vinculo_invalido` ("A escolha desta linha
aponta para o produto X, que não existe mais ou está bloqueado."). Vale só para
aquele import: todo chamador que confere um import salvo passa as escolhas dele
(`decidir_envio`, recheck do vincular, ack-sem-preço, a própria rota); o check da
fonte de preço (`_check_da_fonte`) não (SEQs de outro Fire). Com `preco_fonte_slug` ≠ o
próprio, a linha com escolha vai à fonte com o `CODPROD_ALTERN` do produto escolhido
**lido do Fire no check ao vivo** do ambiente do pedido (R21: item `ok` por
`escolha_pontual` no mesmo SEQ; nunca o `codigo` do catálogo guardado na escolha) como
única chave (`_pedido_para_a_fonte`, sem EAN). Sem esse candidato ao vivo (Fire fora,
`check_preco_para_gravar` sem `live`) ou sem código no Fire, a linha vai sem chave e
bloqueia. As rotas de escolha, vincular e mover passam o check fresco como `live`. Se a fonte resolve a um
único produto ativo pelo código (`estado` `ok`, fonte `codprod_altern`; `depara` da fonte não
vale: ele desempata o código do cliente e não prova unicidade do código), o item é
avaliado normalmente com esse SEQ (`escolha_na_fonte: True` no check; conforme ou
divergente → Flow como qualquer item). Todo item com escolha leva no check da fonte
`escolha_seq`/`escolha_codigo`; a marca só vale se `escolha_seq` for a escolha atual do
import (check salvo de uma escolha antiga = sem avaliação). `chave_repetida` na fonte
(duas linhas chegam com o mesmo código e descrições diferentes) sai
`escolha_pontual_chave_repetida`, que diz isso. Unidade do produto escolhido (Fire ao vivo;
sem ela, `catalogo_fire.unidade` daquele SEQ) diferente da do produto da fonte, ou
desconhecida (kit numa empresa, par na outra) → `escolha_pontual_unidade_divergente`. O resto (sem código, ambíguo, de-para
para outro produto, check salvo sem a marca) sai `escolha_pontual_sem_fonte`: portão
bloqueia e o preview diz o que fazer (cadastrar/ajustar o código na fonte ou deixar a
empresa em `observando`). A escolha regrava o `check_preco`. Intercompany não muda. A linha com escolha
válida também tem os campos legados trocados (`match`, `match_source =
"escolha_pontual"`, `fire_product_id`, `fire_description`, preço/unidade e
`price_status` do escolhido; código da previsão de troca = o `CODPROD_ALTERN` dele),
em todo modo (R20). Fora de `ligado` a saída leva a escolha por cima do v1:
`depara_apply.sobrepor_escolhas(order, vinculos, entry["produto_escolhido"])` (SEQ
no insert, `codigo` gravado na escolha na planilha, EAN do escolhido ou nenhum,
OBS "ref cliente: <orig>"), chamado por `_vinculos_de_para` (envio, export,
preview). **R21:** produto escolhido sem `CODPROD_ALTERN` nunca vai como str(SEQ)
(o importador do Fire casaria por prefixo): a linha fica com o código do cliente e
`sem_codigo_fire`; `codigo_vivo_das_escolhas` prefere o código do candidato decidido
no check ao vivo (`DecisaoEnvio.check_vivo`, preenchido em todo modo) ao do catálogo
guardado. Envio xlsx/both e export-xlsx recusam com 409 `sem_codigo_fire` ("O produto
X escolhido para a linha N não tem código no Fire; …") em qualquer modo, só para
escolha pontual (`_recusa_planilha_pontual`, audit `send_to_fire_blocked` /
`xlsx_export_blocked`); `db` passa pelo SEQ. Em `ligado` quem trava é o portão
(`sem_codigo_fire` de `itens_planilha`). **M1:** escolha pontual que o check ao vivo
marca `vinculo_invalido` (SEQ sumiu ou foi bloqueado no Fire;
`depara_apply.pontuais_invalidas`) não sai em NENHUM modo nem export_mode, insert
incluído: 409 `vinculo_invalido` "O produto X escolhido para a linha N não está mais
ativo no Fire; escolha outro." (mesmo `_recusa_planilha_pontual`, `planilha=False` no
`db`). Fire fora (`fire_indisponivel`) não recusa.
`produto_conferencia_incompleta` (topo e `summary`) = `True` quando o vínculo v2
não pôde ser lido (banco do ambiente fora, intercompany sem resposta). Com
`env` explícito o vínculo vem de `router.env_connect(env["slug"])`, não do
contexto ativo, e qualquer falha marca. Sem `env`, `db.connect()`; sem ambiente
ativo não marca. Fire fora (`available=False`) ou falha só nesta passada →
`estado = "fire_indisponivel"` (do check, não de `decidir`). Os campos legados
(`match`, `match_source`, `fire_product_id`, …) não mudam. Só bloqueia com o
interruptor abaixo em `ligado`.

**Interruptor `environments.produto_chave_modo`** (`desligado` nasce | `observando` |
`ligado`): troca só por `PATCH /api/admin/environments/{id}/produto-chave`
(admin, `{modo}`; 404/422; log via `set_produto_chave_modo`). Portão puro em
`app/erp/produto_trava.py`: `modo(env)` (None/inválido → desligado) e
`avaliar(check, *, env, export_mode) -> TravaProduto(bloqueado, reason, itens)`.
`itens` = `[{index, estado, motivo}]` de todo item com `produto.estado != "ok"` (item
sem `produto` conta como não resolvido), preenchido em `observando` e `ligado`; em
`desligado` vem vazio. Só `ligado` bloqueia: `fire_indisponivel` (check
`available` falso/ausente ou `produto_conferencia_incompleta`) tem precedência sobre
`produto_a_identificar`. Com `export_mode` `xlsx` ou `both` (sai planilha), `ligado`
ainda trava, nesta ordem, com `sem_codigo_fire` (item `ok` cujo candidato decidido não
tem `CODPROD_ALTERN`: "O produto {seq} não tem código no Fire; a planilha não consegue
apontá-lo.") e `troca_no_fire` (`produto.troca_no_fire` preenchido, ou a chave ausente:
"O importador do Fire trocaria este código pelo produto {seq_previsto}." ou, com
`seq_previsto` None, "O código {codigo} do produto {seq} não pode ser conferido na
planilha do Fire."). Em `db` essas duas não valem (o insert entra pelo SEQ). Em
`observando` os itens delas também entram em `itens`. `itens` dessas regras usam
`estado` = o próprio motivo (`sem_codigo_fire`/`troca_no_fire`).

**Saída com o produto decidido (`ligado`, spec §6).** `DecisaoEnvio.check` = o check ao
vivo que a trava aprovou (None fora de `ligado`). `produto_trava.produtos_decididos(check)
-> (vinculos, produtos_por_item)`: todo item `ok` (inclusive quando o código do cliente já
é o `CODPROD_ALTERN`) vira um vínculo no formato do `depara_apply.aplicar`
(`fire_codigo` = `CODPROD_ALTERN` do candidato decidido, lido do Fire no check, nunca do
catálogo local; `fire_ean` = EAN do candidato ou "" com `substitui_ean: True`, e o
`aplicar` troca ou LIMPA o EAN do cliente, nunca o mantém — o v1, sem a flag, segue como
antes; `orig` = código do cliente, que vai na OBS como `ref cliente: …`) e
`{posição: SEQ}`. Os candidatos em `item["produto"]["candidatos"]` levam também
`unidade` e `ean` (os builders `find_produtos_chave_por_*_sql` trazem
`TRIM(CODIGO_EAN13)` como 8ª coluna; os builders legados não mudam). Sem `CODPROD_ALTERN` o código do cliente fica (só
o insert aceita esse item). Em `ligado`, `decidir_envio` avalia o preço por esse mapa (o
de-para v1 passado é ignorado), e `_send_one_to_fire`/`_export_one_xlsx` (via
`_saida_do_envio`) aplicam ESSE mapa no `depara_apply.aplicar` e no
`FirebirdExporter.export(produtos_por_item=…)`, sem segundo `check_order` e sem ler o
de-para v1 (`_vinculos_antes_do_portao` só chama `_vinculos_de_para` fora de `ligado`;
se o modo mudou no meio, `_saida_do_envio` lê o v1 na hora). Na trava de preço,
`_com_vinculos(..., decidido=True)` (produto `ligado`, envio e preview) pega
`fire_unidade`/`fire_description` do candidato decidido em vez de zerar quando o
`fire_product_id` legado é outro SEQ. Com a trava de preço fora de `ligado` (regra de
hoje, `is_blocking`), `_decidir_preco` passa a regra sobre `_preco_do_decidido(order,
live)` (I4): cópia do check em que cada item `ok` tem `price_status`/`price_diff`/
`fire_preco_venda` refeitos por `_classify_price` com o `preco_venda` do candidato
decidido (o candidato serializa `preco_venda`; sem a chave, o item fica como está) — o
match legado podia ser outro produto. O check aprovado (`DecisaoEnvio.check`) não muda. O preview
avalia o preço pelo `produtos_decididos(entry["check"])`. `decidir_envio(...,
export_mode=)`: o que o caminho gera (None = `EXPORT_MODE`); `/export-xlsx` passa sempre
`"xlsx"`. O CLI em `ligado` aplica o mesmo mapa antes da planilha. `desligado` e
`observando`: `_vinculos_de_para`, como antes.

**Onde a trava de produto atua** (todo caminho que grava no Fire ou gera XLSX de pedido):
- `preco_trava.decidir_envio` (send-to-fire, export-xlsx e os lotes): logo depois do
  `check_order(order, env=env)` do ambiente do pedido e **antes** de qualquer lógica de preço,
  em todo modo de preço. `ligado` → `DecisaoEnvio(bloqueado, reason=fire_indisponivel |
  produto_a_identificar, detalhe="Item N: <motivo>" [+ "Mais K item(ns)…"], block_detail=
  {"produto": itens}, metric_labels=["produto"])`; o chamador audita
  (`send_to_fire_blocked`/`xlsx_export_blocked`), conta `portal_price_check_blocks_total
  {reason="produto"}` e responde 409. `observando` → audit `produto_observado {itens}` (só quando `itens` não é vazio)
  (falha nunca muda o envio).
- `_run_exporters` (`/api/import`, `/api/reimport`): `ligado` não gera XLSX e devolve
  `xlsx_adiado: "produto_a_identificar"` (precede `trava_de_preco`); pedido fica em revisão.
- `/api/process` (legado): `ligado` → 409 "Trava de produto ligada: use a revisão de pedidos."
- `main.py` (CLI, só xlsx): sem ambiente, logo sem interruptor no banco. Só com a env var
  `PORTAL_PRODUTO_CHAVE_CLI=ligado` (ausente ou outro valor = hoje, nem chama `check_order`):
  roda `check_order(order)` + `produto_trava.avaliar` com env sintético `ligado` e pula o pedido
  (log `Pedido N não exportado: …`) se o Fire não respondeu, a conferência ficou incompleta ou
  algum item está fora de `ok`. Erro no check pula só aquele pedido, nunca o lote.
- Sem ambiente (sem cookie/legado): `preco_trava.env_trava_produto(None)` vale `ligado` se
  QUALQUER ambiente ativo estiver `ligado` (o mais restrito).

Os guards vivem em `_send_one_to_fire` e `_export_one_xlsx` (web). Audit
events: `send_to_fire_blocked`, `xlsx_export_blocked`, `sem_preco_acknowledged`.
Métricas: `portal_price_check_blocks_total{reason}`, `portal_price_check_acks_total`.

### Runbook: ligar o casamento por chave exata

Sequência operacional (cada passo com OK do Samuel; nada daqui roda sozinho):

1. **Deploy** do pacote pelo `/admin/atualizacao`. Cria a tabela `produto_vinculo_v2` e as
   colunas novas (`environments.produto_chave_modo`, `imports.produto_escolhido_json`). O
   interruptor nasce `desligado`: comportamento idêntico ao de antes. Cria também
   `ambiente_modo_audit` no `app_shared.db` (troca do interruptor, M5).
   **Logo depois de aplicar, rodar o sync do catálogo** (M3): o catálogo local
   existente fica com `codprod_altern` NULL até o próximo sync, e o picker e o v2
   gravam o código do Fire a partir dele.
2. **Migração do de-para v1** (por ambiente, `mm` e depois `nasmar`):
   `python tools/migrar_produto_vinculo_v2.py --slug mm` é DRY-RUN (só mostra); com
   `--aplicar` grava pelo `produto_vinculo_repo.vincular` (origem `migrado_v1`, uma
   transação por vínculo). Nunca apaga o v1, nunca sobrescreve v2 que aponte para outro
   produto, não recria chave já revogada (`revogado_antes`) e é idempotente. Linha v1 só com
   nome vai para `sem_cnpj_unico` (a operação vincula de novo na tela) e a da revenda
   intercompany não migra (`revenda_intercompany`).
3. **`observando`** por alguns dias (`PATCH /api/admin/environments/{id}/produto-chave`
   `{modo: "observando"}`): o check calcula e a tela mostra, a saída continua igual à de
   hoje. Ler o audit `produto_observado` (itens que `ligado` travaria) e a fila
   `GET /api/produtos/a-identificar`.
4. **`ligado`** só depois de (a) o `observando` limpo, (b) a regressão com cobertura
   (abaixo) e (c) a revisão da chave repetida/escolha pontual. **Nasmar fica em
   `observando`** até a regressão dela ter cobertura suficiente (M8: na regressão 2,
   48 arquivos sem pedido no Fire na janela de 72 h); a MM pode ir antes. Reverter = voltar o
   interruptor para `observando`/`desligado`; vínculos v2 nunca são apagados (só revogados).

**Regressão real** (`tools/regressao_chave_exata.py`, SÓ LEITURA): relê os originais
guardados (`--recebidos data/recebidos`, varredura recursiva) por `pipeline.process` com o
LLM desligado, roda o `check_order` novo contra cópias dos `app_state_<slug>.db` (`--db`,
repetível) e do Fire (`FB_USER`/`FB_PASSWORD` no ambiente; `--fb-host --fb-path`), acha o
pedido que o importador gravou e compara SEQ decidido x gravado item a item. Baldes:
`ok_igual`, `ok_igual_conjunto`, `suspeito`, `ok_seq_diferente` (tem que dar zero),
`pendente`, `sem_pedido_fire`, `ambiguo`, `nao_enviado`, `sem_pareamento`, `sem_data`.
Exit 0 só sem `ok_seq_diferente`/`suspeito`/`erro_check` e com cobertura (no máximo 50%
dos arquivos fora de comparação). Teste: `tests/test_regressao_chave_exata.py`.

**Variável de ambiente nova:** `PORTAL_PRODUTO_CHAVE_CLI` (`ligado` aplica a trava de
produto no `main.py`; ausente ou outro valor = CLI de hoje). Única da feature: o
interruptor do web é por ambiente (banco), não por env var.

### Testes (chave exata)
`tests/test_produto_chave.py`, `tests/test_produto_chave_repetida.py`,
`tests/test_produto_depara.py`, `tests/test_produto_escopo.py`,
`tests/test_produto_vinculo_repo.py`, `tests/test_produto_trava.py`,
`tests/test_produto_trava_wiring.py`, `tests/test_produto_saida.py`,
`tests/test_firebird_exporter_produto.py`, `tests/test_migrar_produto_vinculo_v2.py`,
`tests/test_regressao_chave_exata.py`; web em `tests/test_web_vincular_v2.py` e
`tests/test_web_static_produto.py`.

## Troca de produto pelo importador de Excel do Fire (`troca_no_fire`)

O importador de Excel do Fire (`MECANICO=99`) não casa o código exato: pega o
primeiro produto da ORDEM FÍSICA cujo `CODPROD_ALTERN` começa com o código da
planilha, e não recusa código inexistente se algum começa com ele. O portal
prevê isso no check e avisa, sem bloquear (o XLSX está certo; o erro acontece
dentro do Fire).

- `queries.prever_import_fire_sql(n)`: `STARTING WITH ?` em OR, **sem ORDER BY
  de propósito** (a ordem física É o comportamento). `_prever_import_fire` roda
  em lotes de 150 e fica com a primeira linha que começa com cada código.
  Conferido na Fire viva contra `SELECT FIRST 1 ... STARTING WITH ?` um a um:
  620/620 iguais (MM + Nasmar, 24/09/2026).
- Consulta o código que VAI NO XLSX: o do parser, ou o `fire_codigo` do de-para
  (que o `depara_apply` escreve no export).
- Item ganha `troca_no_fire = {fire_product_id, codigo, descricao}` quando o
  palpite difere do match do portal, inclusive item sem match (`fire_product_id`
  None) e código duplicado no Fire (mesmo texto, outro SEQ). `summary` ganha
  `items_troca_no_fire` e `troca_no_fire_checked` (False se a previsão falhou —
  a falha nunca derruba o check).
- `troca_no_fire.conferido` = o item casou por `CODPROD_ALTERN` ou não casou —
  os únicos casos em que o comportamento do Fire foi medido. Casado por EAN (o
  XLSX leva a coluna EAN) ou por vínculo: `conferido: false`, e a UI pede
  conferência em vez de afirmar a troca.
- Código com mais de 30 caracteres fica fora da query: `CODPROD_ALTERN` é
  `VARCHAR(30)` nas duas Fire, e parâmetro maior estoura truncamento.
- **Código decidido (chave exata):** na mesma passada da previsão entra o
  `CODPROD_ALTERN` do candidato de cada item `ok`. `item["produto"]["troca_no_fire"]`
  = `{seq_previsto, codigo}` quando o palpite é outro SEQ, ou `seq_previsto` None
  quando não dá para prever (previsão falhou, nenhum produto começa com o código);
  senão None (também em todo item fora de `ok`). É esse campo que trava a planilha em
  `ligado` (acima); o `troca_no_fire` legado do item não muda.
- Fora de `is_blocking`. UI: `⚠ vira <código>` na coluna Fire + banner com dois
  conselhos (casado no portal = troque depois no Fire; sem match = confira ou
  vincule antes).
- **Limite:** reproduz o comportamento medido, não o código-fonte do Fire. Em
  `EXPORT_MODE=db` (insert com match exato) o aviso não se aplica.
- ⚠️ **Não verificado:** o `fire_codigo` do de-para é `str(SEQ)`
  (`catalogo_fire.codigo`). Se o importador do Fire só olha `CODPROD_ALTERN`, um
  item vinculado sai com o SEQ em texto e pode casar por prefixo com outro
  produto (310 SEQs na MM e 176 na Nasmar são prefixo de algum código). Se ele
  tentar SEQ antes, o aviso nesse caso é falso positivo. BACKLOG 2.15.
  **Com `produto_chave_modo=ligado` isso deixa de valer:** a planilha leva o `CODPROD_ALTERN`
  do produto decidido (nunca `str(SEQ)`) e a trava recusa `troca_no_fire`/`sem_codigo_fire`
  em vez de avisar. O aviso acima segue só em `desligado`/`observando`.

## De-para de cliente intercompany (Nasmar → cliente real)

`app/erp/depara_cliente.py` — `resolver_cliente_real(chave, *, revenda_slug)`.
Só lê o Firebird da revenda e devolve o cliente: não conhece `Order`, não
conhece Flow e não decide quando deve ser usado — isso é
`app/integrations/flowpcp/intercompany.py::resolucao_para(order, *, slug)`, a
camada de política, que casa o CNPJ do pedido contra
`environments.intercompany_cnpj` e, se bater, chama o resolver passando
`order.header.order_number` como chave — **exceto** quando o número foi gerado
pelo portal (`order_number_gerado`, `SN-<hash>`): aí passa `None` e o resolver
responde `sem_chave` sem abrir conexão. O token nunca foi digitado no Fire da
revenda; consultar com ele não casa e, com o `.4` fora do ar, trava o preview.

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

### De-para no insert direto (`EXPORT_MODE=db|both`)

`_send_one_to_fire` aplica `depara_apply` (antes do XLS do modo `both` e do
insert) e passa ao `FirebirdExporter.export` o mapa
`produtos_por_item = {posição em order.items: SEQ}` dos itens vinculados. Esses
itens são buscados **só** por `FIND_PRODUCT_BY_SEQ`: o `fire_codigo` do vínculo
é `str(SEQ)` e relido como `CODPROD_ALTERN` casaria outro produto. SEQ sumido do
Fire → `PRODUCT_NOT_FOUND`, sem cair para EAN/código. Audita
`depara_aplicado_no_fire`.

**Item sem produto no Fire recusa o pedido inteiro** (`PRODUCT_NOT_FOUND`): o
exporter levanta `FirebirdProductNotFoundError` e a conexão desfaz o cabeçalho.
Até 2026-10-17 o item entrava com `CODPRODUTO` NULL. Teste:
`tests/test_firebird_exporter_produto.py`.

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

Número com mais de 20 caracteres (`_TAMANHO_PEDIDO_CLIENTE`, o tamanho de
`CAB_VENDAS.PEDIDO_CLIENTE` nas duas bases) sai da consulta em
`_todos_numeros`, com log: o Firebird recusa o bloco inteiro quando um
parâmetro passa do tamanho da coluna, e um único número desses deixou a
reconciliação em `erro_conexao` de 27/08 a 15/10.

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

**Risco residual: a guarda não tem teto.** `_dentro_da_janela` só filtra pra
trás (`data_linha >= data_candidato − 90 dias`) — não existe limite pra
frente. Um pedido antigo que continua `parsed` (nunca casou) pode, em tese,
casar com um cadastro novo do mesmo número e cliente feito mais de 90 dias
depois. É o último caminho de falso positivo que sobrou nesta feature, e não
foi fechado de propósito: um teto simétrico (também descartar linhas do Fire
"tarde demais" em relação ao candidato) quebraria o caso real que motivou a
feature — os 308 pedidos que ficaram `parsed` porque a operadora só cadastra
no Fire meses depois do import. Se um dia isso doer, um teto largo (na faixa
de 270 a 400 dias) fecharia o reuso anual de número (o próprio motivo da
guarda de 90 dias) sem barrar o cadastro tardio que a feature existe para
cobrir.

### Variantes do número

`app/erp/numero_pedido.py::variantes(numero)` gera as formas aceitas como
"mesmo número", da mais específica pra menos: exata, sem sufixo de loja, sem
zeros à esquerda. Nunca substituem a segunda perna da chave — só ampliam o
que conta como "mesmo número" antes de checar a âncora de cliente.

**Sufixo de loja** = `\s*-\s*\d{1,4}$` — hífen, com ou sem espaços em volta,
seguido de 1 a 4 dígitos no fim. O corte só vale se o que sobra tiver **3+
caracteres E ao menos um dígito** (`_resto_parece_numero_de_pedido`).

**Dois casos reais medidos:**

- Sam's Club guarda `06654993-0000` no portal e `06654993` no Fire — sem
  cortar, 100% de falso negativo em Sam's.
- Authentic Feet e Xambre mandam `AF049-6`, `AW033-6`, `AF090 - 3` e o Fire
  guarda `AF049`, `AW033`, `AF090` (medido na Fire viva 2026-08-24). A regra
  antiga só cortava 4 dígitos: **43 dos 137 pendentes** ficavam de fora, e 19
  de 19 amostrados existiam no Fire sob o MESMO CNPJ.

A trava do resto é o que preserva o contraexemplo `AF-198` da spec: cortado
viraria `AF`, que casaria com qualquer pedido do mesmo cliente numerado `AF`.
A regra antiga escapava dele por acidente (198 tem 3 dígitos, não 4) — mas
cortava `AF-1985`, com exatamente o mesmo problema.

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

### Escolha do representante

`_montar_achado` (`fire_reconcile.py`) elege, entre as linhas casadas, a de
`DATA_PEDIDO` **mais próxima** da data do pedido no portal. Empate — ou data
indisponível dos dois lados — desempata pelo menor `V.CODIGO`. `CANCELADO`
sai do desempate antes de tudo, se houver alternativa.

Era `min()` por `CODIGO` até 2026-08-25. Como `CODIGO` cresce com o tempo,
isso elegia **sempre a linha mais antiga** — e os clientes reusam número: a
Fire da MM tem **143 pares (número, cliente) repetidos**. Em produção, `AF112`
importado em 03/08 apontava para a linha de 22/05; numa amostra de 11
marcados, 6 estavam na linha errada, e **58 dos 169 dividiam `fire_codigo`**
com outro pedido.

O match em si estava certo (o pedido existe no Fire). Errada era a linha — o
que faz `fire_codigo` mentir na tela e o `poll_fire` seguir o status do
pedido errado.

**Pedido já marcado também é corrigido.** `repo.list_found_in_fire_for_recheck`
+ `repo.corrigir_representante` fazem a segunda passada do runner: reapontam
`fire_codigo`/`fire_status_last_seen` sem transição de estado, sem evento de
ciclo de vida e **sem tocar `reconciled_at`** (é a âncora fixa da janela do
poll). Deixa rastro em `audit_log` como `fire_representante_corrigido`. Sem
essa passada, os pedidos marcados pela regra velha ficariam errados pra
sempre, porque `list_parsed_for_reconcile` só enxerga `parsed`.

### Limitação 1↔N (conhecida, documentada)

Um import Riachuelo vira **N linhas** em `CAB_VENDAS` — uma por loja, cada
uma com cliente e CNPJ próprios. `imports.fire_codigo` é uma coluna só, e
guarda **o menor `V.CODIGO` entre as linhas casadas que não estão
canceladas** (`_escolher_representante`, `fire_reconcile.py:375`). O
desempate ignora `CANCELADO` de propósito: quando uma loja é recadastrada,
a linha viva tem `CODIGO` maior que a cancelada, e `min()` puro faria o
portal exibir "CANCELADO" para um pedido que está vivo no Fire.

É representação **parcial** e inofensiva enquanto o `poll_fire` não roda na
MM (`setup-service.ps1` registra só `ui.py`). No dia em que rodar, o poll
seguirá o status de uma loja só. A saída, se importar, é uma tabela de
ligação `import_id ↔ fire_codigo` — não alargar esta coluna.

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

**`erro_conexao=True` cobre falha de conexão E falha de leitura/dado.**
Fix de 2026-08-24 (achado em teste real de navegador): a distinção fina
entre "falhou ao conectar" e "conectou mas falhou ao ler" só importa para o
cool-down (que continua armando exclusivamente em volta do
`connect_with_config`, como acima) — para quem lê o resultado (operadora,
via `Resultado.status`) as duas são igualmente "não consegui consultar o
Fire". Antes do fix, falha de leitura devolvia `erro_conexao=False`,
fazendo a UI reportar "0 casaram" (dado como se tivesse consultado e não
achado nada) quando na verdade a consulta nem completou.

### Testes
`tests/test_fire_reconcile.py` — os 3 caminhos, "todas as lojas", variante
sem sufixo (caso Sam's), guarda temporal, CNPJ divergente, Fire fora
devolve vazio sem levantar, cool-down só em erro de conexão, lote > 200.

`tests/test_numero_pedido.py` — as variantes de número em isolamento,
incluindo zeros à esquerda (`test_zeros_a_esquerda_viram_variante`).

## Trava de preço por rede

Valida o preço do pedido contra a **tabela de preço da rede** (cópia local da tabela
do Flow), no lugar de só comparar com o Fire. O Portal **nunca edita preço** e não
tem tela de gestão de preço: a decisão (aprovar/recusar) vem do Flow. Spec:
`docs/superpowers/specs/2026-10-17-tabela-preco-por-cliente-design.md`. Branch
`feat/tabela-preco-por-rede`. Preço sempre em **centavos inteiros**, tolerância zero
(`_to_cents`).

> **Conversa com o Flow (Task 7) implementada** em `app/integrations/flowpcp/precos.py`
> (ver `modules/flowpcp.md` §6). O `PATCH /api/admin/environments/{id}/preco-trava` ainda
> recusa `ligado` (409) enquanto `precos_repo.ultima_sync_ok` do db da empresa for `None`
> (a cópia nunca completou). `observando` não bloqueia nada, só audita, mas adia o XLSX da
> importação. A conversa está ligada por padrão (`PORTAL_FLOWPCP_PRECOS=0` desliga) e só age
> em ambiente com trava ≠ `desligado`.

### Runbook: ligar a trava de preço
1. Gravar o CNPJ da empresa (`environments.cnpj`); sem ele nada sincroniza (aviso uma vez no log).
2. Nasmar: `preco_fonte_slug="mm"` (avalia pela chave do produto da MM e fala com o Flow da
   MM, com o CNPJ da Nasmar como `empresaFaturadora`) e o Flow ligado na MM.
3. Passar por `observando` antes: a tabela sincroniza e o audit `preco_observado` mostra o que
   `ligado` faria. Conferir no log `flowpcp.precos estado env=…` (`gerado_em`, `ultimo_erro`).
4. Recarregar o preview NÃO regrava o `check_preco`: só `/api/commit` e `vincular-produto`
   regravam. Check salvo antes do deploy (sem `fire_unidade`, ou de outra fonte) mostra "sem
   avaliação"; pedido já importado fica assim até vincular o produto ou reimportar.
   A ida ao Fire da MM no `/api/commit` da Nasmar desiste da conexão em ~5 s
   (`LIMITE_CONEXAO_FONTE_S`; teto real abaixo). Cadastre a MM com **IP** no host: a
   resolução de nome não respeita o limite.
   Escolha pontual na Nasmar: a MM casa a linha pelo `CODPROD_ALTERN` do produto escolhido.
   Produto da Nasmar sem esse código na MM (ou com código repetido) bloqueia o pedido com
   `escolha_pontual_sem_fonte` até cadastrar/ajustar o código na MM; enquanto isso, deixar
   a Nasmar em `observando`.
5. Só então `ligado` (409 até a 1ª cópia completa). Desligar volta ao comportamento de hoje.

### Interruptor por ambiente (`environments.preco_trava_modo`)
- `desligado` (nasce assim) — **o comportamento de hoje** (`check_order` + `is_blocking` +
  ack), exceto o ack de "sem preço" na planilha com a integração direta desligada (acima).
  É também o modo de ambiente ausente.
- `observando` — regra de hoje continua decidindo; a avaliação nova roda e é gravada em
  audit `preco_observado` (`regra_de_hoje_bloqueia`, itens). Nunca bloqueia, nada vai ao Flow.
- `ligado` — a regra de hoje **não decide** (aviso de preço do Fire e ack somem); decide
  a trava. Qualquer exceção ao avaliar **bloqueia** (fail-closed: o portão grava em ERP de produção).

### Arquivos
- `app/erp/preco_avaliacao.py` — `avaliar(...)`: **puro** (sem I/O; recebe `resolver_rede`,
  `preco_vigente(rede_id, erp_produto_id, data) -> PrecoLinha | None` (empresa fixada pelo
  chamador; pode levantar `PrecoInvalido`), `cnpjs_grupo`, `ultima_sync`, `agora` (só frescor)
  e `data_ref` (vigência; `data_ref_do_pedido`: `issue_date`, senão a data civil em São Paulo
  de `imported_at`, senão de `agora`). Devolve `Avaliacao` de `ItemAvaliado` (`situacao`:
  `ok | fila | nao_avaliado | fora_intercompany`; `motivo`; `preco_tabela_id`; `unidade_erp`
  = unidade do Fire normalizada).
- `app/erp/preco_trava.py` — o portão e os helpers compartilhados.
- `app/persistence/precos_repo.py` — cópia local (`aplicar_pagina`, `reiniciar`, `estado_sync`, `resolver_rede`, `preco_vigente`, `ultima_sync_ok`).
- `app/erp/preco_regra.py` — regra pura do §5 do contrato precos v1 (`normalizar_cnpj`, `cnpj_valido`, `resolver_preco`, `data_referencia`, `centavos`); vetores do pcp-app em `tests/fixtures/flow_precos_v1/`.
- `app/persistence/preco_revisao_repo.py` — revisão do pedido e decisões do Flow
  (`garantir_revisao`, `vigente`, `decisoes_da_vigente`, `gravar_decisao`, `retirar` e as
  filas que a thread do Flow consome: `pendentes_envio`, `confirmacoes_pendentes`,
  `retiradas_pendentes`).
- `app/integrations/flowpcp/precos_schema.py` — porta pydantic do zod do contrato:
  `validar_envio` (`aprovacaoEnvioV1` com os `superRefine`), `DecisaoPrecoV1`, `DecisoesPaginaV1`.

### Ordem das regras do avaliador
1. `cliente_sem_cnpj` — sem CNPJ de rede → todos `nao_avaliado`. `cnpj_da_rede`: o do cliente
   (override vence) normalizado (`preco_regra.normalizar_cnpj`, 14 caracteres); sem ele, se
   **todas** as lojas de entrega têm CNPJ válido com a mesma raiz, o menor deles (ordem de texto).
2. `intercompany` — raiz do cliente está em `cnpjs_grupo` (raízes de `environments.cnpj` e
   `intercompany_cnpj` de todos os ambientes) → `fora_intercompany`. Decidido **antes** da
   cópia velha: não depende da cópia.
3. `copia_desatualizada` — sem sync OK ou há mais de **24 h** → todo item `nao_avaliado` (só
   chega aqui o pedido com CNPJ de rede e fora do grupo: `cliente_sem_cnpj` e intercompany decidem antes).
4. Por item, não avaliados (bloqueiam): sem SEQ no Fire → `produto_nao_identificado`; sem
   rede → `cliente_sem_rede`; `PrecoInvalido` → `preco_invalido`; `fire_unidade` vazia, ou
   preço achado com `unidadeErp` nula ou diferente (`strip().upper()`) → `unidade_divergente`.
   Depois, na fila (precedência do contrato §4.1): `rede_travada` > `pedido_sem_preco` >
   `sem_preco_tabela` > `preco_acima` | `preco_abaixo`; igual → `ok` (`ok`). Preço do pedido
   em centavos meia para cima, 0 → sem preço (`preco_regra.centavos`).
Rede (§5 do contrato): CNPJ completo (14) em `cnpjs` de rede ativa vence a raiz (8 primeiros caracteres) em `raizes`; CNPJ normalizado antes (`preco_regra.normalizar_cnpj`). Preço da empresa faturadora = `environments.cnpj` do ambiente.
Chave do produto = `str(check["items"][i]["fire_product_id"])` (SEQ; o `check` guarda `int`).
**Item com vínculo de-para:** o SEQ avaliado é o do vínculo, mesmo que o item case direto
por EAN/código com outro produto. Nesse caso a `fire_unidade` do check é a do outro produto
e sai (`_com_vinculos`): o item fica `unidade_divergente`. O chamador consulta o de-para **uma vez**
(`depara_apply.resolver`) antes do portão e passa `produtos_por_item` a `decidir_envio`
(e a `preview_preco`), ao `FirebirdExporter` e a `depara_apply.aplicar` (códigos do XLSX).
`desligado` ignora o mapa (regra de hoje). Com `produto_chave_modo` `ligado`, o mapa avaliado
é o do produto decidido (`produto_trava.produtos_decididos`, ver a trava de produto).

**Override anterior à migração:** linha com `cliente_override_codigo` e
`cliente_override_cnpj` NULL (override feito antes da coluna existir) sai
`cliente_sem_cnpj` e bloqueia em `ligado`. O operador refaz a escolha do cliente no
preview (o override novo grava o CNPJ).

### Ponto único de decisão: `decidir_envio`
`preco_trava.decidir_envio(import_id, order, entry, env=, agora=)` é o **único** portão
antes do Fire e do XLSX; nenhum caminho grava no Fire sem passar por ele (a perna espelho
do lote Nasmar→MM deve chamá-lo também, conferir no merge das branches). Devolve
`DecisaoEnvio` (`bloqueado`, `reason`, `detalhe`, `block_detail`, `metric_labels`,
`precos_aplicar`, `aplicados`). O chamador audita o bloqueio, conta a métrica e, ao liberar,
chama `aplicar_precos(order, precos_aplicar)` antes de exportar e `registrar_aplicados`
(audit `preco_aplicado`) **só depois do sucesso**.
- Puros e compartilhados com o preview: `avaliar_entry`, `preview_preco`,
  `classificar_itens` (estado final por item) e `bloqueio_do_pedido`. Paridade preview↔envio testada.
- `ligado` (contrato precos v1 §1 passo 6): pedido com item `nao_avaliado` ou bloqueio de
  pedido (`empresa_sem_cnpj`, `cliente_sem_cnpj`, `copia_desatualizada`, sem check,
  `bloqueio_do_pedido`) bloqueia **antes** de calcular hash ou tocar em revisão/decisão.
  Senão calcula corpo e `conteudo_hash` **sempre** (`montar_corpo`) e compara com a vigente
  (`_acao_revisao`): com fila, hash novo (ou vigente `recusada_flow`) → corpo validado
  (`precos_schema.validar_envio`; inválido = bloqueio local, nada gravado) e
  `garantir_revisao`; sem fila, vigente obsoleta (ou `pendente_envio` nunca tentada) →
  `retirar` e segue pela regra local; pedido que virou intercompany retira a vigente; sem
  fila e sem vigente → segue sem falar com o Flow, preço intocado. Com revisão vigente, todo
  item precisa da decisão da sua `itemChave` (`f"{import_id}:{i}"`), inclusive os `ok`.
- `registrar_aplicados` roda depois da transição `SEND_TO_FIRE_SUCCEEDED` / do push do
  XLSX, em try/except: falha no audit nunca derruba um envio que deu certo.
- **Chave do produto de outro ambiente** (`preco_fonte_slug` ≠ próprio; Nasmar → MM,
  contrato §9.2): `_check_para_preco` roda `pc.check_order(order, env=<fonte>)` dentro de
  `env_context.active_env(<fonte>)` (o degrau de-para lê o SQLite da MM) e o
  `fire_product_id`/`fire_unidade` dele é o avaliado e o que vai no corpo. Os vínculos
  de-para do próprio ambiente (`produtos_por_item`) **não** entram nesse check. Item sem
  casamento na MM → `produto_nao_identificado`; MM fora ou fonte inexistente → bloqueia,
  sem fallback para check salvo. A conexão com o Fire da fonte roda dentro de
  `connection.limite_de_conexao(LIMITE_CONEXAO_FONTE_S = 5)`: sonda TCP de 5 s antes do
  `fdb.connect` (o limite garantido; provado contra 10.255.255.1: 5,0 s) e, no DPB,
  `isc_dpb_connect_timeout` com o que **sobrou** da sonda (arredondado para cima, mínimo 1 s;
  hook do fdb registrado uma vez, com lock, que só age no connect TCP de dentro do bloco).
  Teto real: ~5 s (+ até 1 s de arredondamento) se o fbclient respeitar o DPB, o que não foi
  provado sem fbclient; servidor que aceita TCP e trava o attach sem respeitar o DPB fica no
  timeout dele. `getaddrinfo` (host por nome) não respeita o limite: use IP. A sonda abre e
  fecha um TCP sem protocolo: o firebird.log do servidor Windows pode registrar erro 10054
  (ruído). Estourou = `FirebirdConnectionError` → check indisponível, a mesma semântica de
  "MM fora". Os outros caminhos do portal não ganham limite. O preview usa `entry["check_preco"]`
  (`imports.check_preco_json`, gravado no `/api/commit` e regravado no `vincular-produto`
  por `check_preco_para_gravar`, marcado com `preco_fonte_slug` da fonte que o gerou);
  ausente ou de outra fonte → sem avaliação. `desligado` não faz consulta extra.
  **Pedido intercompany não depende da fonte**: portão, preview e `check_preco_para_gravar`
  usam o check próprio (sem ida à MM); bloqueio de pedido e regra de hoje também.
- **Intercompany** (`fora_intercompany`) roda **a regra de hoje** (`_regra_de_hoje`) e audita
  `preco_intercompany_fora_da_trava`; pode bloquear com `price_check_failed`.

### Revisão e decisões (`preco_revisao`, `preco_decisao`)
- `conteudoHash` = `"sha256:" + hex` de JSON canônico de `{cliente_cnpj, empresa_cnpj,
  itens:[{linha, erpProdutoId, unidadeErp, quantidade (texto a 2 casas), precoPedidoCentavos}]}`
  (`conteudo_hash`). Descrição não entra. Mudou preço, quantidade, produto, unidade ou
  cliente → revisão nova `max(maior, piso) + 1`; A→B→A = 3 revisões.
- Decisão só conta se é da revisão vigente **e** o hash da vigente é o atual. Decisão de
  outra revisão grava `descartada`. Duas para a mesma chave: vence o maior `decidido_em`.
- `classificar_itens(order, av, revisao, decisoes)`: sem revisão, `ok`→ok e fila→aguardando;
  com revisão, `recusado`→recusado, outra decisão→aprovado com `precoAprovadoCentavos/100`
  (vai a **todo** item, Fire e XLSX, via `precos_aplicar`; `aplicados[].flow_id` =
  `decisaoId`), sem decisão→aguardando; revisão `recusada_flow`→todos recusados. No portão,
  a vigente `recusada_flow` dá lugar à revisão seguinte com o mesmo corpo (novo envio).
- `retirar` (cancel, reimport dos mesmos bytes por `imports.file_sha256` — ou, para a
  entrada de um reimport anterior, que não grava sha, `file_sha256 IS NULL` + o mesmo nome —,
  fila zerada, intercompany): ativas viram `retirada`; a retirada vai ao Flow só para a última revisão que
  ele pode conhecer (`enviada_em` ou `tentada_em`; `marcar_tentada` roda logo antes do POST e
  devolve False se a revisão não está mais `pendente_envio`). `marcar_enviada` grava
  `enviada_em` mesmo em revisão já substituída, mas só muda a situação de `pendente_envio`;
  se o pedido já foi retirado, a revisão que chegou vira a retirada pendente.
  `marcar_recusa_flow` (422) limpa `tentada_em`: o Flow não guardou a revisão. Na 7.4, 409
  `revisao_obsoleta` na retirada → reenviar com `details.revisaoVigente`; 200 com
  `aprovacaoId` nulo → retirada feita.
- `gravar_decisao` descarta (com log) decisão cujo `erpProdutoId`/`itemChave` ou
  `empresaFaturadoraCnpj` não bate com o corpo da revisão vigente.
- Corpo: `externalId` = `import_id`; `empresaFaturadoraCnpj` = `environments.cnpj`
  normalizado; `cliente` = {nome do override/cabeçalho/razão do Fire, CNPJ que resolveu a
  rede}; `codigo` cai no `erpProdutoId`, `descricao` na do Fire, no código e no
  `erpProdutoId` (textos truncados em unidades UTF-16); número do pedido ausente/acima de 64
  ou quantidade com mais de 2 casas → bloqueio local com texto legível; `emitidoEm` = `issue_date`
  00:00 em São Paulo em UTC (`.000Z`); `recebidoEm` = `imported_at` (hora de São Paulo).

### Razões de 409 (envio ao Fire / export)
`preco_nao_avaliado` (item `nao_avaliado`, ou falha ao avaliar), `preco_recusado`,
`preco_aguardando` (item sem decisão da revisão vigente). Em `desligado`/`observando` e em
intercompany continuam as razões de hoje (`price_check_failed`, etc.).

### Preview
`GET` do preview ganha `payload["preco"]` (`modo`, `itens[]` com `situacao/motivo/preco_pedido/
preco_tabela/decisao/estado`, `resumo`, `bloqueio_pedido`, `copia` = `{sincronizada_em,
idade_min}` (de `precos_repo.ultima_sync_ok`; ambos null se nunca sincronizou), `revisao` =
`{numero, situacao, erro_flow}` ou null); ausente em `desligado` e em qualquer falha. Só
leitura: nunca cria nem retira revisão; em `ligado` lê a revisão como o portão (hash atual,
mesma `_acao_revisao`). Em `ligado` com o pedido bloqueado pela trava (mesmo agregado do
banner) o botão principal do rodapé fica `disabled` e a nota diz o motivo.

### Testes
`tests/test_preco_avaliacao.py`, `tests/test_preco_trava.py`, `tests/test_precos_repo.py`,
`tests/test_preco_revisao_repo.py`, `tests/test_flowpcp_precos_schema.py`, `tests/test_preco_trava_modo.py`.
