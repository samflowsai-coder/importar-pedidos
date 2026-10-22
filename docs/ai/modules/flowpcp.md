# Módulo: flowpcp (integração com o FlowPCP)

## Responsabilidade
Ponte entre o Portal e o **FlowPCP** (chão de fábrica). Quatro fluxos independentes,
todos **por ambiente** e todos com gate próprio:

1. **Pedido → Flow** — pedido aprovado sobe pro Flow (`/recebimento`).
2. **Decisões Flow → Fire** — o Flow decide o prazo pactuado; o Portal escreve a data de
   entrega de volta no Fire (Modelo B / OVERLAY).
3. **Catálogo Fire → Flow** — snapshot de produto, com cópia local sempre e envio opt-in.
4. **Clientes Fire → Flow** — mesma mecânica do catálogo, para master data de cliente.

**Regra que atravessa tudo: best-effort.** O pedido já entrou no Fire/XLS quando o push
acontece — uma falha aqui vira outbox/retry e **nunca** pode derrubar o fluxo principal.

## Arquivos críticos

| Arquivo | Papel |
|---|---|
| `config.py` | `FlowPCPConfig` + `flowpcp_config_for_slug()` / `enabled_flowpcp_envs()` |
| `hook.py` | `push_new_order(order, *, import_id, slug)` — o gatilho |
| `exporter.py` | `FlowPCPExporter.enqueue()` → outbox (`target=flowpcp`) |
| `envio_portal.py` | thread do processo web: entrega a fila (com triagem da fila acumulada) e, com interruptor, as decisões |
| `client.py` | HTTP para o Flow (service token, timeout por ambiente) |
| `mapper.py` / `schema.py` | `Order` → payload de `/recebimento` |
| `intercompany.py` | **política** do de-para de cliente (quando aplica) |
| `poll_decisoes.py` | consome decisões do Flow e reconcilia no Fire |
| `catalogo_*.py` / `clientes_*.py` | sync de produto e de cliente (extract → local → push) |
| `catalogo_fiscal.py` | campos fiscais do produto (NCM, SPED, grupo, `codigoNfe`): normaliza e devolve pendências |
| `capacidades.py` | o que o Flow declara aceitar (`fiscal.v1`, `materiais.dryrun.v1`, `materiais.v1`, `cadastros.v1`, `marcas.v1`, `produtos.nota.v1`) |
| `materiais_sync.py` / `materiais_schema.py` | carga de material (grupo ≠ 8) Fire → cópia local → Flow |
| `cadastro_volta.py` / `cadastro_schema.py` | volta Flow → Fire: modos, portões, `simular` e a ordem do ciclo (§5) |
| `cadastro_gravacao.py` | volta nos modos de gravação: triagem, reserva, gravação, reenvio do resultado (§5) |
| `cadastro_recuperacao.py` | resolve o que um processo caído deixou `em_andamento` (§5) |
| `cadastro_resultado.py` | corpo do `POST .../gravado`, leitura da resposta do Flow, `mensagem_para_flow` |
| `catalogo_marcas.py` | `marcasErp` do catálogo: filtro das regras do Flow e interruptores de marcas e nota (§4) |
| `precos.py` / `precos_schema.py` | trava de preço (contrato precos v1): cópia da tabela, envio de revisões/retiradas, decisões |

## Configuração — por ambiente, não global

Tudo vive em colunas `flowpcp_*` da tabela `environments` (`app_shared.db`); o token é
cifrado (`flowpcp_service_token_enc`, via `secret_store`).

| Coluna | Default | O que controla |
|---|---|---|
| `flowpcp_enabled` | `0` | liga a integração no ambiente |
| `flowpcp_base_url`, `flowpcp_tenant_id`, `flowpcp_service_token_enc` | — | destino e credencial |
| `flowpcp_dry_run` | `0` | simula sem gravar do lado do Flow |
| `flowpcp_poll_interval_s` | `30` | cadência do poll de decisões |
| `flowpcp_request_timeout_s` | `30.0` | timeout HTTP |
| **`flowpcp_catalogo_push`** | `0` | **OFF: o sync só atualiza a cópia local** |
| `flowpcp_catalogo_apenas_meias` | `0` | recorta o catálogo |
| **`flowpcp_clientes_push`** | `0` | idem para clientes |

Variáveis de ambiente do processo (todas **ausentes = desligado**; `slug` em minúsculas):

| Variável | Valor | O que controla |
|---|---|---|
| `PORTAL_FLOWPCP_CATALOGO_FISCAL` | `mm` (lista por vírgula) | manda os campos fiscais no catálogo; sem `fiscal.v1` na resposta do Flow o relatório avisa e a variável deve ser retirada |
| `PORTAL_FLOWPCP_MATERIAIS` | `mm` (lista por vírgula) | envio da carga de material ao Flow; desligada, só atualiza a cópia local `materiais_fire` e `apply` dá 422 |
| `PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_<SLUG>` | `1,4,5,...` | grupos que exigem aprovação (slug em maiúsculas, não alfanumérico vira `_`); padrão `1,4,5,6,11,13,19`; inválido loga ERROR e usa o padrão |
| `PORTAL_FLOWPCP_CADASTRO_VOLTA` | `mm:simular,nas:desligado` | modo da volta por ambiente: `desligado` (padrão), `simular`, `criar`, `criar_e_atualizar`; entrada inválida = desligado, com ERROR uma vez |
| `PORTAL_FLOWPCP_PRECOS` | `0` | **exceção: ausente = ligado.** `0` desliga a conversa da trava de preço com o Flow; ligada, só age em ambiente com `preco_trava_modo` ≠ `desligado` |
| `PORTAL_FLOWPCP_CADASTRO_P0` | `mm` (lista por vírgula) | portão da gravação: sem o slug aqui, `criar`/`criar_e_atualizar` são recusados (o resultado já guardado ainda é reenviado). Só liga depois da P0 (diff da tela do Fire numa cópia do `.fdb`) |
| `PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR` | `mm` (lista por vírgula) | portão da ação `inativar` (só em `criar_e_atualizar`); sem ele o item `inativar` é pulado com WARNING |
| `PORTAL_FLOWPCP_CATALOGO_MARCAS` | `mm` (lista por vírgula) | catálogo manda `marcasErp` (PRODUTOS_MARCA filtrada); exige `marcas.v1` na resposta do Flow |
| `PORTAL_FLOWPCP_CATALOGO_NOTA` | `mm` (lista por vírgula) | catálogo manda `temNotaFiscal` por produto (NOTAPROD); exige `produtos.nota.v1` na resposta |

Rotas admin: `PUT /api/admin/environments/{env_id}/flowpcp`,
`PUT /api/admin/environments/{env_id}/intercompany`,
`POST /api/admin/environments/{env_id}/flowpcp/sync-catalogo?apply=&excluir=&confirmar_remocao=&confirmar_substituicao=`,
`POST /api/admin/environments/{env_id}/flowpcp/sync-clientes?apply=`,
`POST /api/admin/environments/{env_id}/flowpcp/sync-materiais?apply=` — todas exigem
`flowpcp_enabled`. Mais `GET /api/admin/environments/{env_id}/flowpcp/cadastro-volta`
(só leitura do livro; não exige `flowpcp_enabled`). Ver `modules/environments.md` para o CRUD do ambiente.

## 1. Pedido → Flow

`push_new_order` é chamado em `app/web/server.py` **depois** do sucesso no Fire/XLS.
Ele resolve o cliente (ver intercompany, abaixo), monta o payload e chama
`FlowPCPExporter.enqueue()`, que **enfileira no outbox** com
`idempotency_key = "send-{import_id}"`. Quem entrega e faz retry é uma thread do
processo web (`envio_portal.py`, `flowpcp-envio`) — no Windows do cliente só o `ui.py`
roda, e o worker (`drain_outbox`) nunca subiu lá: o envio ficou parado de 24/07 a 10/2026.
O worker continua fazendo o mesmo em deploy docker, onde a thread fica desligada
(`PORTAL_FLOWPCP_ENVIO=0` no `docker-compose.yml`). **Nunca há HTTP no request path** — a
UI não espera o Flow.

