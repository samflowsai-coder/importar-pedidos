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
| `catalogo_fiscal.py` | campos fiscais do produto (NCM, SPED, grupo, `codigoNfe`, cor): normaliza e devolve pendências |
| `capacidades.py` | o que o Flow declara aceitar (`fiscal.v1`, `materiais.dryrun.v1`, `materiais.v1`, `cadastros.v1`) |
| `materiais_sync.py` / `materiais_schema.py` | carga de material (grupo ≠ 8) Fire → cópia local → Flow |
| `cadastro_volta.py` / `cadastro_schema.py` | volta Flow → Fire dos cadastros aprovados (só `simular` na Etapa 1) |
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
dígitos), `tipoItemSped` (0–10 ou 99), `grupoErp`, `codigoNfe` e `cor`. Valor que não passa vai
**ausente** ("não mexer" no Flow) e vira pendência no relatório (`ncm_vazio`, `ncm_invalido`,
`sped_invalido`, `cor_longa`). O Flow descarta campo desconhecido sem erro, então o sucesso só
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

Tabelas locais em `modules/persistence.md`. Ferramentas de linha de comando:
`tools/sync_catalogo_fire.py`, `tools/configurar_flowpcp.py`, `tools/sync_materiais_fire.py`.

## 5. Volta de cadastro (Flow → Fire)

`cadastro_volta.py` lê a fila de cadastros aprovados do Flow
(`client.list_cadastros_pendentes`, até 50 por rodada, o máximo do Flow) e a thread `flowpcp-envio`
(`envio_portal.ciclo`) chama `rodar_se_devido` no máximo a cada 5 min. Modo por ambiente em
`PORTAL_FLOWPCP_CADASTRO_VOLTA`:

| Modo | Efeito na Etapa 1 |
|---|---|
| `desligado` (padrão) | não lê a fila |
| `simular` | lê a fila, valida cada item, confere a cópia local e o Fire (conexão somente leitura, só SELECT parametrizado) e grava no livro o plano do que seria feito |
| `criar`, `criar_e_atualizar` | **recusados** (ERROR no log, `recusado`) até a Etapa 3: gravação exige P0 e `cadastros.v1` |

Em `simular` o portal **nunca grava no Fire e nunca confirma no Flow**. O livro é a tabela
`cadastro_volta`, cuja chave `(cadastro_id, versao)` é o lease (o mesmo item não é processado
duas vezes). **Limitação de `simular`:** a fila do Flow não tem cursor (só o confirmar a anda)
e `simular` nunca confirma, então a volta só enxerga os 50 mais antigos; o que já está no livro
nem abre o Fire. Estados registrados: `simulado` (com `plano_json`) e `erro_definitivo` (com
`erro_codigo`/`erro_mensagem`, sem o valor recebido; ex.: `seq_fora_do_escopo`, `nome_existe`,
`componente_invalido`, `consulta_fire_falhou`, `livro_falhou`). Item inválido ou que quebra não
trava a fila (item que nem é objeto vira `contrato_invalido` com id `sem-id-<hash>`;
`capacidades` malformada = nenhuma); `cadastros` que não é lista = `erro_transitorio`
`resposta_invalida`, nada registrado. Fire fora aborta a rodada sem registrar nada e tenta no
ciclo seguinte. `nome_existe` compara em Python (`casefold`) sobre `SELECT SEQ, DESCRICAO FROM
PRODUTOS`, lido uma vez por ciclo: o `UPPER` do Firebird não é confiável para Ç/Ã/Ú em WIN1252.
Componentes: `criar` de kit/pack exige lista; `atualizar` com `componentes: null` não mexe na
composição (plano com `"componentes": null`, sem consulta de componente no Fire); `inativar`
ignora o campo; `simples`/`material` exigem `null`. Texto opcional `""` = `contrato_invalido`.
Variável vazia: `rodar()` volta na hora, sem listar ambientes nem abrir token; slug na variável
que não é ambiente com FlowPCP habilitado loga WARNING uma vez. Métricas:
`portal_flowpcp_cadastro_volta_total{env,resultado}` (`simulado`, `erro_definitivo`,
`ja_no_livro`), `portal_flowpcp_cadastro_volta_pendentes{env}` (lidos na última leitura, não o
tamanho da fila) e
`portal_flowpcp_cadastro_volta_mais_antigo_segundos{env}`. A tela do ambiente lê o livro por
`GET .../flowpcp/cadastro-volta` (modo, contagem por estado e os 50 últimos, plano truncado em 2 KB,
`ultimo_ciclo` deste processo e `thread_desligada` com `PORTAL_FLOWPCP_ENVIO=0`). O último ciclo
(`cadastro_volta.ultimo_ciclo(slug)`) guarda só contagens, `capacidade_ok` e o código/classe do
erro, em memória: some quando o portal reinicia.

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
- Tabela: 400 `cursor_invalido` com cursor salvo → reinicia a cópia (no máximo 2 reinícios
  por volta; depois pausa).
- Rota (b): 201/200 → `enviada` (2xx fora do contrato também, com o bruto guardado); 409 da
  vigente → `subir_piso` e reenvio na mesma volta; 409 de revisão já substituída → não reenvia;
  422 de conteúdo → `recusada_flow` com texto legível; 413, 400 `payload_invalido` e corpo
  inválido aqui → `bloqueio_local` (revisão fora da fila; o portão bloqueia com o motivo e só
  conteúdo novo cria outra revisão); 403/400/401/404/outros → pausa SÓ da aprovação (15 min),
  sem tocar a revisão; 429/5xx/rede → encerra o envio da volta (retiradas e decisões seguem).
  Retirada: 409 → reenvia com `revisaoVigente`; 200 com `aprovacaoId` nulo = feita; sem saída
  → loga uma vez. Decisão de outra empresa → confirmada `descartada`.
- Estado (`gerado_em`, último erro, `pendente_envio`/`bloqueada_local`/`recusada_flow`) no log
  `flowpcp.precos estado env=…`, só quando muda. Pausas e intervalos vivem na memória do processo.

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
`test_admin_ambiente_edit_cadastro.py`.

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
