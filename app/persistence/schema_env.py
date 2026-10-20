"""Schema do banco SQLite por-ambiente (`app_state_<slug>.db`).

Uma DB por ambiente (MM, Nasmar, ...). Hospeda dados financeiro-operacionais
do pedido: importações, audit, lifecycle, outbox. Coluna `environment_id`
em todas as tabelas é defensiva — a DB já é específica de um ambiente, mas
o ID redundante torna trivial detectar bug de wiring.
"""

from __future__ import annotations

TABLES_SQL = """
CREATE TABLE IF NOT EXISTS imports (
    id                       TEXT PRIMARY KEY,
    environment_id           TEXT NOT NULL,
    source_filename          TEXT NOT NULL,
    imported_at              TEXT NOT NULL,
    order_number             TEXT,
    customer_cnpj            TEXT,
    customer_name            TEXT,
    fire_codigo              INTEGER,
    snapshot_json            TEXT,
    check_json               TEXT,
    output_files_json        TEXT,
    db_result_json           TEXT,
    status                   TEXT NOT NULL,
    error                    TEXT,
    portal_status            TEXT NOT NULL DEFAULT 'sent_to_fire',
    sent_to_fire_at          TEXT,
    production_status        TEXT NOT NULL DEFAULT 'none',
    released_at              TEXT,
    released_by              TEXT,
    trace_id                 TEXT,
    state_version            INTEGER NOT NULL DEFAULT 1,
    gestor_order_id          TEXT,
    apontae_order_id         TEXT,
    cliente_override_codigo  INTEGER,
    cliente_override_razao   TEXT,
    cliente_override_at      TEXT,
    cliente_override_by      TEXT,
    cliente_override_cnpj    TEXT,
    fire_status_last_seen    TEXT,
    fire_status_polled_at    TEXT,
    reconciled_at            TEXT,
    file_sha256              TEXT,
    original_path            TEXT,
    sem_preco_ack_by         TEXT,
    sem_preco_ack_at         TEXT,
    sem_preco_ack_items      TEXT,
    check_preco_json         TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    detail_json    TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS order_lifecycle_events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id TEXT NOT NULL,
    import_id      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    source         TEXT NOT NULL,
    payload_json   TEXT,
    trace_id       TEXT,
    occurred_at    TEXT NOT NULL,
    ingested_at    TEXT NOT NULL,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outbox (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id   TEXT NOT NULL,
    import_id        TEXT NOT NULL,
    target           TEXT NOT NULL,
    endpoint         TEXT NOT NULL,
    payload_json     TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'pending',
    attempts         INTEGER NOT NULL DEFAULT 0,
    next_attempt_at  TEXT,
    last_error       TEXT,
    response_json    TEXT,
    trace_id         TEXT,
    created_at       TEXT NOT NULL,
    sent_at          TEXT,
    FOREIGN KEY (import_id) REFERENCES imports(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS flowpcp_decisoes_mapping (
    decisao_id      TEXT PRIMARY KEY,
    pedido_erp      TEXT,
    cliente_cnpj    TEXT,
    acao_executada  TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    reconciliado_em TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em   TEXT NOT NULL DEFAULT (datetime('now')),
    -- SP1 eco (ver COLUMN_MIGRATIONS para bancos que já existiam)
    base             TEXT,
    erp_codigo       INTEGER,
    erp_codigos_json TEXT,
    nova_data        TEXT,
    aplicada_em      TEXT
);

CREATE TABLE IF NOT EXISTS flowpcp_cursor_state (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    last_cursor   TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Cópia local do catálogo do Fire ("manter no importador"). Snapshot
-- substitutivo a cada sync; envio ao Flow é gated por flowpcp_catalogo_push.
CREATE TABLE IF NOT EXISTS catalogo_fire (
    fire_produto_id TEXT PRIMARY KEY,
    codigo          TEXT NOT NULL,
    nome            TEXT NOT NULL,
    unidade         TEXT,
    ean             TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1,
    tipo            TEXT NOT NULL DEFAULT 'simples',
    extraido_em     TEXT NOT NULL
);

-- Cópia local dos materiais do Fire (tudo fora do grupo 8 CONFECCIONADO), spec
-- 2026-10-16-flow-fonte-do-cadastro §6.2. Snapshot substitutivo a cada sync.
CREATE TABLE IF NOT EXISTS materiais_fire (
    erp_produto_id  TEXT PRIMARY KEY,
    nome            TEXT NOT NULL,
    unidade         TEXT,
    ean             TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1,
    grupo_codigo    INTEGER,
    ncm             TEXT,
    tipo_item_sped  INTEGER,
    exige_aprovacao INTEGER NOT NULL DEFAULT 0,
    extraido_em     TEXT NOT NULL
);

-- Livro da volta Flow → Fire (spec 2026-10-16 §6.3). Estados: `simulado`/`erro_definitivo`
-- (simular) e os da gravação (`em_andamento` ... `orfao_fechado`, ver cadastro_volta_repo.ESTADOS).
-- (cadastro_id, versao) é o lease: o mesmo item não é processado duas vezes.
CREATE TABLE IF NOT EXISTS cadastro_volta (
    cadastro_id    TEXT NOT NULL,
    versao         INTEGER NOT NULL,
    estado         TEXT NOT NULL,
    acao           TEXT,
    tipo           TEXT,
    seq_reservado  INTEGER,
    erro_codigo    TEXT,
    erro_mensagem  TEXT,
    plano_json     TEXT,
    iniciado_em    TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (cadastro_id, versao)
);

-- Cópia local dos clientes ativos do Fire ("manter no importador"). Snapshot
-- substitutivo a cada sync; envio ao Flow é gated por flowpcp_clientes_push.
CREATE TABLE IF NOT EXISTS clientes_fire (
    fire_cliente_id TEXT PRIMARY KEY,
    cnpj            TEXT NOT NULL,
    nome            TEXT NOT NULL,
    grupo_codigo    TEXT,
    ativo           INTEGER NOT NULL DEFAULT 1,
    extraido_em     TEXT NOT NULL
);

-- De-para de produto por cliente: a referência do varejista que não casa no
-- Fire vira um vínculo persistente (feito uma vez, lembrado para sempre).
-- Chaveado por client_key: CNPJ (dígitos) quando o header do pedido tem, ou
-- o nome do cliente normalizado quando não tem (ex: Riachuelo — CNPJ real é
-- por loja, não no header). Ver produto_depara_repo.client_key().
CREATE TABLE IF NOT EXISTS produto_depara (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_key      TEXT NOT NULL,
    chave_tipo      TEXT NOT NULL,   -- 'codigo' | 'ean'
    chave_valor     TEXT NOT NULL,   -- normalizado
    fire_produto_id TEXT NOT NULL,
    fire_codigo     TEXT NOT NULL,
    fire_ean        TEXT,
    fire_nome       TEXT NOT NULL,
    criado_em       TEXT NOT NULL,
    criado_por      TEXT,
    UNIQUE (client_key, chave_tipo, chave_valor)
);

-- De-para v2: vínculo da chave EXATA do varejista (código ou EAN) a um produto do
-- Fire (SEQ), por escopo ('rede:<id>' ou 'raiz:<8 dígitos do CNPJ>'). Nunca é
-- apagado: revogar grava revogado_em/revogado_por. Só um vínculo vigente por
-- (escopo, chave) — índice único parcial em INDEXES_SQL.
CREATE TABLE IF NOT EXISTS produto_vinculo_v2 (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    escopo         TEXT NOT NULL,
    chave_tipo     TEXT NOT NULL CHECK (chave_tipo IN ('codigo','ean')),
    chave_valor    TEXT NOT NULL,
    fire_seq       INTEGER NOT NULL,
    fire_codigo    TEXT,
    fire_descricao TEXT NOT NULL,
    origem         TEXT NOT NULL CHECK (origem IN ('manual','migrado_v1')),
    pedido_id      TEXT,
    criado_em      TEXT NOT NULL,
    criado_por     TEXT,
    revogado_em    TEXT,
    revogado_por   TEXT
);

-- ── SP1: transporte de pedidos do ERP para o Flow (docs/ai/modules/erp_eventos.md) ──
-- Nomes pelo papel (P-PROD-08): erp_*, base. Nenhuma tabela aqui tem FK para
-- imports: pedido do ERP existe sem ter passado pelo Portal.

-- Item do ERP ↔ item do pedido no Portal. `indice_original` é a posição do
-- item em `order.items` — a mesma posição no array `itens` do /recebimento,
-- que o Flow grava como `source_id_externo = <import_id>:<posição>`.
-- origem: 'insert' (exportador, modo db) | 'xlsx' (casado pela planilha).
CREATE TABLE IF NOT EXISTS erp_item_vinculo (
    base                   TEXT NOT NULL,
    erp_item_codigo        INTEGER NOT NULL,
    erp_codigo             INTEGER NOT NULL,
    import_id              TEXT NOT NULL,
    indice_original        INTEGER NOT NULL,
    origem                 TEXT NOT NULL,
    produto_produtor       INTEGER,
    produtor_resolvido_em  TEXT,
    criado_em              TEXT NOT NULL,
    PRIMARY KEY (base, erp_item_codigo)
);

-- O que cada XLSX gerado levou, linha a linha. É o que permite casar o
-- pedido que a operadora importou no ERP com as linhas do Portal.
CREATE TABLE IF NOT EXISTS erp_xlsx_linha (
    import_id        TEXT NOT NULL,
    arquivo          TEXT NOT NULL,
    linha            INTEGER NOT NULL,
    indice_original  INTEGER NOT NULL,
    pedido_cliente   TEXT,
    cnpj_cliente     TEXT,
    quantidade       TEXT,
    preco_unitario   TEXT,
    gerado_em        TEXT NOT NULL,
    PRIMARY KEY (import_id, arquivo, linha)
);

-- Último estado visto de cada pedido do ERP (contra ele a vigia calcula as
-- mudanças). situacao: 'aberto' | 'encerrado' | 'excluido'.
CREATE TABLE IF NOT EXISTS erp_pedido_estado (
    base           TEXT NOT NULL,
    erp_codigo     INTEGER NOT NULL,
    situacao       TEXT NOT NULL,
    estado_json    TEXT NOT NULL,
    visto_em       TEXT NOT NULL,
    atualizado_em  TEXT NOT NULL,
    PRIMARY KEY (base, erp_codigo)
);

-- Marca d'água do seq, À PARTE do estado: apagar/recriar erp_pedido_estado
-- não zera nem reaproveita seq (spec SP1 §4.1).
CREATE TABLE IF NOT EXISTS erp_seq_marca (
    base        TEXT NOT NULL,
    erp_codigo  INTEGER NOT NULL,
    ultimo_seq  INTEGER NOT NULL,
    PRIMARY KEY (base, erp_codigo)
);

-- Fila de saída. Payload congelado na gravação: reenvio repete os mesmos
-- bytes de evento. `unidade` agrupa o que não pode ser partido entre lotes.
-- situacao: 'pendente' | 'aceito' | 'duplicado' | 'rejeitado'.
CREATE TABLE IF NOT EXISTS erp_eventos_saida (
    ordem          INTEGER PRIMARY KEY AUTOINCREMENT,
    id             TEXT NOT NULL UNIQUE,
    base           TEXT NOT NULL,
    erp_codigo     INTEGER NOT NULL,
    seq            INTEGER,
    tipo           TEXT NOT NULL,
    unidade        TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    observado_em   TEXT NOT NULL,
    situacao       TEXT NOT NULL DEFAULT 'pendente',
    motivo         TEXT,
    lote_id        TEXT,
    enviado_em     TEXT,
    respondido_em  TEXT,
    criado_em      TEXT NOT NULL
);

-- Saúde do envio por base: espera, bloqueio, sinal de vida.
-- bloqueio: NULL | 'credencial' | 'lote_invalido' | 'unidade_grande' | 'http_<status>'.
CREATE TABLE IF NOT EXISTS erp_envio_estado (
    base                  TEXT PRIMARY KEY,
    proxima_tentativa_em  TEXT,
    falhas_seguidas       INTEGER NOT NULL DEFAULT 0,
    bloqueio              TEXT,
    ultimo_http           INTEGER,
    ultimo_erro           TEXT,
    ultimo_202_em         TEXT,
    ultima_vigia_em       TEXT,
    sinal_pendente        INTEGER NOT NULL DEFAULT 0,
    atualizado_em         TEXT NOT NULL
);

-- Foto noturna: uma por base por dia local; páginas congeladas na criação
-- (totalPaginas constante, reenvio da mesma página = mesmo corpo).
-- situacao: 'enviando' | 'completa' | 'expirada' | 'recusada' | 'nao_criada'
-- (nao_criada = algum pedido fora do contrato; `motivo` diz qual).
CREATE TABLE IF NOT EXISTS erp_foto (
    foto_id         TEXT PRIMARY KEY,
    base            TEXT NOT NULL,
    dia             TEXT NOT NULL,
    corte_em        TEXT NOT NULL,
    total_paginas   INTEGER NOT NULL,
    situacao        TEXT NOT NULL DEFAULT 'enviando',
    criada_em       TEXT NOT NULL,
    concluida_em    TEXT,
    motivo          TEXT,
    UNIQUE (base, dia)
);

CREATE TABLE IF NOT EXISTS erp_foto_pagina (
    foto_id       TEXT NOT NULL,
    pagina        INTEGER NOT NULL,
    payload_json  TEXT NOT NULL,
    enviada_em    TEXT,
    PRIMARY KEY (foto_id, pagina)
);

-- Lote fiscal (emissor; quem produz o lote é a Fase 2 do roteamento,
-- ainda não construída). A reserva existe ANTES do INSERT do pedido
-- consolidado no ERP: enquanto houver reserva, a vigia segura pedido novo
-- da base produtora para intermediário fiscal — o lote_fiscal entra na
-- fila antes do pedido_visto do consolidado.
CREATE TABLE IF NOT EXISTS erp_lote_reserva (
    chave_lote    TEXT PRIMARY KEY,
    base_espelho  TEXT NOT NULL,
    reservado_em  TEXT NOT NULL
);

-- Um item de origem pertence a UM lote (PK).
CREATE TABLE IF NOT EXISTS erp_lote_perna (
    base_origem             TEXT NOT NULL,
    erp_item_codigo_origem  INTEGER NOT NULL,
    erp_codigo_origem       INTEGER NOT NULL,
    chave_lote              TEXT NOT NULL,
    produto_produtor        INTEGER NOT NULL,
    quantidade              TEXT NOT NULL,
    linha_lote              INTEGER NOT NULL,
    criado_em               TEXT NOT NULL,
    PRIMARY KEY (base_origem, erp_item_codigo_origem)
);

-- Cópia local da tabela de preço do Flow no formato do contrato precos v1
-- (rota a). O Portal nunca edita: só aplica páginas (precos_repo.aplicar_pagina).
-- As tabelas do modelo anterior nunca saíram da branch: dbs de dev com o shape
-- velho perdem a cópia (o próximo ciclo reinicia do zero).
DROP TABLE IF EXISTS preco_rede;
DROP TABLE IF EXISTS preco_rede_cnpj;
DROP TABLE IF EXISTS preco_tabela;
DROP TABLE IF EXISTS preco_sync;

-- Rede = estado inteiro vindo do Flow (upsert por rede_id).
CREATE TABLE IF NOT EXISTS preco_rede_v1 (
    rede_id TEXT PRIMARY KEY,
    nome    TEXT NOT NULL,
    ativa   INTEGER NOT NULL,
    travada INTEGER NOT NULL
);

-- Chaves da rede: tipo 'raiz' (8 caracteres) ou 'cnpj' (14; vence a raiz).
-- Substituídas inteiras a cada reaparição da rede.
CREATE TABLE IF NOT EXISTS preco_rede_chave_v1 (
    rede_id TEXT NOT NULL,
    tipo    TEXT NOT NULL CHECK (tipo IN ('raiz', 'cnpj')),
    chave   TEXT NOT NULL,
    PRIMARY KEY (rede_id, tipo, chave)
);
CREATE INDEX IF NOT EXISTS idx_preco_rede_chave_v1 ON preco_rede_chave_v1(tipo, chave);

-- Preço append-only por preco_id (nunca apagado; cancelado volta pelo mesmo id).
-- rede_id NULL = preço base. Centavos inteiros; vigencia_inicio YYYY-MM-DD;
-- registrado_em é o texto do Flow (comparado como instante na regra).
CREATE TABLE IF NOT EXISTS preco_tabela_v1 (
    preco_id        TEXT PRIMARY KEY,
    rede_id         TEXT,
    empresa_cnpj    TEXT NOT NULL,
    erp_produto_id  TEXT NOT NULL,
    unidade_erp     TEXT,
    preco_centavos  INTEGER NOT NULL,
    vigencia_inicio TEXT NOT NULL,
    registrado_em   TEXT NOT NULL,
    cancelado       INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_preco_tabela_v1_produto
    ON preco_tabela_v1(empresa_cnpj, erp_produto_id);

-- Um cursor só (linha única). completo_em = geradoEm da última página com
-- temMais=false (frescor de 24 h conta daqui).
CREATE TABLE IF NOT EXISTS preco_sync_v1 (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    empresa_cnpj TEXT,
    cursor       TEXT,
    completo_em  TEXT
);

-- Aprovação por item do modelo anterior (assinatura por linha): nunca saiu da
-- branch; o contrato precos v1 aprova por revisão do pedido (abaixo).
DROP TABLE IF EXISTS preco_aprovacao;

-- Revisão do pedido enviada ao Flow (rota b): número monotônico por pedido +
-- hash do conteúdo que muda a avaliação. corpo_json = aprovacaoEnvioV1 com a
-- revisão. situacao: pendente_envio | enviada | recusada_flow | substituida | retirada.
-- bloqueio_local: corpo que não pode ir ao Flow (fora do contrato, 413, 400
-- payload_invalido); a revisão fica pendente_envio, fora da fila, até o conteúdo mudar.
CREATE TABLE IF NOT EXISTS preco_revisao (
    import_id           TEXT NOT NULL,
    revisao             INTEGER NOT NULL,
    conteudo_hash       TEXT NOT NULL,
    corpo_json          TEXT NOT NULL,
    situacao            TEXT NOT NULL CHECK (situacao IN
                            ('pendente_envio', 'enviada', 'recusada_flow',
                             'substituida', 'retirada')),
    tentada_em          TEXT,
    enviada_em          TEXT,
    resposta_json       TEXT,
    erro_definitivo     TEXT,
    retirada_em         TEXT,
    retirada_motivo     TEXT,
    retirada_enviada_em TEXT,
    bloqueio_local      TEXT,
    criado_em           TEXT NOT NULL,
    PRIMARY KEY (import_id, revisao)
);
CREATE INDEX IF NOT EXISTS idx_preco_revisao_situacao ON preco_revisao(situacao);

-- Piso da revisão (409 revisao_obsoleta do Flow): a próxima nasce acima dele.
CREATE TABLE IF NOT EXISTS preco_revisao_piso (
    import_id TEXT PRIMARY KEY,
    piso      INTEGER NOT NULL
);

-- Decisão do Flow por item (rota c), idempotente por decisao_id.
-- confirmacao = o que vai ao Flow no confirmar: aplicada | descartada.
CREATE TABLE IF NOT EXISTS preco_decisao (
    decisao_id              TEXT PRIMARY KEY,
    import_id               TEXT NOT NULL,
    revisao                 INTEGER NOT NULL,
    item_chave              TEXT NOT NULL,
    resultado               TEXT NOT NULL,
    preco_aprovado_centavos INTEGER,
    motivo_recusa           TEXT,
    observacao              TEXT,
    decidido_em             TEXT NOT NULL,
    recebida_em             TEXT NOT NULL,
    confirmacao             TEXT NOT NULL CHECK (confirmacao IN ('aplicada', 'descartada')),
    confirmada_em           TEXT
);
CREATE INDEX IF NOT EXISTS idx_preco_decisao_revisao ON preco_decisao(import_id, revisao);
CREATE INDEX IF NOT EXISTS idx_preco_decisao_pendente ON preco_decisao(confirmada_em);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_imports_imported_at    ON imports(imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_imports_customer_cnpj  ON imports(customer_cnpj);
CREATE INDEX IF NOT EXISTS idx_imports_fire_codigo    ON imports(fire_codigo);
CREATE INDEX IF NOT EXISTS idx_imports_status         ON imports(status);
CREATE INDEX IF NOT EXISTS idx_imports_portal_status  ON imports(portal_status);
CREATE INDEX IF NOT EXISTS idx_imports_prod_status    ON imports(production_status);
CREATE INDEX IF NOT EXISTS idx_imports_sha256         ON imports(file_sha256);

CREATE INDEX IF NOT EXISTS idx_audit_import_id ON audit_log(import_id);
CREATE INDEX IF NOT EXISTS idx_audit_created   ON audit_log(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_lifecycle_import_id  ON order_lifecycle_events(import_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_lifecycle_trace_id   ON order_lifecycle_events(trace_id);
CREATE INDEX IF NOT EXISTS idx_lifecycle_event_type ON order_lifecycle_events(event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_outbox_pending   ON outbox(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_outbox_import_id ON outbox(import_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_imports_fire_poll
    ON imports(portal_status, production_status, fire_status_polled_at)
    WHERE fire_codigo IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_depara_client ON produto_depara(client_key);
CREATE UNIQUE INDEX IF NOT EXISTS uq_vinculo_v2_vigente
    ON produto_vinculo_v2 (escopo, chave_tipo, chave_valor) WHERE revogado_em IS NULL;

CREATE INDEX IF NOT EXISTS idx_erp_item_vinculo_import ON erp_item_vinculo(import_id, indice_original);
CREATE INDEX IF NOT EXISTS idx_erp_xlsx_linha_pedido   ON erp_xlsx_linha(pedido_cliente);
CREATE INDEX IF NOT EXISTS idx_erp_estado_situacao     ON erp_pedido_estado(base, situacao);
CREATE INDEX IF NOT EXISTS idx_erp_eventos_pendentes   ON erp_eventos_saida(base, situacao, ordem);
CREATE INDEX IF NOT EXISTS idx_erp_lote_perna_chave    ON erp_lote_perna(chave_lote);

CREATE INDEX IF NOT EXISTS idx_cadastro_volta_produto  ON cadastro_volta(produto_id);
"""