**Janelas de envio:** a fila só sai em janelas de 1 h às 8h, 11h, 14h e 17h (Brasília),
segunda a sábado (`app/utils/janelas_envio.py`, `PORTAL_FLOW_JANELAS`); fora delas o pedido
só enfileira. Resumo por janela no log (`janela … fechada: total=… por_ambiente=…`) e em
`/metrics` (`portal_flowpcp_janela_pedidos{janela,resultado}`, últimas 8; resultado =
enviado | recusado | descartado | retido). A contagem é lida do banco (`outbox.sent_at` e
audits `flowpcp_recusado/descartado/retido`, hora local do servidor), então sobrevive a
reinício e inclui o worker. Vale para thread e worker.

**Regra de envio** (`envio_portal.drenar_ambiente`, usada pela thread E pelo worker):
- pedido com `imports.fire_codigo`, qualquer idade: só vai se segue aberto no ERP
  (`CAB_VENDAS.STATUS`, READ ONLY, uma leitura por ciclo). Faturado, cancelado ou apagado
  → `dead` com `last_error = "descartado: …"` e audit `flowpcp_descartado`;
- sem código e com até 7 dias (`LIMITE_ATRASO`): vai (no modo xlsx o pedido ainda nem
  entrou no ERP);
- sem código e mais velha: `retido` + audit `flowpcp_retido`; volta sozinha à fila quando
  a reconciliação preenche o `fire_codigo` (`outbox_repo.release_retained`);
- ERP fora, ou ERP que não acha NENHUM de vários códigos (banco trocado): quem tem código
  espera, com pausa de 5 min e nada é descartado; recentes vão antes das acumuladas;
- falha de envio pausa o ambiente por 5 min (as outras linhas não queimam tentativa);
- linha flowpcp **nunca morre por falha passageira** (rede, 5xx, 401/403/408/429): passado o
  escalonamento (30 s…6 h), tenta a cada 6 h. Recusa definitiva (outro 4xx, payload
  inválido) → `dead` com `last_error = "recusado: …"`. Linhas `retido` contam como pendentes
  em `/metrics`; descartadas e recusadas contam como `dead` (o `last_error` diz qual). `next_attempt_at` é hora local, no formato de `outbox_repo._now()`
  (antes era UTC com offset: no Brasil, 30 s viravam 3 h 30 s).

Duplicata no outbox (`OutboxDuplicateError`) é no-op com log, não erro.

**Vínculo forte de produto (`erpProdutoId`):** cada item do `/recebimento` leva, além do
`produtoCodigo`/`produtoEan` de sempre, o SEQ do Fire que o Portal já decidiu
(`itens[].erpProdutoId`, string). É ADICIONAL: o código do cliente continua indo como antes.
Sem isso o Flow não acha o produto quando recebe o código do varejista (182 itens
`__UNKNOWN__` em 22/10). Fonte, do mais fraco ao mais forte (`produto_erp.seqs_por_item`): produto
que o check casou (`fire_product_id`, só com `match`) → decisão do check em `produto_chave_modo`
`ligado` → mapa explícito do envio (de-para / escolha pontual). Montado em
`server._pedido_para_o_flow`. O Flow ignora o campo até passar a ler (zod não é `strict`).

## 2. De-para de cliente intercompany

Divisão de responsabilidade que não deve ser embaralhada:

- **`intercompany.py::resolucao_para(order, *, slug)`** — *política*. Casa o CNPJ do
  pedido contra `environments.intercompany_cnpj`; se bater, chama o resolver.
- **`app/erp/depara_cliente.py`** — *leitura*. Só lê o Firebird da revenda e devolve o
  cliente. Contrato, motivos e cool-down em `modules/erp.md`.

O `hook.py` ainda embrulha a chamada num guard próprio (`_resolucao_segura`) — cinto e
suspensório, porque `push_new_order` é o contrato de que `server.py` depende.

A resolução é auditada em `depara_cliente` com chave, motivo, CNPJ/nome reais e o
`revenda_slug`. A lista `pedidos_no_4` é capada em **50** itens no audit
(`pedidos_no_4_total` guarda a contagem real): uma chave genérica digitada à mão na
revenda (`PULMÃO`, `AF`, `AW`, `GFNASMAR`) casa centenas de linhas. **O cap é só no
audit** — pôr `LIMIT` na query esconderia um segundo CNPJ e quebraria a detecção de
ambiguidade.

## 3. Decisões Flow → Fire (`poll_decisoes`)

`poll_decisoes_once` lê o cursor persistido (`flowpcp_cursor_state`), busca decisões,
aplica o `UPDATE` da data de entrega no Fire e confirma de volta ao Flow. Job
`poll_flowpcp`, a cada 30s, **por ambiente habilitado** — um ambiente ruim não
derruba os outros. Roda **só com `PORTAL_FLOWPCP_DECISOES=1`** (worker e processo web)
(desligado por padrão: é escrita no ERP de produção, parada desde julho). O "Dry-run" da
tela **não** é interruptor: ele confirma a decisão no Flow sem gravar no ERP.

- Não achou o pedido pela chave do cliente real? Tenta de novo com
  `PEDIDO_CLIENTE + CNPJ da revenda` — no Fire o pedido continua no nome de quem faturou.
- Ainda não achou: `register_attempt`; em **5** tentativas (`_MAX_NAO_ENCONTRADO`) loga
  `critical`.
- **Bug aberto:** exceção no retry da revenda retorna sem contar tentativa e **segura o
  cursor**. Ver `docs/BACKLOG.md` §1.2.

## 4. Catálogo e clientes (Fire → local → Flow)

`run_catalogo_sync` / `run_clientes_sync` extraem do Firebird, **sempre** gravam o
snapshot local (`catalogo_fire` / `clientes_fire`, no db do ambiente) e só empurram ao
Flow se o gate correspondente estiver ligado. Gate OFF é caminho normal, não erro — o
log diz `envio ao Flow DESLIGADO`.

**Catálogo diário** (`catalogo_diario.py`, desde 15/10/2026): a thread
`flowpcp-envio` (`envio_portal.ciclo`) manda o catálogo inteiro
(`fullSync=true`) uma vez por dia, na hora `PORTAL_FLOWPCP_CATALOGO_HORA`
(padrão 5, Brasília), em cada ambiente com `flowpcp_catalogo_push` ligado.
`PORTAL_FLOWPCP_CATALOGO_DIARIO`: `simular` (padrão, `dryRun=true`), `gravar`
(`dryRun=false`, só depois do ok ao primeiro promote) ou `desligado`. "Já foi hoje" = `MAX(catalogo_fire.extraido_em)`
desde o início da hora. Falha antes da cópia local pausa 10 min. O promote do
Flow não desativa produto só do Flow (sai como `flowOnly` no relatório).

**Tipo e recorte** (`catalog_extract._tipo`, desde 15/10/2026): para o Flow,
`simples` = par e vira OP; kit/pack sem composição fica fora do pool. Por isso
`pack` = unidade PACK ou PACK no nome; `kit` = pai em `PRODUTOS_KIT`, unidade KIT
ou KIT no nome; o resto é `simples`. O promote atualiza o tipo dos já casados.
O recorte "só meias" inclui os componentes (CTE recursiva sobre `PRODUTOS_KIT`),
porque o par não é vendido e sem isso ficava só no Flow (trava de 5 níveis
contra ciclo; componente cueca fora). A composição em si (`componentes`) só vai
no payload com `PORTAL_FLOWPCP_CATALOGO_COMPOSICAO` ligada para o slug (ver abaixo). Com ela ligada, `tools/configurar_flowpcp.py --promover` grava composição e vínculo com o Apontaê: só com o ok do Samuel. **Portão antes de gravar:** o promote casa por
`fire_produto_id`, código e nome; par só do Flow (`APT-…`, sem
`fire_produto_id`) com nome diferente do Fire vira produto novo ao lado dele
(par duplicado). Conferir no dryRun `fireOnly`/`nomeAmbiguo` dos pares antes do
`apply=true`.

