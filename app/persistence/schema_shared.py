"""Schema do banco SQLite compartilhado (`app_shared.db`).

Hospeda metadata transversal: autenticação, sessões, ambientes (multi-empresa),
idempotência de webhooks inbound, rate-limit buckets. Nada que seja
financeiro/operacional de um pedido vive aqui — isso fica no schema_env.
"""
from __future__ import annotations

TABLES_SQL = """
-- Multi-empresa (MM, Nasmar, ...). slug é imutável após create e dele
-- deriva o nome do arquivo `app_state_<slug>.db`. Senha cifrada via
-- secret_store (Fernet). is_active=0 esconde da UI mas preserva FKs.
CREATE TABLE IF NOT EXISTS environments (
    id              TEXT PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    watch_dir       TEXT NOT NULL,
    output_dir      TEXT NOT NULL,
    fb_path         TEXT NOT NULL,
    fb_host         TEXT,
    fb_port         TEXT,
    fb_user         TEXT NOT NULL DEFAULT 'SYSDBA',
    fb_charset      TEXT NOT NULL DEFAULT 'WIN1252',
    fb_password_enc TEXT,
    -- CNPJ da empresa que este ambiente representa, SO DIGITOS. E a chave do
    -- roteamento pelo documento: o CNPJ do fornecedor impresso no pedido casa
    -- aqui. NULL = ambiente nao participa do roteamento automatico.
    cnpj            TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    -- Ponte FlowPCP (Modelo B/OVERLAY) por ambiente. Token cifrado via
    -- secret_store (Fernet), como fb_password_enc. Só MM liga.
    flowpcp_enabled           INTEGER NOT NULL DEFAULT 0,
    flowpcp_base_url          TEXT,
    flowpcp_tenant_id         TEXT,
    flowpcp_timezone          TEXT NOT NULL DEFAULT 'America/Sao_Paulo',
    flowpcp_dry_run           INTEGER NOT NULL DEFAULT 0,
    flowpcp_poll_interval_s   INTEGER NOT NULL DEFAULT 30,
    flowpcp_request_timeout_s REAL NOT NULL DEFAULT 30.0,
    flowpcp_service_token_enc TEXT,
    -- Gate do envio de catálogo Fire→Flow: OFF = sync só atualiza a cópia
    -- local (catalogo_fire no db do ambiente); ON = também envia ao Flow.
    flowpcp_catalogo_push     INTEGER NOT NULL DEFAULT 0,
    -- Filtro do catálogo extraído do Fire: OFF = tudo (PRODUTOS inteiro,
    -- comportamento atual); ON = só o subgrupo MEIAS (GRUPO_PRODUTOS_SUB).
    -- Depende da marcação no Fire (Parte 2); default OFF preserva o hoje.
    flowpcp_catalogo_apenas_meias INTEGER NOT NULL DEFAULT 0,
    flowpcp_clientes_push     INTEGER NOT NULL DEFAULT 0,
    fiscal_codfigfiscal       INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'operator',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

CREATE TABLE IF NOT EXISTS user_invites (
    token              TEXT PRIMARY KEY,
    email              TEXT NOT NULL COLLATE NOCASE,
    role               TEXT NOT NULL DEFAULT 'operator',
    invited_by_user_id INTEGER NOT NULL,
    created_at         TEXT NOT NULL,
    expires_at         TEXT NOT NULL,
    accepted_at        TEXT,
    accepted_user_id   INTEGER,
    revoked_at         TEXT,
    FOREIGN KEY (invited_by_user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (accepted_user_id)   REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token        TEXT PRIMARY KEY,
    user_id      INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    ip           TEXT,
    user_agent   TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inbound_idempotency (
    provider         TEXT NOT NULL,
    event_id         TEXT NOT NULL,
    received_at      TEXT NOT NULL,
    response_status  INTEGER,
    response_body    TEXT,
    import_id        TEXT,
    PRIMARY KEY (provider, event_id)
);

CREATE TABLE IF NOT EXISTS rate_limit_buckets (
    key            TEXT PRIMARY KEY,
    tokens         REAL NOT NULL,
    last_refill_at REAL NOT NULL
);

-- Memória das escolhas de ambiente feitas pelo operador. NASCE VAZIA e isso é
-- um estado válido: o Portal funciona sem uma linha aqui. Perde para o
-- documento E para o histórico — é o degrau 3, não a verdade.
-- `divergiu_*` é preenchido quando um degrau mais forte contradiz a escolha
-- depois: erro que aparece é erro que se conserta.
CREATE TABLE IF NOT EXISTS decisao_ambiente (
    cnpj_cliente  TEXT PRIMARY KEY,
    env_slug      TEXT NOT NULL,
    decidido_por  TEXT NOT NULL,
    decidido_em   TEXT NOT NULL,
    divergiu_em   TEXT,
    divergiu_de   TEXT
);
"""

