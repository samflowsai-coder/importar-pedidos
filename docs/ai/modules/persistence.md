# Módulo: persistence (SQLite log)

## Responsabilidade
Persistência local em SQLite: histórico de pedidos importados (`imports`),
log humano de auditoria (`audit_log`) e log append-only do ciclo de vida
(`order_lifecycle_events`, escrito pela state machine — ver `state.md`).

## Arquivos críticos
- `app/persistence/db.py` — conexão sqlite, schema, migrations idempotentes
  via `_COLUMN_MIGRATIONS`. Tabelas: `imports`, `audit_log`,
  `order_lifecycle_events`.
- `app/persistence/repo.py` — repositório de `imports` + `audit_log`.
  - `insert_import(entry)` — cria/upsert linha de pedido. **Não clobbera**
    colunas SM-owned (`portal_status`, `production_status`, `state_version`,
    `sent_to_fire_at`, `released_at`, `released_by`) nem as colunas de
    cliente override (`cliente_override_*`) em conflito de id.
  - `update_fire_metadata(import_id, fire_codigo, db_result, output_files,
    sent_to_fire_at)` — atualiza só campos auxiliares pós-Fire. Não toca
    status. Use antes de `transition(SEND_TO_FIRE_SUCCEEDED)`.
  - `set_gestor_order_id(import_id, gestor_order_id)` — id externo
    devolvido pelo Gestor de Produção. Use após `POST_TO_GESTOR_SENT`.
  - `set_client_override(import_id, *, codigo, razao, user=None)` —
    persiste seleção manual de cliente (recovery do CLIENT_NOT_FOUND).
    Last-write-wins; `audit_log` mantém histórico de tentativas. `user`
    fica `None` até auth (v5).
  - `append_audit(import_id, event_type, detail)` — log humano (quem
    clicou em quê). Convive com `lifecycle_events`.
- `app/persistence/outbox_repo.py` — fila durável de outbound. Ver
  `modules/gestor.md`. API: `enqueue`, `claim_next`, `mark_sent`,
  `mark_failed`, `find_by_idempotency_key`, `list_for_import`.
- `tools/migrate_log_to_sqlite.py` — migração one-shot do log antigo.

## Schema

### Campos da Fase 1 (state machine)
- `imports.trace_id TEXT` — UUID por pedido, mintado no boundary
  (commit/send-to-fire/webhook). Preservado em upserts via COALESCE.
- `imports.state_version INTEGER NOT NULL DEFAULT 1` — bump em cada
  `transition()` para optimistic concurrency.
- `order_lifecycle_events` — tabela append-only, FK em `imports`.
  Cascade DELETE. Lida exclusivamente por `app.state.transition`.

### Campos da Fase 3 (Gestor de Produção)
- `imports.gestor_order_id TEXT` — id externo devolvido pelo Gestor.
  Setado por `repo.set_gestor_order_id` após `POST_TO_GESTOR_SENT`.
- `outbox` — fila durável de chamadas outbound. `idempotency_key UNIQUE`,
  `status pending|sent|dead`, `attempts`, `next_attempt_at` (backoff),
  `last_error`, `response_json`, `trace_id`. FK em `imports`, cascade
  DELETE. Ver `modules/gestor.md`.

### Campos da Fase 4 (webhooks inbound)
- `imports.apontae_order_id TEXT` — id do Apontaê. Setado em qualquer
  webhook que carregue o campo (idempotente).
- `inbound_idempotency` — dedup de webhooks por
  `(provider, event_id)`. PK composta. Cacheia `response_status` +
  `response_body` para replay devolver bytes idênticos. Ver
  `modules/security.md`.

### Cliente override (CLIENT_NOT_FOUND recovery)
- `imports.cliente_override_codigo INTEGER` — `CADASTRO.CODIGO` escolhido
  pelo usuário no picker. Lido por `_send_one_to_fire` e passado como
  `override_client_id` ao `FirebirdExporter.export`.
- `imports.cliente_override_razao TEXT` — razão social denormalizada
  (evita re-query ao Fire só para mostrar na UI).
- `imports.cliente_override_at TEXT` — ISO timestamp da seleção.
- `imports.file_sha256 TEXT` / `imports.original_path TEXT` — hash e caminho da cópia
  imutável do arquivo recebido (`<APP_DATA_DIR>/recebidos/...`, ver `web.md`, seção
  "Arquivo original"). Preenchidos em todo import desde 2026-09-03; linhas anteriores
  ficam `NULL` (o download responde 404 amigável). `get_import` devolve os dois;
  `list_imports` não (não precisa na listagem).
- `imports.cliente_override_by TEXT` — email do usuário autenticado que
  aplicou (vem de `User.email` via `require_user`). NULL apenas em rows
  legadas anteriores ao auth.
- Mutação só via `repo.set_client_override(...)`; `insert_import` upsert
  NÃO clobbera essas colunas.
- Override **não muda `portal_status`** — não há lifecycle event próprio;
  rastreio operacional fica em `audit_log` com `event_type=cliente_override_selected`.