**Fora do envio** (desde 15/10/2026): `sync-catalogo?excluir=3174,3381` tira esses
SEQs só do payload daquela rodada (a cópia local continua com o Fire inteiro). É
para segurar cadastro com problema no Fire sem travar o resto. Vale **por rodada**:
o diário e os scripts `tools/sync_catalogo_fire.py`/`tools/configurar_flowpcp.py`
não usam o parâmetro: sem a exclusão guardada (abaixo), não ligar o diário em `gravar`
com exclusão pendente (o `?excluir=` vale só para a rodada). Só dígitos
(zero à esquerda normalizado; `excluir` pode repetir); SEQ fora do catálogo
extraído dá 422 e nada é enviado.

**Composição e código do Apontaê** (`catalogo_composicao.py`, desde 15/10/2026, por empresa em
`PORTAL_FLOWPCP_CATALOGO_COMPOSICAO=mm`): cada item leva `componentes`
(`[{erpProdutoId, quantidade}]`, de `PRODUTOS_KIT`, um nível) e `codigoMes`
(`CODPROD_ALTERN` só dígitos, até 9, só em `simples` ativo, sem repetição no envio).
Chave `componentes` ausente = "não mexer" no Flow; `[]` só em par; kit/pack sem
`PRODUTOS_KIT` vai sem a chave (C3: o PCP pode ter composição provisória no Flow).
Leitura com erro, vazia ou nenhum kit do extract presente na leitura = ausente em todos. Filho fora do
envio, repetido, nulo, quantidade fora de 0 < q ≤ 100000, mais de 50 filhos ou kit dentro
dele mesmo: ausente naquele item, com log. Quantidade que não bate com o nome ("KIT C/5"
somando 3): o kit vai sem a chave (congelado, o Flow mantém o que tem) até o Fire ser
corrigido; aparece em `divergencia_nome`, no log como `composicao congelada` e põe o diário
em atenção.
O relatório ganha `composicao_portal` e `fora_do_envio_guardado` (SEQs tirados pela
exclusão guardada).
**Apply:** sem repetição depois de conectar; timeout de leitura 300 s, conexão/pool 30 s.
Erros da rota: `ExcluirGuardadoInvalidoError`, `ConfirmacaoSemApplyError` e
`ForaDoEnvioInexistenteError` = 422; `FlowPCPApplyEmAndamentoError` (o Flow já está
gravando) = 409; `ConnectTimeout`/`PoolTimeout` = 504 "nada foi gravado"; `ReadTimeout`/
`WriteTimeout` no apply = 504 "pode ainda estar em andamento" (simular antes de regravar).
Travas do Flow (`travaRemocaoAtiva`/`travaSubstituicaoAtiva`) liberadas só pela rota manual
(`confirmar_remocao=1`, `confirmar_substituicao=1`, exigem `apply=true`); o diário nunca manda.
A tela do ambiente mostra os blocos Portal/Flow/Apontaê do relatório e, depois de um apply
com trava ativa, o botão "Gravar liberando a trava".
**Exclusão guardada:** `PORTAL_FLOWPCP_CATALOGO_EXCLUIR_<SLUG>` sai de todo envio (manual
e diário), com a composição ligada ou não; valor inválido não envia nada. SEQ guardado que
não existe mais no extract = 422 apontando a variável.
**Diário:** com composição ligada também manda composição; loga WARNING quando
`travaRemocaoAtiva`/`travaSubstituicaoAtiva` é true, `erpSemChave > 0` ou a leitura do
Portal falhou (`leitura_ok` false); caso contrário, INFO.
Só ligar a composição depois de o Flow aceitar os campos (Fatia A); antes disso ele
recusa o lote com 400. Spec: `docs/superpowers/specs/2026-10-15-catalogo-composicao-portal-design.md`.

**Fiscal e material** (Etapa 1 de `docs/superpowers/specs/2026-10-16-flow-fonte-do-cadastro-design.md`,
tudo desligado por padrão). *Fiscal* (`catalogo_fiscal.py`, `PORTAL_FLOWPCP_CATALOGO_FISCAL`): o
catálogo passa a levar `ncm` (tira pontos, espaços e hífens; só vai se sobrar exatamente 8
dígitos), `tipoItemSped` (0–10 ou 99), `grupoErp` e `codigoNfe`. **`cor` não sai mais**: `DESCRICAO2` é a
"Descrição Interna" da tela do Fire, não cor (17/10/2026); a extração do catálogo nem a lê mais, e `cor_longa` deixou de existir. Valor
que não passa vai **ausente** ("não mexer" no Flow) e vira pendência no relatório (`ncm_vazio`,
`ncm_invalido`, `sped_invalido`). **Pacotes anteriores a 17/10/2026 mandam `DESCRICAO2` como `cor`; só ligar
`PORTAL_FLOWPCP_CATALOGO_FISCAL` com pacote posterior.** O Flow descarta campo desconhecido sem erro, então o sucesso só
vale com `fiscal.v1` nas `capacidades` da resposta. *Material* (`materiais_sync.run_materiais_sync`,
`PORTAL_FLOWPCP_MATERIAIS`): tudo fora do grupo 8 CONFECCIONADO vai para
`materiais_fire` (sempre) e ao Flow; bloqueado vai com `ativo=false` (o Flow não cria insumo
novo inativo, `inativoSemInsumo`, então só serve para desativar o que ele já tem). Fora do
envio (`materiais_portal.fora`): `sem_grupo`, `campo_excede`, `sem_nome`, `duplicado`.
Vão com pendência (`materiais_portal.pendencias`): `grupo_sem_nome` (vai como `GRUPO <n>`),
`ncm_vazio`, `ncm_invalido`, `sped_invalido`. A rota e `tools/sync_materiais_fire.py <slug>` fazem dryRun por
padrão; `apply=true`/`--apply` manda a sonda dryRun primeiro e só grava se o Flow declarou
`materiais.v1` (senão `ApplySemCapacidadeError` = 422, nada gravado). `materiais.dryrun.v1`
ausente marca `capacidade_ok=false` e loga ERROR. 403 do Flow = flag `materiaisErpAtivo`
desligada no tenant. A tela do ambiente tem um bloco para cada um.