INDEXES_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_environments_slug ON environments(slug);
CREATE INDEX IF NOT EXISTS idx_environments_active      ON environments(is_active);
-- UNIQUE, não só INDEX: dois ambientes ATIVOS com o mesmo CNPJ fariam
-- find_by_cnpj devolver o que o LIMIT 1 pegasse — pedido pra empresa errada,
-- em silêncio. Escopado a is_active=1 porque é exatamente o conjunto que
-- find_by_cnpj enxerga (mesmo WHERE): a restrição existe só pra garantir que
-- essa busca nunca tenha dois candidatos, nem mais nem menos — um ambiente
-- desativado não pode segurar o CNPJ pra sempre, senão desativar e recadastrar
-- (pasta errada, etc.) trava com 409 sem motivo real. O DROP antes é
-- necessário porque `CREATE ... IF NOT EXISTS` com o MESMO nome não substitui
-- um índice já existente (mesmo que a definição mude) — sem o DROP, um banco
-- que já tivesse materializado uma versão anterior deste índice nunca
-- ganharia a garantia atual.
DROP INDEX IF EXISTS idx_environments_cnpj;
CREATE UNIQUE INDEX IF NOT EXISTS idx_environments_cnpj ON environments(cnpj)
    WHERE cnpj IS NOT NULL AND is_active = 1;

CREATE INDEX IF NOT EXISTS idx_sessions_user_id    ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_invites_email_pending ON user_invites(email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_invites_expires_at    ON user_invites(expires_at);

CREATE INDEX IF NOT EXISTS idx_inbound_received_at  ON inbound_idempotency(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_inbound_import_id    ON inbound_idempotency(import_id);

CREATE INDEX IF NOT EXISTS idx_decisao_ambiente_env ON decisao_ambiente(env_slug);
"""

# Migrações de coluna para shared.db — aplicadas se a coluna ainda não existir.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("environments", "flowpcp_enabled",
     "ALTER TABLE environments ADD COLUMN flowpcp_enabled INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_base_url",
     "ALTER TABLE environments ADD COLUMN flowpcp_base_url TEXT"),
    ("environments", "flowpcp_tenant_id",
     "ALTER TABLE environments ADD COLUMN flowpcp_tenant_id TEXT"),
    ("environments", "flowpcp_timezone",
     "ALTER TABLE environments ADD COLUMN flowpcp_timezone TEXT NOT NULL DEFAULT 'America/Sao_Paulo'"),
    ("environments", "flowpcp_dry_run",
     "ALTER TABLE environments ADD COLUMN flowpcp_dry_run INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_poll_interval_s",
     "ALTER TABLE environments ADD COLUMN flowpcp_poll_interval_s INTEGER NOT NULL DEFAULT 30"),
    ("environments", "flowpcp_request_timeout_s",
     "ALTER TABLE environments ADD COLUMN flowpcp_request_timeout_s REAL NOT NULL DEFAULT 30.0"),
    ("environments", "flowpcp_service_token_enc",
     "ALTER TABLE environments ADD COLUMN flowpcp_service_token_enc TEXT"),
    ("environments", "flowpcp_catalogo_push",
     "ALTER TABLE environments ADD COLUMN flowpcp_catalogo_push INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_catalogo_apenas_meias",
     "ALTER TABLE environments ADD COLUMN flowpcp_catalogo_apenas_meias INTEGER NOT NULL DEFAULT 0"),
    ("environments", "flowpcp_clientes_push",
     "ALTER TABLE environments ADD COLUMN flowpcp_clientes_push INTEGER NOT NULL DEFAULT 0"),
    # De-para de cliente intercompany: CNPJ que dispara (a revenda) + slug do
    # ambiente cujo Firebird tem o vínculo. Qualquer um vazio = desligado.
    ("environments", "intercompany_cnpj",
     "ALTER TABLE environments ADD COLUMN intercompany_cnpj TEXT"),
    ("environments", "intercompany_env_slug",
     "ALTER TABLE environments ADD COLUMN intercompany_env_slug TEXT"),
    # Figura fiscal do ambiente. Medido na Fire viva: 1 na MM Americanense,
    # 5 na Nasmar. NULL = usa o default de app/erp/fiscal.py.
    ("environments", "fiscal_codfigfiscal",
     "ALTER TABLE environments ADD COLUMN fiscal_codfigfiscal INTEGER"),
    # CNPJ da empresa do ambiente, só dígitos — chave de `find_by_cnpj`.
    ("environments", "cnpj",
     "ALTER TABLE environments ADD COLUMN cnpj TEXT"),
)