# Cada entrada: (table, column_name, ALTER TABLE DDL) — aplicada só se a coluna ainda não existir.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("imports", "sem_preco_ack_by", "ALTER TABLE imports ADD COLUMN sem_preco_ack_by TEXT"),
    ("imports", "sem_preco_ack_at", "ALTER TABLE imports ADD COLUMN sem_preco_ack_at TEXT"),
    ("imports", "sem_preco_ack_items", "ALTER TABLE imports ADD COLUMN sem_preco_ack_items TEXT"),
    # Âncora FIXA da janela do poll de status Fire — gravada uma única vez em
    # `mark_found_in_fire`, nunca recarimbada por `update_fire_poll_result`.
    # Sem ela, a janela usava `fire_status_polled_at` (que o próprio poll
    # renova a cada tick) e nunca expirava — ver `list_pending_for_fire_poll`.
    ("imports", "reconciled_at", "ALTER TABLE imports ADD COLUMN reconciled_at TEXT"),
    # Trava de preço: CNPJ do cliente escolhido no override (define a rede da tabela).
    ("imports", "cliente_override_cnpj", "ALTER TABLE imports ADD COLUMN cliente_override_cnpj TEXT"),
    # Trava de preço: check do ambiente fonte da chave do produto (Nasmar → MM),
    # gravado no commit e no vínculo de-para; o preview avalia por ele.
    ("imports", "check_preco_json", "ALTER TABLE imports ADD COLUMN check_preco_json TEXT"),
    # SP1 — eco: o que a decisão do Flow mudou no ERP. `erp_codigos_json` porque
    # o UPDATE por PEDIDO_CLIENTE + CLIENTE pode tocar mais de um pedido
    # (BACKLOG 1.5); `erp_codigo` só quando tocou exatamente um.
    ("flowpcp_decisoes_mapping", "base", "ALTER TABLE flowpcp_decisoes_mapping ADD COLUMN base TEXT"),
    ("flowpcp_decisoes_mapping", "erp_codigo", "ALTER TABLE flowpcp_decisoes_mapping ADD COLUMN erp_codigo INTEGER"),
    ("flowpcp_decisoes_mapping", "erp_codigos_json", "ALTER TABLE flowpcp_decisoes_mapping ADD COLUMN erp_codigos_json TEXT"),
    ("flowpcp_decisoes_mapping", "nova_data", "ALTER TABLE flowpcp_decisoes_mapping ADD COLUMN nova_data TEXT"),
    ("flowpcp_decisoes_mapping", "aplicada_em", "ALTER TABLE flowpcp_decisoes_mapping ADD COLUMN aplicada_em TEXT"),
    # Trava de preço: revisão cujo POST saiu (o Flow pode conhecê-la sem 201).
    ("preco_revisao", "tentada_em", "ALTER TABLE preco_revisao ADD COLUMN tentada_em TEXT"),
    # Trava de preço: corpo que não pode ir ao Flow (bloqueio local, não recusa do Flow).
    ("preco_revisao", "bloqueio_local", "ALTER TABLE preco_revisao ADD COLUMN bloqueio_local TEXT"),
    # Livro da volta v2: gravação no Fire, reenvio do resultado ao Flow e regra 9b.
    ("cadastro_volta", "produto_id", "ALTER TABLE cadastro_volta ADD COLUMN produto_id TEXT"),
    ("cadastro_volta", "modo", "ALTER TABLE cadastro_volta ADD COLUMN modo TEXT"),
    ("cadastro_volta", "erp_produto_id", "ALTER TABLE cadastro_volta ADD COLUMN erp_produto_id TEXT"),
    ("cadastro_volta", "corpo_json", "ALTER TABLE cadastro_volta ADD COLUMN corpo_json TEXT"),
    ("cadastro_volta", "tentativas", "ALTER TABLE cadastro_volta ADD COLUMN tentativas INTEGER NOT NULL DEFAULT 0"),
    ("cadastro_volta", "proxima_tentativa_em", "ALTER TABLE cadastro_volta ADD COLUMN proxima_tentativa_em TEXT"),
    ("cadastro_volta", "alerta", "ALTER TABLE cadastro_volta ADD COLUMN alerta TEXT"),
    ("cadastro_volta", "resposta_flow", "ALTER TABLE cadastro_volta ADD COLUMN resposta_flow TEXT"),
    ("cadastro_volta", "nota_operador", "ALTER TABLE cadastro_volta ADD COLUMN nota_operador TEXT"),
)