**Marcas, nota e SEQs em aberto** (Etapa 3, `catalogo_sync.run_catalogo_sync`; só com
`flowpcp_catalogo_push` ligado e o interruptor do slug). *Marcas*
(`PORTAL_FLOWPCP_CATALOGO_MARCAS`): lê `PRODUTOS_MARCA` inteira e manda `marcasErp`
`[{codigo, nome}]` depois de `catalogo_marcas.filtrar`, porque uma marca fora das regras do Flow
derruba o lote com 400 (código inteiro 1..999999999, nome aparado de 1 a 30 caracteres, até 500;
o resto vai para `filtradas` com `codigo_invalido`, `nome_vazio`, `nome_longo` ou `excede_500`).
Leitura que falha = `marcasErp` ausente e `marcas_portal.leitura_ok=false`. *Nota*
(`PORTAL_FLOWPCP_CATALOGO_NOTA`): `temNotaFiscal` por produto, de `SELECT DISTINCT CODPRODUTO FROM
NOTAPROD`. `NOTAPROD` vazia ou sem nenhum SEQ do lote é tratada como anomalia (banco errado, base
restaurada): ERROR e **nada** de `temNotaFiscal` (mandar `false` em todos tiraria a nota de todo
produto no Flow); leitura que falha idem, nunca `false`. O relatório ganha `marcas_portal` e
`nota_portal` (`capacidade_ok`, contagens, e o que o Flow devolveu em `marcas`/`notaFiscal`); sem
`marcas.v1`/`produtos.nota.v1` na resposta o Flow ignorou o campo (`capacidade_ok=false`, ERROR, tirar
o slug da variável); `erro: true` no bloco do Flow vira `reenviar: true` e ERROR; `semPar`/`conflitos`
viram WARNING. 400 do Flow com marcas, nota ou composição ligadas aponta no erro as variáveis a retirar.
O diário (`catalogo_diario`) conhece os dois blocos e entra em atenção com `capacidade_ok`/`leitura_ok`
falsos ou `reenviar`. *SEQs em aberto*: em **toda** rodada com envio saem do lote os SEQs que o livro
da volta tem em trânsito (`cadastro_volta_repo.seqs_em_aberto`, o mesmo predicado "pode ter gravado"
da 9b, `_PODE_TER_GRAVADO_SQL`: `em_andamento`, `gravado_sem_volta`, `incerto`, `liberado`/`transitorio`
com SEQ ou código, e, só de `criar`, `orfao` e `erro_a_enviar`/`erro_registrado`/`superado` com SEQ ou
código), reportados em `fora_do_envio_volta`:
o Flow criaria outro produto para um SEQ que o Portal acabou de gravar (ou criou e o Flow não vinculou).
`orfao_fechado` fica fora da lista: fechar libera o SEQ para a ida, então só se fecha um órfão depois
de o Flow estar resolvido. A leitura do livro é falha fechada: se
falhar, a cópia local fica e o envio aborta.

Tabelas locais em `modules/persistence.md`. Ferramentas de linha de comando:
`tools/sync_catalogo_fire.py`, `tools/configurar_flowpcp.py`, `tools/sync_materiais_fire.py`.

## 5. Volta de cadastro (Flow → Fire)

`cadastro_volta.py` lê a fila de cadastros aprovados do Flow e a thread `flowpcp-envio`
(`envio_portal.ciclo`) chama `rodar_se_devido` no máximo a cada 5 min (`INTERVALO`). Contrato com o
Flow: P4a rev. 4. Um consumidor só por ambiente (`PORTAL_FLOWPCP_ENVIO=0` desliga a thread).

### Modos e portões

| Variável | Efeito |
|---|---|
| `PORTAL_FLOWPCP_CADASTRO_VOLTA=mm:simular,nas:desligado` | modo por ambiente: `desligado` (padrão), `simular`, `criar`, `criar_e_atualizar`. Entrada inválida = desligado, com ERROR uma vez |
| `PORTAL_FLOWPCP_CADASTRO_P0=mm` | portão da gravação. Sem o slug, `criar`/`criar_e_atualizar` voltam `recusado` (ERROR uma vez); o resultado já guardado ainda é reenviado antes da recusa |
| `PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR=mm` | portão de `inativar`. Sem ele a fila só pede `criar` (modo `criar`) ou `criar`+`atualizar` (`criar_e_atualizar`) e um `inativar` que chegar é pulado com WARNING |

| Modo | O que faz |
|---|---|
| `desligado` | não lê a fila; só reenvia resultado guardado, se houver |
| `simular` | `simular=1` (não reserva): valida, confere cópia local e Fire (conexão somente leitura, só SELECT parametrizado) e grava no livro o plano (`simulado`). Nunca grava no Fire nem confirma no Flow |
| `criar` | grava só `criar` |
| `criar_e_atualizar` | grava `criar` e `atualizar`; `inativar` só com o portão |

Variável de modo vazia ou ambiente `desligado`: sem modo e sem resultado guardado, `rodar()` não abre
token nem Flow; ele lista os ambientes ativos e olha o livro de cada um para reenviar resultado guardado.
Slug na variável que não é ambiente com FlowPCP habilitado loga WARNING uma vez.

Em `simular` a fila não tem cursor (só o confirmar a anda) e `simular` nunca confirma: a volta só
enxerga os 50 mais antigos (`LIMITE_FILA`), e o que já está no livro nem abre o Fire. O `simular` pede
só as ações que a gravação consumiria (`_acoes(slug, "criar_e_atualizar")`: `criar` e `atualizar`, mais
`inativar` só com `PORTAL_FLOWPCP_CADASTRO_VOLTA_INATIVAR`); sem esse filtro as inativações mais antigas
enchiam a janela e escondiam os `atualizar` (21/10/2026: 50 `inativar`, 0 dos 20 `atualizar`). Nos modos de
gravação o GET reserva os itens no Flow e pede no máximo 20 (`LIMITE_GRAVACAO`).

### Onde roda e a ordem do ciclo

`cadastro_volta.processar_ambiente` → `cadastro_gravacao.processar_gravacao` →
`cadastro_recuperacao.recuperar`. Nos modos de gravação, a ordem é esta (nada mais sobe se um
passo anterior parar o ciclo):

1. **Pendências**: `reenviar_pendencias` manda ao Flow o corpo guardado das linhas
   `gravado_sem_volta` e `erro_a_enviar` cuja espera venceu (até 20). Atualiza o gauge `sem_volta`. Um `401` no reenvio encerra o ciclo inteiro (`erro: credencial`, sem recuperação nem GET).
2. **P0**: sem `PORTAL_FLOWPCP_CADASTRO_P0` para o slug, devolve `recusado` e o ciclo termina.
3. **Recuperação** (só se há linha `em_andamento`, ou `transitorio` com SEQ, com mais de 15 min): abre a conexão bloqueante e resolve o que um processo caído deixou. Falha de conexão vira `{"erro": ...}` no resumo e o ciclo segue.
4. **"Aguardando modo"**: GET com `simular=1` e `acoes=` das ações que o modo **não** consome, só para contar. Roda antes do GET que reserva para não gastar a janela da reserva.
5. **GET** com reserva (`acoes=` do modo, limite 20).
6. **Triagem sem ERP**, item a item, nesta ordem: validação/ids legíveis; duplicata no lote; livro (`(cadastroId, versao)`); item inválido responde o erro; `material` (`tipo_nao_suportado`, ainda não é gravado); `inativar` sem portão (pulado); regra 9b (só `criar`: produto que já tem `criar` que pode ter gravado, pelo predicado único `cadastro_volta_repo._PODE_TER_GRAVADO_SQL`, o mesmo da ida do catálogo: `em_andamento`, `gravado_sem_volta`, `incerto`, `confirmado`, `orfao`, `orfao_fechado`, e `liberado`/`transitorio`/`erro_a_enviar`/`erro_registrado`/`superado` com SEQ ou código); escopo de `atualizar`/`inativar` (SEQ na cópia local ou criado pelo Portal, senão `seq_fora_do_escopo`). Só os itens que sobram "a gravar" abrem o ERP.
7. **Gravação**: abre `connect_escrita_cadastro`, lê nomes e marcas do Fire uma vez e, para cada item, confere (nome, marca, componentes; só SELECT), **reserva a linha no livro** (lease, com commit do SQLite) e só então grava. Antes de cada item checa `PRAZO_LOTE`.

### Regras de ouro do contrato P4a rev. 4 e como cada uma foi implementada

