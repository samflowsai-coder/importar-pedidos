# Módulo: web (FastAPI)

## Responsabilidade
Interface humana de upload → preview → commit. Uvicorn em `PORTAL_HOST:PORTAL_PORT`, default `127.0.0.1:3636` (`ui.py`).

## Arquivos críticos
- `app/web/server.py` — rotas FastAPI.
- `app/web/preview_cache.py` — cache em memória de pré-visualizações.
- `app/web/static/index.html` — Pedidos (vanilla JS, dark-first).
- `app/web/static/admin-usuarios.html` — Configurações > Usuários.
- `app/web/static/config-banco.html` — Configurações > Banco de dados (Firebird).
- `app/web/static/config-diretorios.html` — Configurações > Diretórios (substitui modal antigo).
- `app/web/static/css/tokens.css`, `shell.css`, `js/shell.js` — app shell compartilhado.
- `ui.py` — entrypoint `uvicorn`.

## App shell (sidebar persistente)
Todas as páginas autenticadas (Pedidos, Configurações/*) carregam:
```html
<link rel="stylesheet" href="/static/css/tokens.css?v=1">
<link rel="stylesheet" href="/static/css/shell.css?v=1">
<script src="/static/js/shell.js?v=1" defer></script>
<div id="app-shell"></div>
```
`shell.js` injeta sidebar (Pedidos + Configurações com sub-itens Banco/Diretórios/Usuários) e topbar (status Firebird, user pill, logout). O grupo Configurações é admin-only. Páginas públicas (`login.html`, `invite.html`) carregam apenas `tokens.css`.

API pública para páginas-filho:
- `window.appShell.showError(msg, traceId)` — toast com botão "copiar trace_id".
- `window.appShell.showSuccess(msg)`, `showInfo(msg)`.
- `window.appShell.refreshFb()` — força refetch do `/api/config` para atualizar o pill Firebird.
- `window.__shellUser` — cache do `/api/auth/me` (use em vez de chamar de novo).

## Rotas
- `GET /` → `index.html` (Pedidos).
- `GET /configuracoes/banco` → `config-banco.html` (admin gating client-side; writes admin-only via API).
- `GET /configuracoes/diretorios` → `config-diretorios.html`.
- `GET /configuracoes/usuarios` → `admin-usuarios.html`.
- `GET /admin/usuarios` → 301 → `/configuracoes/usuarios` (legado).
- `GET /health` → `{"status":"ok"}` — sem auth.
- `GET /metrics` → scrape Prometheus (text/plain, sem auth — restringir no
  reverse-proxy em produção). Atualizado por jobs a cada 15s (Gauges) e em
  tempo real (Counter/Histogram).
- `GET /api/config` → `{watchDir, outputDir, exportMode, firebirdConfigured}`.
- `POST /api/config` → atualiza diretórios e modo (`require_user`).
- `GET /api/firebird/config` → `{path, host, port, user, charset, configured, passwordSet}` — **nunca** retorna senha (`require_user`).
- `POST /api/firebird/config` → salva config + chama `apply_to_env` (`require_admin`). Body: `{path, host, port, user, charset, password?}`. Senha omitida = mantém atual; vazia = limpa.
- `POST /api/firebird/test` → testa conexão com config salva ou payload ad-hoc (`require_admin`). Retorna `{ok: bool, error?, traceId}` (`current_trace_id()` injetado).
- `POST /api/process` → upload + parse + cache de preview. Legado, gera XLSX sem o portão de preço: com a trava de preço ativa (`observando`/`ligado`) no ambiente da request responde **409** ("Com a trava de preço ativa, use a revisão do pedido para exportar.").
- `POST /api/imported/{id}/export-xlsx` → gera XLSX do pedido `parsed` **sem** tocar Firebird (`require_user`). Mantém `portal_status='parsed'`. Retorna `{entry_id, output_files, portal_status}`. Usado quando `EXPORT_MODE='xlsx'`. **Também dispara `push_new_order` pro FlowPCP** (gated por `flowpcp_enabled` do ambiente; best-effort; o Flow deduplica por `externalId` — re-export não duplica; audita `flowpcp_push {ok}`).
- `POST /api/batch/export-xlsx` → versão lote do anterior (mesmo limite 1..100).
- `GET /api/download?path=` → download xlsx (`require_user`). Só sufixo `.xlsx`
  E dentro do `OUTPUT_DIR` legado ou do `output_dir` de um ambiente ATIVO (403
  fora — mesmo padrão de confinamento de `baixar_arquivo_original`).
- `GET /api/imported/{id}/arquivo-original` → cópia exata do arquivo recebido, antes do
  parse (`require_user`). Serve só de dentro de `<APP_DATA_DIR>/recebidos/` (403 fora);
  404 amigável em pedido anterior à guarda. Nome do download = `source_filename`.
- `POST /api/admin/environments/{env_id}/flowpcp/sync-materiais?apply=` → carga de material Fire → Flow (`require_admin`). Padrão dryRun; `apply=true` exige `materiais.v1` do Flow (422 antes de enviar se faltar; 422 também com `PORTAL_FLOWPCP_MATERIAIS` desligado; 409 se o FlowPCP não está habilitado no ambiente ou se o Flow já está gravando; 502 em falha). Sem o interruptor e sem apply, devolve `{local_only, itens, extraido_em}`. Ver `modules/flowpcp.md` §4.
- `PATCH /api/admin/environments/{env_id}/preco-trava` → troca o interruptor da trava de preço (`require_admin`). Body `{modo: desligado|observando|ligado, preco_fonte_slug?: str|null}` (422 fora disso ou fonte inválida — fonte é gravada antes do modo, então 422 não muda nada; ausente = não muda; 404 ambiente inexistente). `ligado` responde **409** enquanto a tabela de preço da empresa nunca sincronizou com o Flow (`precos_repo.ultima_sync_ok` do db do ambiente = `None`); `observando`/`desligado` sempre passam. Ver `modules/erp.md` §Trava de preço por rede.
- `POST /api/admin/environments/{env_id}/preco-trava/desbloquear` → devolve à fila de envio ao Flow as revisões da trava com `bloqueio_local` (413, 400 `payload_invalido`, corpo inválido aqui) do ambiente (`require_admin`; 404 ambiente inexistente). Resposta `{desbloqueadas: N}`; loga `env=` e `por=` (e-mail do admin). O `GET /api/admin/environments/{env_id}` traz `preco_bloqueadas_local` (int; `0` sem abrir nada se o SQLite do ambiente ainda não existe — o GET não o cria; `null` se o banco não respondeu) para o cartão "Trava de preço" mostrar a contagem e o botão "Reenviar bloqueados" (só com N > 0).
- `GET /api/admin/environments/{env_id}/flowpcp/cadastro-volta` → livro da volta Flow → Fire (`require_admin`, só leitura do SQLite do ambiente): `{modo, por_estado, ultimos, ultimo_ciclo, thread_desligada, p0_ok, inativar_ligado, sem_volta, n_orfaos, n_incertos, orfaos, incertos, n_aguardando_recuperacao, aguardando_recuperacao, sem_volta_itens, n_conflitos_codigo, conflitos_codigo}` (50 últimos, `plano_json` truncado em 2 KB com `plano_truncado`; `ultimo_ciclo` = `{em, resumo}` do último ciclo deste processo ou `null`, com `aguardando_modo` nos modos de gravação; `thread_desligada` = `PORTAL_FLOWPCP_ENVIO=0`; `p0_ok`/`inativar_ligado` = os dois portões do slug; as listas `orfaos`, `incertos`, `sem_volta_itens` e `aguardando_recuperacao` trazem até 50 linhas com colunas permitidas, nunca `corpo_json`/`plano_json`/nota; `incertos` inclui `transitorio` com SEQ já passado de `IDADE_MINIMA_RECUPERACAO`, e os mais novos vão em `aguardando_recuperacao`; `conflitos_codigo` são os `superado` com alerta `conflito_codigo`, um `atualizar`/`inativar` gravado no Fire cujo código o Flow recusou). A tela do ambiente (`admin-ambiente-edit.html`) tem o bloco da volta com contadores de Órfãos, Incertos, Sem volta e Conflito de código, o contador "Aguardando outro modo" (some quando o Flow não respondeu a contagem) e a tabela dos itens por grupo (Órfão, Incerto, Sem volta, Aguardando recuperação, Conflito de código). A tela só mostra: quem resolve incerto e órfão é `tools/cadastro_volta_operar.py`. Ver `modules/flowpcp.md` §5.
- `GET /api/fs?path=` → listagem de pastas (usado pelo browser de `/configuracoes/diretorios`).
- `GET /api/clientes/search?q=&limit=` → busca em `CADASTRO` (razão social ou
  CNPJ). Min 2 chars; clamp `limit` em [1, 50]. 503 se Fire não configurado.
  Requer auth (`require_user`). Usado pelo picker manual de cliente
  (CLIENT_NOT_FOUND recovery).
- `POST /api/imported/{id}/override-cliente` body `{cliente_codigo, reason?}` →
  aplica seleção manual ao pedido (sidecar em `imports.cliente_override_*`).
  Requer auth. Só permitido em `portal_status='parsed'`. Logs em `audit_log`
  (`cliente_override_selected`) com `user_email`/`user_id` do autenticado.
- `POST /api/imported/{id}/ack-sem-preco` → operador confirma itens sem preço cadastrado no Fire (`require_user`).
  Body vazio. Pre: `portal_status='parsed'`. Re-roda check, persiste lista
  em `imports.sem_preco_ack_*`, audit `sem_preco_acknowledged`. 503 se Fire offline.
- `GET /api/produtos/search?q=&desc=&code=&ean_item=&limit=` → busca na cópia
  local do catálogo (`catalogo_fire`, zero Firebird — funciona offline; cada item
  devolve também `fire_codprod_altern`). Requer
  auth; clamp `limit` em [1, 50]. `q` é **opcional**: com `q` ≥ 2 chars popula
  `results` (busca por nome/código/EAN). As dicas do item do pedido (`desc`,
  `code`, `ean_item`) — quando ao menos uma vier — populam `suggestions` (top 5
  ranqueados por `app/erp/product_ranking.rank_candidates`: EAN parcial > Jaccard
  descrição×nome > código contido). O picker de de-para abre chamando só com as
  dicas (sem `q`) pra já mostrar candidatos. 400 só quando `q` < 2 **e** sem
  dicas. Itens de `results`/`suggestions`: `{fire_produto_id, fire_codigo,
  fire_ean, fire_codprod_altern, fire_nome, score?}`.
- `POST /api/imported/{id}/reconferir` (`require_user`, `env_do_pedido`) — só
  pedido `parsed` (senão **409**): roda `check_order(order, env, escolhas=escolhas_do_import(entry))`,
  grava `check_json` (e `check_preco` da fonte, se houver) quando `available`, e
  devolve `{entry_id, check, produto_chave_modo, produto_trava}`. É o botão
  "Recarregar"/"Conferir de novo" da tela. Fire fora: devolve o check indisponível e
  mantém o salvo. `produto_trava` do preview com check onde nenhum item tem
  `produto` (gravado antes do pacote da chave exata), fora de `desligado`, é
  `{bloqueado: false, reason: "check_antigo"}` — a tela pede "Conferir de novo";
  o portão do envio segue fechado (confere ao vivo).
- `POST /api/imported/{id}/vincular-produto` body
  `{item_index, fire_produto_id, substituir=false}` → vínculo v2
  (`produto_vinculo_v2`, `produto_vinculo_repo.vincular`) da chave exata do item
  (`codigo` e/ou `ean`, normalizadas) ao SEQ, no escopo
  `escopo_de_gravacao(cnpj_do_item(order, i, cnpj_real=...))` (`rede:` ou `raiz:`).
  Pedido intercompany (`resolucao_para(order, slug)` não é None) só grava com o
  cliente real resolvido (`cnpj_real`): `erro_conexao` → **503** "Não consegui
  confirmar o cliente real…"; qualquer outro motivo (sem_chave, nao_encontrado…) →
  **422** "Não sei qual é o cliente real deste pedido (pedido da revenda)…". Nunca
  grava no escopo da revenda (R11); a leitura do check não muda. Sem escopo → **422** "Este pedido não tem CNPJ
  do cliente…". Chave já vigente em outro SEQ e `substituir` falso → **409**
  `{"conflito": {chave_tipo, chave_valor, atual: {fire_seq, fire_descricao,
  criado_por, criado_em}}, "detail"}`; as duas chaves são conferidas antes de
  gravar (sem gravação pela metade). `fire_codigo` do v2 = `codprod_altern` do
  catálogo local; `fire_descricao` = `nome`. Enquanto o `produto_chave_modo` não é
  `ligado` (`_trava_produto_ligada`), grava também o v1 (`produto_depara`) para o
  check legado — exceto em pedido intercompany (R13: o v1 é chaveado pela revenda);
  o audit registra `v1: true|false`. Audit `produto_vinculo_criado` ou `produto_vinculo_trocado`
  (escopo, `chaves[{chave_tipo, chave_valor, fire_seq_antigo, fire_seq_novo}]`,
  `vinculo_ids`, pedido, usuário). Re-roda `check_order` deste pedido e, se ele veio
  `available` (com as escolhas pontuais do import, que o check regravado mantém), de até 50 pedidos `parsed` do ambiente (mais antigos primeiro, R12)
  cujo check guardado tem item na mesma chave fora de `ok` e cujo escopo de busca
  inclui o escopo gravado. Resposta:
  `{entry_id, vinculos: [{id, env, escopo, chave_tipo, chave_valor}], check,
  produto_trava, rechecados: [import_id…]}` (`produto_trava` = a mesma decisão do
  preview sobre o check novo; a tela troca a dela por esta). `_produto_trava_preview`
  falha fechado: exceção vira `{bloqueado: true, reason: "fire_indisponivel"}` + log. Só em `portal_status='parsed'`. 422 se o produto não
  existir no catálogo local (ou SEQ não numérico) ou item sem código nem EAN.
  **Escolha pontual (R18):** item em `chave_repetida` no check guardado, ou já com
  fonte `escolha_pontual`, ou cuja chave se repete no pedido com descrições
  diferentes em QUALQUER estado (`_item_em_chave_repetida`, só `chaves_repetidas`,
  sem Fire): NÃO grava v1 nem v2
  nem dá 409; grava `imports.produto_escolhido_json["<item_index>"] = {fire_seq, por,
  em, descricao, codigo, ean}` (`repo.set_produto_escolha`, um `json_set`; codigo/ean
  = `codprod_altern`/EAN do catálogo local), audita
  `produto_escolha_pontual` (item_index, fire_produto_id, fire_seq_antigo, fire_nome,
  pedido, usuário), re-roda o check com as escolhas do import, regrava o check se
  `available` e responde `{entry_id, pontual: true, vinculos: [], check,
  produto_trava, rechecados: []}` (`_escolher_pontual`). Os outros itens da mesma
  chave continuam pedindo escolha; nenhum outro pedido é rechecado.
- `DELETE /api/produtos/vinculo/{vinculo_id}?env=<slug>` → revoga o vínculo v2 (a
  linha fica, com `revogado_em/por`) no banco da empresa `env` (obrigatório; o id é
  por empresa, o cookie não decide). Empresa inexistente/inativa → 403; id
  inexistente nela → 404. Fora de `ligado`, apaga também o v1 (`produto_depara`)
  com a mesma chave e o mesmo SEQ, do cliente do pedido que criou o vínculo ou de
  CNPJ do mesmo escopo (v1 apontando para outro SEQ fica). Audit
  `produto_vinculo_revogado` (com `env` e `v1_apagados`) no pedido que criou o
  vínculo, se ele ainda existe; senão log direto. Resposta `{"revogado": id}`.
- `GET /api/produtos/a-identificar` → fila: itens com `produto.estado` ≠ `ok` no
  `check_json` guardado dos pedidos `parsed` do ambiente ativo (sem Fire). Resposta
  `{"modo", "itens": [{import_id, pedido, cliente, item_index, codigo, ean, descricao,
  estado, motivo, importado_em}]}`, por `importado_em` crescente. Com
  `export_mode` xlsx/both, o item `ok` que a planilha não aponta ou que o importador
  do Fire trocaria também entra, com `estado` `sem_codigo_fire`/`troca_no_fire` e o
  motivo do portão (`produto_trava.itens_planilha`). `modo` =
  `produto_chave_modo` do ambiente do request (`_produto_chave_modo`: mesma regra do
  portão, sem ambiente vale o mais restrito); a tela só mostra a aba fora de `desligado`.
- `DELETE /api/produtos/depara/{depara_id}` → desfaz um vínculo v1. Log direto
  via `logger.info` (não `audit_log` — `import_id` é `NOT NULL` com FK pra
  `imports`, e o undo não tem um import associado). **M4:** revoga também o v2 gêmeo
  (vigente, mesma chave normalizada, mesmo SEQ, escopo em `escopos_para(client_key)`),
  com audit `produto_vinculo_revogado` (`origem: "depara_v1_removido"`) no pedido que
  criou o v2 (senão log). `client_key` sem CNPJ: v2 fica e a resposta traz `aviso`.
  Resposta `{deleted, v2_revogados: [ids], aviso}`.
- Guards de preço em `_send_one_to_fire` / `_export_one_xlsx`: re-roda
  `check_order` + `is_blocking`; bloqueia 409 com audit `send_to_fire_blocked` /
  `xlsx_export_blocked` quando há mismatch / no_order_price / no_price_unacked.
  Fire offline = best-effort, segue.
- **Ponte FlowPCP (Modelo B/OVERLAY):** após `SEND_TO_FIRE_SUCCEEDED` (dentro do
  `with_trace_id`), `_send_one_to_fire` chama `push_new_order(order, import_id,
  slug)` (`app/integrations/flowpcp/hook.py`) — notifica o FlowPCP do pedido novo
  em paralelo ao Fire. Gated por `flowpcp_config_for_slug(slug)` (config
  per-ambiente em `environments`, `flowpcp_enabled=1`); sem env ativo → no-op.
  Best-effort: erro nunca derruba o send-to-fire (falha vira outbox `target=flowpcp`
  + retry no worker). Vale para single e batch (ambos passam por `_send_one_to_fire`).
  CLI `main.py` fica fora (batch legado, sem contexto multi-ambiente).
  - **Push é enqueue-only (sem HTTP no request path):** `push_new_order` →
    `FlowPCPExporter.enqueue` **enfileira** direto no outbox (`target=flowpcp`) e
    retorna; o `drain_outbox` entrega e faz retry. Não há mais tentativa HTTP
    inline (antes esperava até 30s no timeout do Flow, segurando a UI). Idempotente
    por `send-{import_id}` (`OutboxDuplicateError` = no-op).
  - **Identidade de-para no XLS (e no push):** antes de `ERPExporter().export`,
    `_export_one_xlsx` chama `app/erp/depara_apply.apply(order, conn)` que reescreve
    o item vinculado com a identidade do Fire. Como o `push_new_order` compartilha o
    mesmo `order`, o FlowPCP também recebe a identidade Fire dos itens vinculados
    (desejável: o catálogo do Flow é Fire-synced). Só afeta itens com vínculo.
    **Exceção em `produto_chave_modo` `ligado` (I2):** `_pedido_para_o_flow` tira uma
    cópia do pedido ANTES do `aplicar` (depois do preço aprovado) e é ela que vai ao
    `push_new_order` — o Flow recebe código/EAN do cliente, não os do produto decidido,
    até consumir `erpProdutoId` (Task 9). Fora de `ligado`, como acima.

## Tela: produto por chave exata (`index.html`)
Os 3 payloads de preview (`preview_file`, `preview_pending`, `rehydrate_preview`)
trazem `produto_chave_modo` (`_produto_chave_modo(request_env)`) e `produto_trava`
(`_produto_trava_preview`: `produto_trava.avaliar` sobre o check guardado, com o
`export_mode` do envio → `{bloqueado, reason, export_mode, itens}`). A tela espelha
essa decisão (estados de planilha por item e o bloqueio do rodapé), não refaz a regra. Em `desligado` a
tela é a de sempre (coluna "Fire" com ✓/✗, picker com busca no topo e confirmar
direto). Em `observando`/`ligado`, com `check.items[i].produto` (índice = posição do
item):
- `produtoBadge(produto, {acaoIdx})`: rótulos fixos (`ok` "Produto confirmado",
  `a_identificar` "Escolher produto", `ambiguo` "Mais de um produto com essa chave",
  `produto_bloqueado` "Produto bloqueado", `vinculo_invalido` "Vínculo inválido",
  `chaves_discordam` "Código e EAN apontam para produtos diferentes",
  `chave_repetida` "Código repetido no pedido" (≡, tom de pendência),
  `fire_indisponivel` "Fire sem resposta"; o `ok` que `produto_trava.itens` lista vira
  "Fire grava outro produto" ou "Sem código para a planilha", em tom de aviso —
  `produtoEstadoTela`), ícone por
  forma + cor, motivo logo abaixo. Linha travada ganha faixa à esquerda. Banner
  (`produtoContagem`: pendentes, trocas e Fire sem resposta contados à parte, plural
  de verdade) e rodapé resumem; Fire sem resposta tem banner próprio com "Recarregar" (traz o que está salvo;
  quem reconfere no Fire é o envio). Em
  `ligado` o botão de envio fica desabilitado com o motivo (o backend decide, a tela
  só antecipa). Telefone: coluna Produto presa à direita, botões de ação com 44px.
- Picker (`openProductPicker`): 3 seções na ordem "Candidatos exatos"
  (`produto.candidatos`, bloqueado não selecionável), "Sugestões pela descrição"
  (marca "sugestão", sem pré-seleção) e "Buscar no catálogo" (sem repetir quem já
  está acima). Código mostrado = `fire_codprod_altern`; nome nunca trunca; cada linha
  traz chips "≠ tamanho/pares/cor" contra a linha do cliente. Nada vincula sozinho.
- Confirmação lado a lado (`ppMostrarConfirmacao`): linha do cliente × produto
  escolhido; `divergenciasAtributos(descCliente, descProduto)` marca tamanho, pares e
  cor divergentes (regras da auditoria de 19/10). Só alerta: com divergência o botão
  vira "Vincular mesmo assim" (secundário, borda de aviso) e o foco começa em
  "Voltar". Quem grava é o `#ppVincularBtn`, que nasce desabilitado por 400 ms (duplo
  clique no "Confirmar" não pula a confirmação). Erro do vínculo aparece em
  `#ppConfErro`, dentro da confirmação. Linha em `chave_repetida` ou com fonte
  `escolha_pontual` (`_ppPontual`): a nota diz "Esta escolha vale só para esta linha
  deste pedido." e o botão "Usar nesta linha"/"Usar mesmo assim"; o `ok` por
  escolha pontual mostra "escolhido para esta linha" e o botão "Trocar escolha".
- 409 `conflito`: painel `#ppConflito` na página ("O código X de CLIENTE já aponta
  para…", com as linhas Cliente pediu / Vínculo atual / Você escolheu e a divergência
  marcada); "Manter" é o primário e fecha sem gravar, "Trocar vínculo" reenvia com
  `substituir: true`. Vale também em
  `desligado` (sem ele, trocar vínculo ficaria impossível).
- Aba "Itens a identificar" (`#tab-identificar`, `loadFilaIdentificar`) lê
  `GET /api/produtos/a-identificar`; cada linha chama
  `openImportDetail(id, {itemIndex})`, que rola até o item e a coluna do produto e
  o destaca até o foco sair da linha. Itens com Fire sem resposta vão para o fim, no
  grupo "Aguardando o Fire".
- Teste: `tests/test_web_static_produto.py` (textos/ids fixos no HTML servido e o
  `produto_chave_modo` dos payloads).

## Roteamento intercompany (wiring web)

Domínio completo (degraus, interruptor, tabelas) em
[`modules/routing.md`](routing.md). Aqui só o que muda nas rotas de `web`.

- `GET /admin/roteamento` → `admin-roteamento.html` (interruptor + taxa de
  acerto). Exige login (`require_user`); a troca do modo em si é
  admin-only, enforced na API, não na página.
- `GET /api/roteamento/modo` (`require_user`) → `{modo, modos}`.
- `PUT /api/roteamento/modo` (`require_admin`) body `{modo}` → grava via
  `roteamento_repo.set_modo`, 400 se fora de `MODOS`.
- `GET /api/roteamento/taxa?dias=` (`require_user`) → taxa de acerto da
  janela + `divergencias` (até 50, de `roteamento_sombra`) + `pendencias`
  (até 50, de `roteamento_pendencia`). `dias` é clampado em `[1, 3650]`
  dentro de `roteamento_repo.taxa` — protege contra `OverflowError` do
  `timedelta` com valor absurdo e contra janela no futuro com `dias`
  negativo.
- `POST /api/env/clear` (`require_user`) → remove o cookie `portal_env`
  (`HttpOnly`, por isso o clear precisa ser uma rota — JS não apaga
  sozinho). Só faz sentido com o roteamento `ligado`, onde não ter empresa
  escolhida é um estado válido ("todas as empresas"); nos outros modos a UI
  não oferece a opção.

- `GET /api/roteamento/regra` (`require_user`) → política, redes e chaves.
  `PUT /api/roteamento/regra` (`require_admin`) grava `env_sp`, `env_fora_sp`,
  `ativa` (400 se a empresa não é ambiente ativo).
- `POST /api/roteamento/regra/redes`, `PUT|DELETE
  /api/roteamento/regra/redes/{rede_id}` (`require_admin`) → redes fixas e suas
  chaves (`DELETE` com a regra ligada = 400).
- `GET /api/roteamento/decisoes?import_id=` (`require_user`) → rastro
  `roteamento_decisao` do pedido.
- `POST /api/imported/{import_id}/mover-empresa` (`require_user`) body
  `{destino}` → move pedido em revisão para a outra empresa (409 em
  `desligado` — salvo para concluir um mover interrompido —, pedido não
  movível com `motivo`, pedido em duas origens ou banco ocupado; 404/422 para
  pedido ou destino inexistente). `GET` do detalhe do pedido ganha `env_slug`,
  `roteamento_modo`, `pode_mover` e `motivo_nao_move` (em `desligado`:
  `pode_mover=false`, sem ler a guarda).

`POST /api/commit` (fluxo normal, sem mudança de rota) ganhou o wiring do
roteamento: usa o `(modo, decisao)` que o preview guardou no `PreviewEntry`
(`_roteamento_para_preview(order, entry=...)`; 409 se o modo mudou desde o
preview; sem decisão guardada, `_decidir_ambiente`), decide o
ambiente de gravação via `_resolver_env_alvo` (409 pede escolha quando
`ligado` não resolve e o operador não mandou `environment_slug`; 412 se o
ambiente resolvido não existe mais ou está inativo). Em `observando` grava
`roteamento_sombra` (nunca em `ligado` — sombra é "o que teria feito", não
"o que fez"); em `ligado` sem resposta, a escolha do operador vira memória
(`decisao_ambiente_repo.lembrar`). O evento de audit `imported_to_portal`
carrega `{degrau, env_slug, explicacao}` sob a chave `roteamento` (`None`
em `desligado`) — é o registro de POR QUE o pedido foi para aquela empresa,
não só o `environment_id` (achado 3 da revisão final de branch).

`GET /api/imported` (`require_user` — faltava antes de um fix de revisão;
sem ele a rota cross-env respondia 200 pra chamador anônimo em `ligado`
sem cookie) soma as empresas — lista, `total` e chips juntos, via
`repo.list_imports_all_envs` / `count_imports_all_envs` /
`count_by_portal_status_all_envs` — quando `roteamento_modo == 'ligado'` **e**
não há ambiente no cookie. Com cookie presente, mesmo em `ligado`, o cookie
volta a ser filtro (só aquela empresa). Fora de `ligado`, comportamento
idêntico ao anterior à feature. Cada linha da listagem somada carrega o
selo da empresa (`environment_slug`/`environment_name`).

`GET /` deixa de redirecionar para `/selecionar-ambiente` quando
`roteamento_modo == 'ligado'` — o ambiente é propriedade do pedido, não da
sessão, e o passo de seleção deixa de fazer sentido. `current_environment`
(dependency) muda a mensagem do 412 de "Selecione um ambiente" para "Esta
ação é de uma empresa específica — abra o pedido para agir nele" — mais
honesta fora de `ligado` também (a antiga sugeria um passo que nem sempre
existe).

### Fluxo da pasta de entrada (empresa DECLARADA, não derivada)

Diferente do resto do wiring de roteamento: `import_id` é único globalmente,
mas **nome de arquivo não é** — dois arquivos podem ter o mesmo nome nas
pastas de empresas diferentes. Por isso a empresa aqui não é derivada do
pedido (não existe pedido ainda); é **declarada** pelo cliente via `env_slug`,
resolvido por `_env_da_pasta(request, env_slug)` (`app/web/server.py`):

- Fora de `'ligado'`, ou com cookie `portal_env` presente mesmo em `'ligado'`,
  o cookie manda e `env_slug` é ignorado — comportamento idêntico ao anterior
  a esta task.
- Em `'ligado'` sem cookie: `env_slug` ausente → 400 nomeando o campo;
  não corresponde a empresa ATIVA (`environments_repo.list_active()`) → 404.
  Nunca um default.

`GET /api/pending` (agora `require_user` — faltava antes; a pasta de entrada
respondia 200 pra chamador anônimo) soma as pastas de todas as empresas
ativas nesse caso, e cada item ganha `env_slug`/`env_name` (mesmo selo de
`/api/imported` somado). **Mudança de shape:** `watchDir` no topo da resposta
vira `null` (sem valor único possível quando há mais de uma pasta) e `exists`
passa a significar "pelo menos uma pasta existe" — a UI usa o selo por item
em vez do rótulo do topo (`renderFileRows`/`renderPending` em `index.html`
tratam `watchDir === null` como o caso somado). Fora de `'ligado'`, ou com
cookie, o shape é o de sempre (`watchDir` da empresa/legado, sem os campos
novos por item).

`POST /api/import`, `/api/reimport` e `/api/preview-pending` ganham
`env_slug: str | None = None` no corpo. Quando `_env_da_pasta` resolve uma
empresa (só acontece em `'ligado'` sem cookie), o handler troca o cfg pelo
de `_cfg_para_env(env)` (pastas DESTA empresa, não do cookie) e envolve todo
o trabalho — inclusive `_guardar_original` e `_append_log`, que resolvem a
DB pelo contextvar, não por `cfg` — em `env_context.active_env(env["id"],
env["slug"])`. Fábrica `_persist_ctx(env)`, não uma instância guardada: um
context manager de `@contextmanager` só entra/sai uma vez, e `reimport_file`
precisa dele nos dois ramos (sucesso e erro).

## Lote semanal intercompany (wiring web)

Rotas em `app/web/routes_lotes.py`; fechamento em `app/consolidators/fechamento`.

- `GET /lotes` → `lotes.html`. Sessão **válida** exigida (`current_user`, não
  só cookie presente); sem ela, redirect para `/login`. Item "Lotes" na seção
  Geral do `shell.js` (operador, não admin).
- `GET /api/lotes` (`require_user`) → `config`, `fator_vigente`,
  `proximo_fator`, `aberto`, `pendentes` (`aguardando_fator`), `fora_do_lote`,
  `recentes` (traz `lote_fiscal_ok`, `erro_motivo` e `espelho_existe`: a linha
  do espelho está no ambiente espelho; `pendentes` também trazem o campo).
- `GET /api/lotes/{id}` (`require_user`) → lote + `pernas` + `previa`.
- `PUT /api/lotes/config` e `POST /api/lotes/fator` (`require_admin`; o fator
  global vale da próxima janela, devolve `vigente_desde`).
- `POST /api/lotes/{id}/fator` e `POST /api/lotes/{id}/fechar` (`require_user`).
- A tela recebe o fator em **%** e manda o decimal como string (`"7,5"` →
  `"0.075"`); a prévia ao vivo refaz `preco_espelho` em BigInt (4 casas, depois
  2, `ROUND_HALF_UP`) para bater centavo a centavo com o `exemplo` da API.
- `PUT /api/lotes/config`: origem = espelho → 422; trocar `janela` com lote
  aberto que tem pernas → 409. `POST /api/lotes/{id}/fator` só em
  `aguardando_fator` (senão 409); `fechar` sem fator ou sem pernas → 409.
- `fora_do_lote` (em `GET /api/lotes`): pedidos da origem que chegaram ao Fire
  na janela sem virar perna (digitados no Fire/Excel, `found_in_fire`) — não
  serão refaturados.

Ganchos no envio (`_send_one_to_fire`; detalhe em `modules/consolidators.md`):

- Depois do `check_order` + `is_blocking` de sempre,
  `integracao.preparar_envio_espelho`: `None` = não é espelho; `BloqueioEspelho`
  → audita `send_to_fire_blocked` e devolve **409** com `reason` =
  `ambiente_errado` | `lote_status_<status>` | `lote_invalido` |
  `lote_alterado` | `reserva_falhou`.
- Espelho: sem XLSX mesmo em `EXPORT_MODE=both`; insert com
  `obs=espelho.obs`; `skipped` → `abortar_envio_espelho`; exceção antes de o Fire
  aceitar → `guardar_reserva` cancela a reserva; sucesso →
  `concluir_envio_espelho` **antes** de `update_fire_metadata`; não chama
  `push_new_order` (`/recebimento`).
- Pedido comum: `integracao.anexar_perna_de_origem` logo depois da transição
  `SEND_TO_FIRE_SUCCEEDED` (nunca levanta).
- `_export_one_xlsx` recusa o espelho com 409 `lote_espelho` ("vai ao Fire só
  pelo cadastro direto").

## Segurança (não relaxar)
- Whitelist de extensão: `.pdf`, `.xls`, `.xlsx`.
- Limite de upload: 50 MB.
- `/api/download` exige sessão (`require_user`), aceita SOMENTE `.xlsx` e
  confina o caminho resolvido ao `OUTPUT_DIR` legado + `output_dir` dos
  ambientes ativos (403 fora das raízes).
- `POST /api/auth/login` — rate-limit 10 req/15 min/IP via token bucket SQLite.
  Retorna 429 + `Retry-After: 900` quando esgotado.
  Env `RATE_LIMIT_ENABLED=false` desativa (dev/test).
- `GET /api/pending` ganhou `require_user` (task "ambiente é propriedade do
  pedido" — faltava desde sempre; a rota nunca teve dependency de auth
  nenhuma, então um chamador anônimo via a estrutura de pastas/arquivos
  pendentes de todas as empresas em `ligado` sem cookie). `/api/import`,
  `/api/reimport` e `/api/preview-pending` já exigiam `require_user`.

## Testes
- `tests/test_web_server.py` — inclui o push FlowPCP no send-to-fire
  (`test_send_to_fire_pushes_to_flowpcp_for_env_with_slug` / `_skips_flowpcp_without_env`)
  e as rotas de de-para de produto (`test_produtos_search_local`,
  `test_vincular_produto_persiste`, `test_vincular_rejects_wrong_status`,
  `test_delete_depara_desfaz`).
- `tests/test_flowpcp_hook.py` — `push_new_order` (gating MM + best-effort).
- `tests/test_preview_cache.py`
- `tests/test_firebird_config_api.py` — endpoints `/api/firebird/*`, redirect legacy, gating por role.
- `tests/test_routing_wiring.py` — wiring do roteamento em `/api/commit`, os três modos.
- `tests/test_routing_modo.py` — `/api/roteamento/modo|taxa`, `/api/env/clear`, gate de `/` e cross-env de `/api/imported`.
- `tests/test_env_do_pedido.py` — `env_do_pedido`/`env_do_import_id` (resolver + gate), inclusive
  o requisito de `async def` (contextvar em threadpool).
- `tests/test_rotas_por_pedido_cross_env.py` — as 10 rotas por-pedido e o lote (`/api/batch/*`)
  agrupado por empresa em `'ligado'` sem cookie. Inclui a prova nas rotas de **escrita**
  (`export-xlsx`, `send-to-fire`): as quatro amarrações — Firebird/check de preço, pasta de
  saída, slug do Flow e SQLite — são a empresa do pedido, com cookie de outra empresa e sem
  cookie nenhum. É o teste que faltava quando as rotas por-pedido amarravam três empresas
  diferentes de uma vez.
- `tests/test_pasta_cross_env.py` — `GET /api/pending` somado com selo por item; `env_slug`
  obrigatório (400) e validado contra empresa ativa (404) nas três rotas de ação
  (`/api/import`, `/api/reimport`, `/api/preview-pending`); prova de que o `env_slug`
  declarado lê a pasta certa mesmo com nome de arquivo repetido entre empresas.
- Comando: `.venv/bin/pytest tests/test_web_server.py tests/test_preview_cache.py tests/test_firebird_config_api.py tests/test_routing_wiring.py tests/test_routing_modo.py tests/test_env_do_pedido.py tests/test_rotas_por_pedido_cross_env.py tests/test_pasta_cross_env.py -v`

## EXPORT_MODE=db|both não grava mais no Fire pela importação

`/api/import`, `/api/reimport` e `main.py` **nunca** gravam no Fire: `_run_exporters`
devolve `db_result={"skipped": True, "skip_reason": "USE_PREVIEW"}` e o `main.py` (CLI)
recusa `db|both` com `sys.exit`. A gravação só ocorre no preview → enviar
(`_send_one_to_fire`), onde passa por `preco_trava.decidir_envio` (ver `modules/erp.md`,
"Trava de preço por rede"). Com a trava `observando`/`ligado`, a importação **também não
gera XLSX** (resposta traz `xlsx_adiado: "trava_de_preco"`); o XLSX sai pelo preview →
exportar. Com `produto_chave_modo` em `ligado` idem, com `xlsx_adiado: "produto_a_identificar"`
(precede o de preço), e `/api/process` responde 409. O payload do preview ganha `preco`.

## Reatividade de config (exportMode)
O botão de ação principal (`#pvCommitBtn` no preview e `#batchSendBtn` no log)
é dirigido por `cfg.exportMode` — fonte: `GET /api/config`. Mapeamento:
`xlsx` → "Gerar XLS" (chama `/api/imported/{id}/export-xlsx`); `db` → "Cadastrar
no Fire" (`/send-to-fire`); `both` → "Cadastrar no Fire + XLS" (`/send-to-fire`,
backend gera XLSX adicionalmente).

Quando o modo é alterado em `/configuracoes/diretorios`, a aba Pedidos atualiza
label/handler **sem reload** via `BroadcastChannel('app-config')` com payload
`{type:'config-changed', exportMode}`. Fallback p/ navegadores sem
BroadcastChannel: chave `app:config:bumped` em `localStorage` + `storage`
event listener.

## Arquivo original: guarda de 100% do que entra

Todo recebimento — `POST /api/preview` (upload), `POST /api/preview-pending` (pasta
vigiada), `POST /api/import` (lote) e o legado `POST /api/process` — chama
`_guardar_original(raw, nome)` **antes do parse**. A cópia vai para
`<APP_DATA_DIR>/recebidos/<slug>/<AAAA>/<MM>/<AAAAMMDD-HHMMSS>_<sha12>_<nome>.<ext>`
(`app/ingestion/arquivo_recebido.py`), nunca é sobrescrita e a retenção não a toca.
Parse que falha (422), preview descartado ou expirado: a cópia já existe.

- **Falha na gravação bloqueia a operação** (HTTP 500 com mensagem clara). A garantia
  pedida é 100%; best-effort silencioso viraria 97% sem ninguém saber.
- O caminho + sha viajam no `PreviewEntry` (`original_path`, `file_sha256`) e o
  `commit` grava em `imports.original_path` / `imports.file_sha256`.
- O `move` para `<watch_dir>/Pedidos importados` continua igual — é a cópia do cliente;
  a nossa é a segunda, imutável.
- `GET /api/imported/{id}/preview` devolve `arquivo_original: {disponivel, nome}`; o
  modal mostra **Baixar arquivo original** quando disponível.
- Motivação: caso AF127/AF017 (H2S4, 27/07/2026) — dois arquivos com o mesmo nome no
  mesmo dia, o segundo já errado, e nenhuma cópia nossa pra provar de onde veio.

## Armadilhas
- Não cachear bytes do arquivo original (vazamento de memória); só o `Order` parseado.
- Toda mudança de rota: atualizar este arquivo + a página relevante.
- `app_state.db` vive em `<repo_root>/data/` (override: `APP_DATA_DIR`). NÃO depende de `watch_dir` — mudar diretórios via `POST /api/config` não move sessões nem dados operacionais.
- Sidebar gating é client-side (escondemos o item para não-admin no shell.js); a fonte de verdade
  é o backend — `require_admin` em todos os writes. Nunca confie só na UI para gating.
- Páginas em `/configuracoes/*` carregam para qualquer usuário logado (estáticas). Acesso real é
  feito pelas APIs que cada página consome — daí ser admin-only no `POST` do Firebird.
- Assets estáticos não têm hash. Use `?v=1` em `<link>`/`<script>` ao mudar tokens/shell;
  caso contrário, hard-reload no browser.
- `_export_one_xlsx` re-roda `check_order` SEM passar `request_env` (caminho
  legado). Em deploy multi-ambiente isso usa env vars `FB_*`. Follow-up:
  passar env do request quando essa rota também adotar `getattr(request.state, "environment")`.
- `POST /api/import` (lote por nome de arquivo) e `POST /api/reimport` existem no backend
  (com teste) mas **não têm nenhum call site em `index.html`** — `doReimport()` está definida
  mas nada renderiza o botão `id="reimport-..."` que ela espera (grep de `api/import\b` e
  `reimport-` no arquivo inteiro, antes desta task, não achou chamador nenhum). Aceitam
  `env_slug` porque o contrato do backend precisa ser correto independente de UI, mas não há
  hoje um fluxo real que monte o corpo com `env_slug` para elas — só `previewPending` (aba de
  pendentes) tem essa informação disponível (`f.env_slug` da listagem somada).
- Um context manager de `@contextmanager` (como `env_context.active_env(...)`) só entra/sai
  UMA vez. `_persist_ctx(env)` em `server.py` é fábrica (chama `active_env`/`nullcontext` de
  novo a cada `with`), nunca guarde o retorno numa variável pra reusar em dois blocos `with`
  (ex.: ramo de sucesso e ramo de erro) — a segunda entrada estoura.