### Roteamento intercompany (shared db, `app_shared.db`)

Tabelas transversais (`app/persistence/schema_shared.py`) — comportamento
completo em [`modules/routing.md`](routing.md), aqui só o schema:

- `environments.cnpj TEXT` — CNPJ (dígitos) da empresa que o ambiente
  representa; chave do degrau 1 (documento). `NULL` = ambiente fora do
  roteamento automático. Índice único PARCIAL, escopado a `is_active = 1`
  (`idx_environments_cnpj`) — um ambiente desativado não segura o CNPJ pra
  sempre.
- `roteamento_modo` — linha única (`id=1 CHECK`), o interruptor de três
  estados (`desligado|observando|ligado`, `CHECK` no `valor`). Sem linha =
  `'desligado'` (lido em `roteamento_repo.modo()`, não pelo `CHECK`).
- `roteamento_sombra` — uma linha por decisão observada: `import_id`,
  `degrau`, `env_sugerido`, `env_escolhido_pelo_operador`, `bateu`. Só
  gravada em `observando` (nunca em `ligado` — sombra é "o que o roteador
  TERIA feito", não "o que ele fez").
- `roteamento_pendencia` — PK `sha256`; arquivo que o watcher não soube
  rotear em `ligado`. `visto_em`/`visto_vezes` — reavaliado no máximo
  1x/hora (`app/worker/jobs/scan_environments.py`), nunca a cada ciclo de
  30s.
- `regra_faturamento_config` — linha única (`id=1 CHECK`): `env_sp`,
  `env_fora_sp`, `ativa` (nasce 0), `alterado_por`, `alterado_em`. Sem linha =
  regra desligada (`regra_faturamento_repo`).
- `regra_faturamento_rede` — `id`, `nome` UNIQUE, `env_slug`, `alterado_por`,
  `alterado_em`. Semente (só com a tabela vazia e sem linha em
  `regra_faturamento_config`): Centauro, Riachuelo, Kallan,
  Sam's Club, todas em `mm`, `alterado_por='semente'`.
- `regra_faturamento_chave` — PK `(tipo, chave)`, `rede_id` FK (`ON DELETE
  CASCADE`); `tipo` `raiz` (8 dígitos) ou `cnpj` (14). Uma chave pertence a
  uma rede só. Semente: raízes Centauro/Riachuelo/Kallan e 12 CNPJs de CD do
  Sam's Club.
- `roteamento_decisao` — rastro só-INSERT (`roteamento_decisao_repo`):
  `import_id`, `decidido_em`, `modo`, `degrau`, `env_sugerido`,
  `env_escolhido`, `decidido_por`, `motivo`, `acao` (`CHECK`: sugestao_aceita,
  troca_no_preview, escolha_sem_sugestao, movido_apos_importar, automatico,
  observando). Índice `(import_id, decidido_em)`.
- `decisao_ambiente` — PK `cnpj_cliente`; a escolha que um humano já fez
  (degrau 3, memória). `divergiu_em`/`divergiu_de` marcam quando um degrau
  mais forte contradisse depois — nunca reescreve, só marca.

### Cadastro Fire ↔ Flow (db do ambiente, `schema_env.py`)
- `materiais_fire` — PK `erp_produto_id`; cópia local dos materiais (fora do grupo 8),
  substituída a cada sync (`materiais_fire_repo.py`). Colunas: `nome`, `unidade`, `ean`,
  `ativo`, `grupo_codigo`, `ncm`, `tipo_item_sped`, `exige_aprovacao`, `extraido_em`.
- `cadastro_volta` — livro da volta Flow → Fire (`cadastro_volta_repo.py`); PK
  `(cadastro_id, versao)` é o lease. Colunas da Etapa 1: `estado`, `acao`, `tipo`,
  `seq_reservado`, `erro_codigo`, `erro_mensagem`, `plano_json`, `iniciado_em`, `atualizado_em`.
  Colunas da Etapa 3 (aditivas, `COLUMN_MIGRATIONS`): `produto_id` (UUID do produto no Flow; índice
  `idx_cadastro_volta_produto`, base da regra 9b), `modo`, `erp_produto_id` (SEQ gravado),
  `corpo_json` (corpo do `POST .../gravado` guardado para reenvio), `tentativas`,
  `proxima_tentativa_em` (ISO UTC), `alerta`, `resposta_flow` (rótulo curto, até 64), `nota_operador`
  (nota do `fechar-orfao`, até 500; o `erro_mensagem` aparece como erro na tela, por isso a nota
  tem coluna própria). `estado` ganhou `em_andamento`, `gravado_sem_volta`, `erro_a_enviar`,
  `confirmado`, `erro_registrado`, `transitorio`, `liberado`, `orfao`, `superado`, `incerto` e
  `orfao_fechado` (final). As transições valem pela lista branca `TRANSICOES` e pelas portas
  próprias do repo (`liberar_comprovado`, `marcar_erro_a_enviar(rollback_comprovado=)`,
  `retomar_transitorio_com_seq`, `fechar_orfao`, `adotar_manual`, `liberar_manual`); `gravado`
  e o que pode ter comitado no ERP nunca voltam a estado reaproveitável. `listar` (alimenta a
  rota admin) não traz `corpo_json` (a rota trunca o `plano_json`); as listas de atenção da tela usam
  `listar_por_estado`, só com colunas permitidas. `seqs_em_aberto` alimenta a ida do catálogo (`fora_do_envio_volta`)
  e usa o mesmo predicado "pode ter gravado" da 9b (`criar_do_produto`). `duravel(conn)` liga
  `synchronous=FULL` só na conexão do livro (volta e ferramenta do operador); o router segue `NORMAL`.
  Detalhe de estados, recuperação e métricas em `modules/flowpcp.md` §5.

### Trava de preço (db do ambiente, `schema_env.py`)
Cópia local da tabela do Flow no formato do contrato precos v1 (rota a); o Portal só aplica
páginas (`precos_repo.aplicar_pagina`, uma transação por página; `reiniciar` zera tudo). O
bootstrap derruba as tabelas do modelo anterior (`preco_rede`, `preco_rede_cnpj`, `preco_tabela`,
`preco_sync`).
- `preco_rede_v1` — PK `rede_id`; `nome`, `ativa`, `travada` (estado inteiro vindo do Flow).
- `preco_rede_chave_v1` — PK `(rede_id, tipo, chave)`; `tipo` = `raiz` (8 caracteres) | `cnpj`
  (14, vence a raiz); substituídas inteiras quando a rede reaparece.
- `preco_tabela_v1` — PK `preco_id` (append-only; `cancelado` volta pelo mesmo id); `rede_id`
  (NULL = base), `empresa_cnpj`, `erp_produto_id`, `unidade_erp`, `preco_centavos`,
  `vigencia_inicio`, `registrado_em` (texto do Flow, comparado como instante).
- `preco_sync_v1` — linha única: `empresa_cnpj`, `cursor`, `completo_em` (`geradoEm` da última
  página com `temMais: false`; base da regra de 24 h).
- `preco_revisao` — PK `(import_id, revisao)`: revisão do pedido na aprovação de preço
  (contrato precos v1 §4). `conteudo_hash`, `corpo_json` (`aprovacaoEnvioV1` com a revisão),
  `situacao` (`pendente_envio | enviada | recusada_flow | substituida | retirada`),
  `tentada_em` (hora da última tentativa do POST; ordena a fila de envio), `enviada_em`/`resposta_json`, `erro_definitivo` (422),
  `bloqueio_local` (texto do bloqueio: corpo que não pode ir ao Flow; sai da fila de envio e só
  conteúdo novo cria outra revisão), `retirada_em`/`retirada_motivo`/
  `retirada_enviada_em`. Repo: `preco_revisao_repo`.
- `preco_revisao_piso` — PK `import_id`: piso vindo do 409 `revisao_obsoleta` (a próxima
  revisão nasce acima dele).
- `preco_decisao` — PK `decisao_id` (idempotente): decisão do Flow por `item_chave`
  (`resultado`, `preco_aprovado_centavos`, `motivo_recusa`, `decidido_em`) e a `confirmacao`
  a devolver (`aplicada | descartada`, `confirmada_em`).
- `preco_aprovacao` (aprovação por item, modelo anterior) saiu: o bootstrap faz `DROP TABLE IF
  EXISTS` (nunca saiu da branch).
- `imports.check_preco_json` — check de produto que a trava avalia (gravado no `/api/commit` e
  regravado no `vincular-produto`); leva a marca `preco_fonte_slug` da fonte que o gerou, e
  de outra fonte vale como sem avaliação. `repo.get_import` devolve como `check_preco`.
- `imports.produto_escolhido_json` — escolha pontual do produto por linha (R18, chave
  repetida no pedido), só deste import: `{"<item_index>": {fire_seq, por, em, descricao,
  codigo, ean}}`. Valor podre: a leitura devolve `{}` com aviso e a gravação recomeça
  de um objeto novo.
  Grava só `repo.set_produto_escolha` (`json_set`, nunca clobbado pelo upsert);
  `get_import`/`list_imports` devolvem como `produto_escolhido` (`{}` sem escolha).
- `imports.cliente_override_cnpj` — CNPJ do cliente escolhido no override; é o que define a
  rede quando o pedido veio sem CNPJ. Ver `modules/erp.md` ("Trava de preço por rede").

## Testes
- `tests/test_persistence_repo.py` —
  `.venv/bin/pytest tests/test_persistence_repo.py -v`
- `tests/test_state_machine.py` (cobre transition + projection drift) —
  `.venv/bin/pytest tests/test_state_machine.py -v`

## Armadilhas
- Banco é local-only por design. Não promover sem mudar estratégia
  (multi-tenant é v5).
- **Nunca escrever direto em `portal_status`/`production_status`** em
  novos call-sites. Toda mutação passa por `app.state.transition()` — o
  `repo.insert_import` upsert já protege essas colunas, mas o invariante
  precisa ser respeitado também na entrada (criação inicial).