1. **Idempotência**: o livro decide antes do ERP. Linha com resultado guardado só reenvia o corpo, nunca chama o ERP de novo; `em_andamento` espera a recuperação; `incerto`, `orfao`, `orfao_fechado`, `confirmado`, `erro_registrado` e `superado` são pulados (WARNING). O SEQ vai ao disco (`ao_reservar` → `marcar_seq` + commit) **antes** do INSERT. **`gravado` nunca é descartado**: só `simulado`, `erro_definitivo`, `transitorio` e `liberado` são reaproveitáveis (`ESTADOS_DESCARTAVEIS`), e `transitorio`/`liberado` **com SEQ** não são, porque o INSERT pode ter comitado.
2. **Resultado guardado sai antes de tudo**, até sem a P0.
3. **Timeout incerto**: conexão que cai depois do comando de escrita nunca vira erro nem resposta. A linha fica `em_andamento` com o SEQ, o lote para e a recuperação confere no Fire. Erro só é respondido ao Flow com o rollback provado (`rollback_comprovado=True`: o Fire respondeu erro e desfez, ou nenhum comando de escrita saiu); sem a prova a linha fica `em_andamento` e o lote para.
4. **Lote < 10 min** (a reserva do Flow dura 10): `PRAZO_LOTE` = 8 min, checado antes de cada item; o que sobra fica para a reentrega do Flow. Rede travada no meio de um item ainda passa do prazo (o `fdb` não tem timeout de socket).
5. **Decisão pelo status** da resposta, nunca pela `acao` que o item supõe (a única exceção é o 409 de um `gravado`, abaixo): `cadastro_resultado.ler_resposta` devolve um `Desfecho` (`confirmado`, `erro_registrado`, `transitorio_registrado`, `orfao`, `superado`, `repetir` com espera crescente, `bug_corpo` com espera de 60 min e alerta, `credencial` que para os envios do ciclo). Mapa (`cadastro_resultado.ler_resposta`):

| Resposta do Flow | Desfecho |
|---|---|
| 200 `ja_registrado` | `confirmado` (se o corpo era `gravado`) ou `erro_registrado` (se era `erro`) |
| 200 `gravado` / `registrado_versao_superada` | `confirmado` para `gravado`; `bug_corpo` para `erro` |
| 200 `erro_registrado` | `erro_registrado` para `erro`; `bug_corpo` para `gravado` |
| 200 `transitorio_registrado` | `transitorio_registrado` para `erro`; `bug_corpo` para `gravado` |
| 200 sem `status` texto, ou status desconhecido | `repetir` |
| 409 `versao_superada` | `orfao` para `gravado` de `criar`; `superado` para `gravado` de `atualizar`/`inativar` (o caso comum, contrato §5) e para `erro` |
| 409 `conflito_codigo` | `orfao` para `gravado` de `criar`; `superado` com alerta `conflito_codigo` para `gravado` de `atualizar`/`inativar`; `bug_corpo` para `erro` |
| outro 409 | `bug_corpo` |
| 401 | `credencial` |
| 429 | `repetir` |
| outros 4xx | `bug_corpo` |
| 5xx, sem resposta | `repetir` |

No 409 de um `gravado`, `ler_resposta` devolve `orfao` e `cadastro_gravacao._orfao_ou_superado` decide pela `acao` que o livro guardou (o que o Portal fez de fato): órfão só se o Portal **criou** o produto. Em `atualizar`/`inativar` o produto já existia no ERP; a gravação fica e a linha fecha `superado`.

### Estados do livro

`cadastro_volta_repo.TRANSICOES` é uma **lista branca** (destino → origens) usada só por `marcar_desfecho`; destino fora dela levanta `ValueError`:

| Destino | Origem permitida |
|---|---|
| `confirmado`, `orfao` | `gravado_sem_volta` |
| `erro_registrado`, `transitorio` | `erro_a_enviar` |
| `superado` | `erro_a_enviar`; `gravado_sem_volta` só de `atualizar`/`inativar` (o `criar` que gravou nunca vira `superado`) |
| `incerto` | `em_andamento` |

O resto tem porta própria, de propósito: `reservar` (cria/reaproveita `em_andamento`), `marcar_gravado`
(`em_andamento` → `gravado_sem_volta`), `marcar_erro_a_enviar(rollback_comprovado=)` (só de `em_andamento`;
o SEQ só é zerado com `True`), `liberar_comprovado` (`em_andamento` → `liberado`, zera o SEQ),
`retomar_transitorio_com_seq` (`transitorio` com SEQ → `em_andamento`), `fechar_orfao`
(`orfao` → `orfao_fechado`), `adotar_manual` e `liberar_manual` (ferramenta do operador).

| Estado | O que é | O que pede da operação |
|---|---|---|
| `simulado`, `erro_definitivo` | resultado do modo `simular` | nada; descartáveis |
| `em_andamento` | lease de um item em gravação (ou de um processo que caiu) | nada nos primeiros 15 min; depois a recuperação resolve |
| `gravado_sem_volta` | gravado no Fire, o Flow ainda não aceitou | nada, reenvio automático; olhar se o alerta for `bug_corpo` |
| `erro_a_enviar` | erro definido, ainda não aceito pelo Flow | idem |
| `confirmado`, `erro_registrado`, `superado` | finais | nada; `superado` com alerta `conflito_codigo` (um `atualizar`/`inativar` gravado no Fire cujo código o Flow recusou): avisar a área de cadastro do Flow; aparece no cartão "Conflito de código" da tela e no `listar` da ferramenta |
| `transitorio` | erro passageiro (`erp_ocupado`, `conexao_recusada`...); o item volta no GET | nada se sem SEQ; **com SEQ** a recuperação o retoma; passado o prazo aparece como incerto na tela |
| `liberado` | voltou para a fila (recuperação ou operador); as portas zeram o SEQ, então o item é reservado de novo na reentrega do Flow | nada |
| `orfao` | produto que o Portal **criou** no Fire e o Flow não vinculou (`versao_superada`, `conflito_codigo` de um `criar`) | avisar a área de cadastro com o código; o SEQ fica fora da ida enquanto `orfao`; `fechar-orfao` com nota só depois de o Flow estar resolvido (fechar devolve o SEQ à ida) |
| `orfao_fechado` | final: `orfao` fechado por uma pessoa (`nota_operador`). Não vira `superado` para não apagar a prova de que o produto existe | nada |
| `incerto` | sem prova do que aconteceu; nem grava nem responde | `adotar` ou `liberar` pela ferramenta do operador |

### Caminhos manuais (o que a recuperação e a ferramenta não decidem sozinhas)

Nos quatro casos a linha está segura: nada grava de novo e a 9b segura o produto. O item do Flow é
pulado em todo ciclo até alguém agir.

- **`em_andamento` em motor ≥ 4.0** (só o DEV, Firebird 5; a produção é 2.5). A recuperação não roda
  (ERROR uma vez, `{"parado": "motor_sem_bloqueio"}`) e `liberar` recusa: sem a leitura bloqueante não
  há prova de ausência. A ferramenta **não tem porta** para `em_andamento`, de propósito. O que fazer:
  conferir o SEQ reservado com `tools/produto_no_fire.py <slug> --seq N` (sem SEQ, a janela por
  `DATA_CAD`) e levar ao Samuel; destravar a linha no DEV é decisão dele, fora do código.
- **`incerto` "DATA_CAD ilegível"** (log da recuperação: o SEQ reservado existe no ERP com
  `DATA_CAD` nulo ou ilegível, ou seja, a linha foi mexida à mão). `adotar` e `liberar` recusam, porque
  os dois dependem dessa data para provar se o produto é ou não é o nosso. O que fazer: conferir o
  produto com `tools/produto_no_fire.py <slug> --seq N` e com quem mexeu no Fire. Com a data de
  cadastro legível de novo (correção feita na tela do Fire por quem cuida do cadastro), `adotar` (é o
  nosso) ou `liberar` (cadastrado antes da reserva) decidem pela prova de sempre.
- **`incerto` com alerta `nome_do_proprio_produto`** (o nome do item já está no ERP num SEQ que o livro
  liga a este mesmo produto do Flow, por outra versão). Nada foi enviado ao ERP nesta versão, e
  `adotar` recusa porque o SEQ é de outra linha. O que fazer: no log (ou em `listar`) está a outra
  linha (`cadastroId vN`, estado) e o SEQ X. A área de cadastro do Flow vincula o produto ao código X,
  como num órfão. Depois, `liberar` esta linha (sem SEQ: conferir a janela): a reentrega sai como
  `atualizar` com o código X (contrato §4) e é processada.
- **`criar` novo de um produto com um `criar` anterior `confirmado`, `erro_a_enviar`, `erro_registrado`
  ou `superado` com SEQ no livro** (log da 9b: "conferir com `tools/produto_no_fire.py`"). O `criar`
  anterior pode ter gravado o produto, então a versão nova nem grava nem responde; a ferramenta do
  operador não age nesses estados. O que fazer: conferir o SEQ do log com
  `tools/produto_no_fire.py <slug> --seq N` e levar ao Samuel com o SEQ, o estado da linha anterior e
  o que o Fire mostrou; a decisão (vincular no Flow ou outra) é dele, fora do código.

### Conexões com o Fire (`app/erp/connection.py`)

- `connect_escrita_cadastro`: READ COMMITTED + REC_VERSION, WAIT com `isc_tpb_lock_timeout` de **10 s** (sem isso um SEQ aberto na tela do Fire travaria o gravador sem limite). A saída é **sempre rollback**, nunca commit (`default_action = "rollback"`): o gravador comita cada cadastro; o que sobra pendente não prova nada.
- `connect_leitura_bloqueante`: READ COMMITTED + **NO_REC_VERSION**, WAIT, lock timeout de 10 s, saída sempre rollback. A transação é **WRITE**, não READ ONLY, porque READ ONLY + READ COMMITTED pode ser tratada como pré-comitada e ignorar o NO_REC_VERSION (não provado no Fire 2.5; ver riscos). Nada escreve por ela: o guarda `SoSelect` só deixa passar SELECT (recusa `WITH LOCK`, `GEN_ID`, `NEXT VALUE FOR`, comentário) e `commit` é proibido.
- Com NO_REC_VERSION, ler um registro cujo INSERT está pendente em outra transação espera e, vencido o timeout, levanta conflito de lock: a recuperação para e **não marca nada** (um INSERT pendente nunca parece ausência). No Firebird 4+ o ReadConsistency faz o read committed ignorar o NO_REC_VERSION, então a recuperação **não roda em motor ≥ 4.0** (nem em versão ilegível): ERROR uma vez e `{"parado": "motor_sem_bloqueio"}`.

### Recuperação (`cadastro_recuperacao.recuperar`)

Idade mínima `IDADE_MINIMA_RECUPERACAO` = `PRAZO_LOTE` + 7 min = **15 min** (cobre um processo travado na rede). Só LÊ o Fire; cada linha sai por uma porta:

- **`atualizar`/`inativar`**: idempotentes, `liberar_comprovado` (contagem `reaplicar`; a métrica sai como `liberado`).
- **`criar` sem plano e sem SEQ**: nada saiu ao ERP, libera.
- **`criar` com SEQ**: `ler_produto(SEQ)`. Não existe → libera. `DATA_CAD` ilegível → incerto. `DATA_CAD` anterior à janela da reserva → libera (é produto de outro). Senão compara `DESCRICAO`, `UNIDADE`, `GRUPO`, `CODIGO_EAN13`, `CLASSIF_FISCAL` e a composição com o plano: tudo bate → **adota** (`marcar_gravado`, o envio fica com `reenviar_pendencias`); qualquer divergência → incerto. **Descrição diferente sozinha não libera** (pode ser renomeado na tela ou trigger): vai a incerto.
- **`criar` sem SEQ** (o commit do `marcar_seq` se perdeu): `cadastro_fire.candidatos_por_data` busca por `DATA_CAD` na janela, **sem `GRUPO`** (um grupo trocado na tela tem que aparecer). Zero → libera; mais de um → incerto; um só, com nome exato, não ligado a outra linha do livro e com tudo batendo → adota; qualquer outro → incerto.
- **Janela**: dias locais da reserva (`iniciado_em`; reserva a menos de 10 min da meia-noite conta o dia seguinte) com **folga de 1 dia** para cada lado (outro fuso).
- Falha de conexão ou de lock **para** a recuperação (`parado`), nada marcado; outro erro de leitura vira incerto só naquela linha. `ligado_a_outra_linha` (qualquer linha com o SEQ) manda para incerto. Linha `transitorio` com SEQ passa pelas mesmas regras e a retomada vai no mesmo commit da porta.

### Ferramentas

- `tools/cadastro_volta_operar.py <slug> <comando>` (nunca escreve no ERP nem no Flow, só o livro; sem `--confirmar` só mostra): `listar` (incerto, órfão, transitório/liberado com SEQ e `superado` com conflito de código); `adotar <cadastroId> <versao> <seq> --operador NOME [--aceito-divergencia]` (prova **presença**: lê o ERP somente leitura, recusa SEQ de outra linha, diferente do reservado, cadastrado antes da reserva ou inexistente); `liberar <cadastroId> <versao> --operador NOME [--candidatos-conferidos 4005,4006 | --nenhum-candidato]` (prova **ausência** pelo mesmo caminho da recuperação: conexão bloqueante, motor < 4, idade mínima; sem SEQ lista a janela e exige que o operador informe os SEQs conferidos, ou `--nenhum-candidato`, e relê a janela na hora); `fechar-orfao <cadastroId> <versao> --operador NOME --nota TEXTO`.
- `tools/sanear_marcas_fire.py <slug>`: insere em `PRODUTOS_MARCA` as marcas ativas do Flow (lista de 17/10/2026). Dry-run por padrão; `--gravar` exige `--backup-servidor <.fbk no servidor>`, refaz o plano, recusa grafia parecida, faz o backup, grava numa transação, relê e confere. **Só roda com `--gravar` com o ok do Samuel.** Marca nova depois de 17/10 é cadastro manual.
- `tools/perfil_cadastro_fire.py <slug> --ultimos N` e `tools/produto_no_fire.py <slug> --seq N`: só leitura (P0), perfil das colunas de `PRODUTOS` nos últimos pares e kits do grupo 8 e rastro de um SEQ em toda tabela com coluna de produto. Servem ao diff que preenche `PADROES_P0`.

### Mensagens ao Flow

A frase que a área de cadastro lê sai sempre de `cadastro_resultado.mensagem_para_flow` (ou `mensagem_nome_existe`), sem jargão e **sem o valor recebido**, SQL ou `str(exc)`; o texto técnico (até 500 caracteres) fica só no livro (`erro_mensagem`) e no log. Código desconhecido = "Motivo não identificado. Fale com o suporte do Portal." `nome_existe` e `ja_criado_no_erp` levam o SEQ como **primeiro número** da frase (o Flow mostra esse). Códigos de erro do Portal: `contrato_invalido`, `campo_excede_fire`, `caractere_invalido`, `componente_ausente`, `componente_bloqueado`, `marca_ausente_erp` (definitivo, contrato §7), `nome_existe`, `ja_criado_no_erp`, `seq_fora_do_escopo`, `seq_inexistente`, `tipo_nao_suportado`, `erp_recusou`, e os transitórios `conexao_recusada`, `erp_ocupado`, `consulta_erp_falhou`. Em `simular`, item que nem é objeto vira `contrato_invalido` com id `sem-id-<hash>`; nos modos de gravação, item sem `cadastroId`/`versao` legíveis é pulado com WARNING.

### Validação do item (todos os modos)

Item inválido ou que quebra não trava a fila; `chaves_desconhecidas` no item de topo só gera WARNING (uma vez por ambiente e nome). `nome_existe` compara em Python (NFC, espaços internos colapsados, `casefold`) sobre `SELECT SEQ, DESCRICAO FROM PRODUTOS`, lido uma vez por ciclo (o `UPPER` do Firebird não é confiável para Ç/Ã/Ú em WIN1252). `marcaErp.codigo` precisa existir em `PRODUTOS_MARCA` (lida uma vez por ciclo), senão `marca_ausente_erp`; `MARCA` é gravada com o nome da tabela. Componentes: `criar` de kit/pack exige lista; `atualizar` com `componentes: null` não mexe; `inativar` ignora; `simples`/`material` exigem `null`. `atualizar`: `ean` e `codigoNfe` `null` limpam no ERP; os demais `null` = não mexer (`ncm`, `tipoItemSped`). `cor` não tem coluna: `DESCRICAO2` não é gravada pela volta. Texto opcional `""` = `contrato_invalido`.

**`camposAlterados` (contrato rev. 5 §2.3).** Num `atualizar`, o Portal escreve no ERP só as colunas dos campos listados (`cadastro_schema.vai_ao_erp`, usado por `_campos`, `_componentes` e `_contra_o_fire`): campo fora da lista não é escrito nem conferido no ERP, mesmo preenchido (é referência). `ean`/`codigoNfe` listados com `null` limpam; `componentes` só troca a composição se listado; `marcaErp` listado grava `MARCA` pelo nome de `PRODUTOS_MARCA`. `nome` listado que já existe em **outro** SEQ do ERP (mesma comparação normalizada) = `nome_existe` definitivo, com o código na frase. Regras de borda, todas `contrato_invalido`: valor fora do enum, repetição, lista em `criar`/`inativar`, `null` explícito em `atualizar`, `componentes` listado em `simples`/`material`, e campo listado com `null` que não seja `ean`/`codigoNfe`. **Lista vazia**: a triagem segue (livro, escopo), mas o ERP não é aberto nem chamado; a linha passa por `reservar` → `marcar_gravado(erp_produto_id=<o do item>)`, as mesmas portas de um `atualizar` gravado (nenhum caminho novo no livro), e o `POST gravado` sai com o `erpProdutoId` do item (métrica `sem_mudanca`). `atualizar` **sem** a chave segue a rev. 4 (grava tudo). Em `simular`, o plano é o mesmo da gravação, com `campos_alterados`; lista vazia = `{"acao": "atualizar", "sem_mudanca": true}`.

### Métricas

- `portal_flowpcp_cadastro_volta_total{env,resultado}`. `resultado`: `simulado`, `erro_definitivo`, `ja_no_livro` (simular); `criado`, `atualizado`, `inativado` (gravado no Fire); `sem_mudanca` (`atualizar` com `camposAlterados` vazia: respondido sem chamar o ERP); `adotado`, `liberado`, `incerto` (recuperação); `confirmado`, `erro_registrado`, `transitorio`, `orfao`, `superado`, `repetir`, `bug_corpo`, `credencial` (o que o Flow respondeu ao POST).
- `portal_flowpcp_cadastro_volta_pendentes{env}` (lidos na última leitura, não o tamanho da fila) e `..._mais_antigo_segundos{env}`.
- `portal_flowpcp_cadastro_volta_sem_volta{env}`: resultado (gravado no Fire ou erro) ainda não aceito pelo Flow, `gravado_sem_volta` + `erro_a_enviar`, atualizado em todo ciclo de gravação (também quando recusado pela P0). Acima de zero por muito tempo = Flow não aceita o resultado.
- `portal_flowpcp_cadastro_volta_aguardando_modo{env}`: itens aprovados que o modo não grava (contagem aproximada, até 50, lida sem reservar; em `criar` conta também o `atualizar` derivado de `criar`, que esse modo grava). `0` medido quando o modo consome as três ações (`criar_e_atualizar` com o portão de `inativar`). **Não publica série** (a série é removida) quando o modo é `desligado`/`simular`, a P0 está desligada, a credencial foi recusada ou o Flow não respondeu à contagem (um 400 do Flow rev. 3 vira WARNING uma vez): ausência de série é "não medido", nunca zero.

A tela do ambiente lê tudo por `GET .../flowpcp/cadastro-volta` (ver `modules/web.md`). O último ciclo
(`cadastro_volta.ultimo_ciclo(slug)`) guarda só contagens, `capacidade_ok`, `aguardando_modo` e o código/classe do
erro, em memória: some quando o portal reinicia.

### Riscos registrados e pendências da P0 (parte B)

- **Leitura bloqueante provada** (18/10/2026, cópia de abril da MM, Firebird 2.5.9 local, duas conexões): com um INSERT pendente, `ler_produto` e `candidatos_por_data` dão `lock time-out on wait transaction` (SQLCODE -901) em vez de "não existe"; a leitura `connect_somente_leitura` devolve `None` (a ausência falsa que a leitura bloqueante evita); depois do rollback, `None` na hora. Produção roda 2.5.7: mesma família, não reprovado lá.
- **`PRODUTOS.DATA_CAD` não tem índice** (`PLAN (PRODUTOS NATURAL)`, 18/10): a busca sem SEQ varre a tabela e **trava em qualquer edição pendente de qualquer produto** (provado: UPDATE pendente no SEQ 100 parou `candidatos_por_data`; `ler_produto` de outro SEQ seguiu). Só liveness: a recuperação sem SEQ adia e nunca marca errado. Alternativa barata, não feita: buscar por faixa de SEQ (PK). Também visto: `DATA_CAD` do SEQ 3566 é 16/04 com estoque desde 31/03, então não é sempre a data de criação.
- **Trava entre processos por ambiente: backlog** (`docs/BACKLOG.md`). A spec garante um consumidor por ambiente; dois processos no mesmo ambiente não são impedidos por código, só mitigados pela idade mínima de 15 min e pela leitura bloqueante.
- **`PADROES_P0` preenchido** (18/10/2026, `app/erp/cadastro_fire.py`): `EST_MINIMO_GERENCIA='Nao'`, `PRODUZIDO='Nao'`, `CODORIG_MERC=0`, `PRE_CADASTRO='Nao'`. Diff de par e kit cadastrados pela tela (3566, 3382) contra par e kit gravados pelo Portal (3567, 3568) numa cópia da MM; bate com o perfil da parte A. `CODSUBGRUPO=1` ficou de fora (amostras da tela nulas). No cadastro, a tela só grava em `PRODUTOS`/`PRODUTOS_KIT` (`produto_no_fire.py`: o resto é estoque, entrada/saída e nota, de operações posteriores). **Falta** abrir na tela do Fire um par e um kit gravados pelo Portal (Step 8, com o Samuel).
- Caminhos residuais de liberação com produto no ERP (aceitos): exigem `DATA_CAD` editado à mão no Fire ou perda do commit do WAL entre duas reservas (a conexão do livro, na volta e na ferramenta do operador, abre com `synchronous=FULL` por `cadastro_volta_repo.duravel`; o resto do router segue `NORMAL`, então só sobra um disco que mente sobre o fsync); o custo é um produto duplicado, visível no Flow como `conflito_codigo`/órfão.

## 6. Trava de preço (contrato precos v1)

`precos.rodar(agora)`, na thread `flowpcp-envio` (`envio_portal.ciclo`), fora das janelas.
Por ambiente ativo com a trava ≠ `desligado` e `environments.cnpj` válido, com a config do
Flow de `preco_fonte_slug or slug` (Nasmar: config da MM, `empresaFaturadora` = CNPJ da Nasmar;
cópia no db do próprio ambiente):

- **Tabela** (`GET /precos/tabela`) a cada 60 s: página em transação até `temMais: false`;
  `reiniciar` ou cópia de outra empresa → `precos_repo.reiniciar`. Guarda as `capacidades`.
- **Só em `ligado`**, a cada 30 s e só com `precos.aprovacao.v1` declarado: revisões
  `pendente_envio` (`marcar_tentada` antes do POST; False = não envia), retiradas, e
  decisões (lê sem cursor → `gravar_decisao` → confirma com `empresaFaturadoraCnpj`, até vir vazia).
- Fila em rotação: revisões por `COALESCE(tentada_em, criado_em)` (`marcar_tentada` grava a
  hora de cada tentativa), lote de 50; retirada que falhou vai para o fim
  (`preco_revisao.retirada_presa_em`, a mais antiga primeiro; acima de 1000 presas a ordem é
  esquecida, nenhuma retirada se perde). Um pedido preso não segura os outros. Mais de 5 falhas
  passageiras da mesma revisão (`preco_revisao.tentativas`, zera em qualquer outra resposta) →
  aviso único no log (por processo). Decisão de revisão acima da maior local → `descartada`
  e aviso no log (sinal de restore de backup do SQLite).
- Retenção (`RETENTION_DAYS`, padrão 180, a mesma do worker): no máximo 1x por dia por ambiente
  (memória do processo; reiniciar roda de novo, é idempotente), `preco_revisao_repo.reter` apaga
  em lotes de 500 (até 20 lotes por tabela) as revisões `substituida`/`retirada` mais velhas que
  o corte que não são a última do pedido nem a última conhecida pelo Flow, e as decisões já
  confirmadas que não são da revisão vigente. Não toca a cópia da tabela (`preco_*_v1`: rede e
  chaves são upsert, `preco_tabela_v1` é a cópia append-only do Flow), o piso (1 linha por
  pedido) nem decisão não confirmada. Não há log de sync em tabela. Falha só loga.
  **Por desenho, o que decide fica e cresce com o volume:** a última revisão de todo pedido
  (inclusive a vigente `enviada` de pedido já faturado ou esquecido), a última conhecida pelo
  Flow e as decisões da revisão vigente nunca saem; só o histórico intermediário é podado.
- Estado (`gerado_em`, último erro, `pendente_envio`/`bloqueada_local`/`recusada_flow`) no log
  `flowpcp.precos estado env=…`, só quando muda. Tentativas e retiradas presas sobrevivem ao
  reinício (SQLite do ambiente); pausas, intervalos, capacidades e avisos vivem na memória do
  processo: reiniciar = tentar de novo, o que é aceitável.

Quatro rotas do Flow (`/api/portal-pedidos/precos/…`): (a) `GET tabela`, (b) `POST aprovacoes`,
(c) `GET decisoes` e `POST decisoes/confirmar`. Resumo do tratamento (`precos.py`):

| Resposta | Rota | Efeito |
|---|---|---|
| 400 `cursor_invalido` (com cursor salvo) ou `reiniciar: true` | a | reinicia a cópia (máx. 2 por volta; depois pausa tabela e aprovação por 15 min) |
| 2xx sem objeto JSON (`resposta_sem_json`), 429, 503, rede | a, b, c | transitório: tenta no próximo ciclo, nada muda na revisão; em (b) encerra o envio da volta (retiradas e decisões seguem) |
| 500, 502, 504 | a, c | transitório: tenta no próximo ciclo |
| 500, 502, 504 | b | pula só aquele pedido ou retirada; a fila segue e ele vai para o fim. 3 seguidos na volta (revisões e retiradas somadas, zera com qualquer outra resposta) = disjuntor: encerra o envio da volta (decisões seguem) |
| 201/200 | b (pedido) | `enviada` |
| 2xx com JSON fora do contrato | b | `enviada`, com o corpo bruto guardado (o Flow gravou) |
| 2xx com JSON fora do contrato | `POST decisoes/confirmar` | confirmação tida como feita (se não, a decisão volta e é reconfirmada) |
| 2xx com JSON fora do contrato | `GET decisoes` | pausa a aprovação por 15 min |
| 2xx com JSON fora do contrato | a | pausa tabela e aprovação por 15 min |
| 409 `revisao_obsoleta`/`conteudo_divergente` | b | vigente aqui: sobe o piso e reenvia (máx. 3 por pedido na volta); já substituída: não reenvia |
| 409 `revisao_obsoleta` sem `revisaoVigente` | b (pedido) | pausa só a aprovação por 15 min |
| 409 `revisao_obsoleta` | b (retirada) | reenvia com `revisaoVigente`; sem revisão nova, ou segundo 409 seguido com outra `revisaoVigente`, WARNING uma vez por revisão (slug, pedido, revisões) e a retirada vai para o fim |
| 200 com `aprovacaoId` nulo | b (retirada) | retirada feita |
| 413, 400 `payload_invalido`, corpo inválido local | b | `bloqueio_local`: sai da fila; volta com conteúdo novo (nova revisão) ou pelo botão "Reenviar bloqueados" do admin (`POST .../preco-trava/desbloquear`) |
| 422 `rede_desconhecida`/`produto_desconhecido`/`empresa_faturadora_desconhecida` | b | `recusada_flow`; reenvio do operador cria a revisão seguinte (rev+1) |
| 403, 400/401/404, outros | b, c | pausa só a aprovação por 15 min (a tabela segue) |
| demais erros | a | pausa tabela e aprovação por 15 min |
| decisão de outra empresa faturadora | c | não grava; confirmada `descartada` |

Riscos conhecidos:
- Uma decisão fora do schema (divergência entre o pydantic daqui e o zod do Flow) recusa a página inteira de `GET decisoes` e pausa a aprovação por 15 min, em laço,
  até o Flow parar de mandá-la.
- `reiniciar: true` e `cursor_invalido` apagam a cópia antes de baixá-la de novo: até a última
  página chegar, a cópia fica sem `completo_em` e a trava bloqueia (`copia_desatualizada`).
  Autocura na mesma volta ou na seguinte (60 s), se as páginas vierem.

Intervalos: tabela 60 s (trava ≠ `desligado`), envio e decisões 30 s (só `ligado`); independem
das janelas do Flow. `PORTAL_FLOWPCP_PRECOS=0` desliga tudo.

**Só a thread do processo web roda isso**: `envio_portal.ciclo` chama `precos.rodar`; o worker
docker (`python -m app.worker`) **não** chama. O deploy Windows roda `ui.py`, então vale.
Ligar a trava: runbook em `modules/erp.md` ("Trava de preço por rede").

Cliente: `get_precos_tabela`, `post_precos_aprovacao`, `get_precos_decisoes`,
`post_precos_confirmar` (GET com `read_only_policy`, POST com `idempotent_post_policy`);
`FlowPCPClientError` carrega `status_code`, `error` e `details` do corpo de erro.
Testes: `tests/test_flowpcp_precos.py`, `tests/test_flowpcp_precos_schema.py`.

## Testes

Cobertura larga — 25+ arquivos. Os principais:

```bash
.venv/bin/pytest tests/test_flowpcp_hook.py tests/test_flowpcp_exporter.py \
  tests/test_flowpcp_poll.py tests/test_flowpcp_intercompany.py \
  tests/test_catalogo_sync.py tests/test_clientes_sync.py -v
```

Fiscal, material e volta: `test_catalogo_fiscal.py`, `test_catalog_extract.py`,
`test_materiais_extract.py`, `test_materiais_sync.py`, `test_sync_materiais_route.py`,
`test_cadastro_schema.py`, `test_cadastro_volta.py`, `test_cadastro_volta_route.py`,
`test_admin_ambiente_edit_cadastro.py`, `test_cadastro_volta_gravar.py`, `test_cadastro_volta_recuperar.py`,
`test_cadastro_volta_repo.py`, `test_cadastro_volta_operar.py`, `test_cadastro_resultado.py`, `test_cadastro_fire.py`,
`test_catalogo_marcas.py`, `test_sanear_marcas.py`, `test_perfil_cadastro_fire.py`.

Config e wiring: `test_flowpcp_config.py`, `test_flowpcp_worker_wiring.py`,
`test_intercompany_config.py`. Mappers: `test_flowpcp_mapper_*.py`.

## Armadilhas

- **Gate OFF não é falha.** `catalogo_push`/`clientes_push` desligados só suprimem o
  envio; a cópia local continua atualizando.
- **O Flow casa cliente só por CNPJ.** Mandar razão social não ajuda; o CNPJ tem que ser
  o de faturamento correto.
- **`/recebimento` é insert-only e deduplica por `externalId`.** Re-enviar não corrige um
  pedido que subiu errado — corrigir histórico exige patch do lado do Flow.
- **Cliente criado automático no Flow nasce sem marca** (`grupoCodigo` vazio). Ver
  `docs/BACKLOG.md` §3.5.
