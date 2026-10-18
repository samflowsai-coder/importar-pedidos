"""
All SQL strings for the Firebird/Fire Sistemas integration layer.

Schema verified against BKP_MM2_CONFECCAO_TERCA.fbk (April 2026).

Tables used:
  CAB_VENDAS    — sales order header (= "pedido de compra" recebido do varejista)
  CORPO_VENDAS  — sales order items (one row per product)
  CADASTRO      — clients/suppliers master (lookup by CPF_CNPJ)
  PRODUTOS      — product catalog (lookup by CODIGO_EAN13 or CODPROD_ALTERN)
"""

# ── Schema Discovery (read-only exploration) ─────────────────────────────────

LIST_TABLES = """
    SELECT TRIM(RDB$RELATION_NAME)
    FROM RDB$RELATIONS
    WHERE RDB$SYSTEM_FLAG = 0
      AND RDB$VIEW_BLR IS NULL
    ORDER BY RDB$RELATION_NAME
"""

LIST_COLUMNS = """
    SELECT
        TRIM(RF.RDB$FIELD_NAME),
        F.RDB$FIELD_TYPE,
        F.RDB$FIELD_LENGTH,
        F.RDB$FIELD_SUB_TYPE,
        RF.RDB$NULL_FLAG,
        RF.RDB$DEFAULT_VALUE
    FROM RDB$RELATION_FIELDS RF
    JOIN RDB$FIELDS F ON F.RDB$FIELD_NAME = RF.RDB$FIELD_SOURCE
    WHERE TRIM(RF.RDB$RELATION_NAME) = ?
    ORDER BY RF.RDB$FIELD_POSITION
"""

LIST_GENERATORS = """
    SELECT TRIM(RDB$GENERATOR_NAME)
    FROM RDB$GENERATORS
    WHERE RDB$SYSTEM_FLAG = 0
    ORDER BY RDB$GENERATOR_NAME
"""

# Use str.format(name=gen_name) — generator names cannot be parameterized
GET_GENERATOR_CURRENT = "SELECT GEN_ID({name}, 0) FROM RDB$DATABASE"
GET_GENERATOR_NEXT = "SELECT GEN_ID({name}, 1) FROM RDB$DATABASE"

LIST_FK_CONSTRAINTS = """
    SELECT
        TRIM(RC.RDB$RELATION_NAME),
        TRIM(ISEG.RDB$FIELD_NAME),
        TRIM(RC2.RDB$RELATION_NAME)
    FROM RDB$REF_CONSTRAINTS REFC
    JOIN RDB$RELATION_CONSTRAINTS RC
        ON RC.RDB$CONSTRAINT_NAME = REFC.RDB$CONSTRAINT_NAME
    JOIN RDB$INDEX_SEGMENTS ISEG
        ON ISEG.RDB$INDEX_NAME = RC.RDB$INDEX_NAME
    JOIN RDB$RELATION_CONSTRAINTS RC2
        ON RC2.RDB$CONSTRAINT_NAME = REFC.RDB$CONST_NAME_UQ
    ORDER BY RC.RDB$RELATION_NAME, ISEG.RDB$FIELD_NAME
"""

LIST_TRIGGERS = """
    SELECT
        TRIM(RDB$TRIGGER_NAME),
        RDB$TRIGGER_TYPE,
        RDB$TRIGGER_INACTIVE
    FROM RDB$TRIGGERS
    WHERE RDB$SYSTEM_FLAG = 0
      AND TRIM(RDB$RELATION_NAME) = ?
    ORDER BY RDB$TRIGGER_SEQUENCE
"""

LIST_INDEXES = """
    SELECT
        TRIM(RDB$INDEX_NAME),
        TRIM(RDB$RELATION_NAME),
        RDB$UNIQUE_FLAG
    FROM RDB$INDICES
    WHERE RDB$SYSTEM_FLAG = 0
    ORDER BY RDB$RELATION_NAME, RDB$INDEX_NAME
"""

# ── Business Queries ──────────────────────────────────────────────────────────

# Idempotency: check if order with same PEDIDO_CLIENTE + CLIENTE already exists.
# Production data shows DOCUMENTO is usually NULL; PEDIDO_CLIENTE stores the
# retailer's reference (e.g. 'AW097', '6694675', '0167180736956').
CHECK_ORDER_EXISTS = """
    SELECT COUNT(*) FROM CAB_VENDAS
    WHERE TRIM(PEDIDO_CLIENTE) = ?
      AND CLIENTE = ?
"""

# Get next PK for CAB_VENDAS (no dedicated generator — use safe MAX+1)
GET_NEXT_CABVENDAS_CODIGO = """
    SELECT COALESCE(MAX(CODIGO), 0) + 1 FROM CAB_VENDAS
"""

# Get next PK for CORPO_VENDAS via its generator
GET_NEXT_CORPOVENDAS_CODIGO = "SELECT GEN_ID(GEN_CORPO_VENDAS_CODIGO, 1) FROM RDB$DATABASE"

# Client lookup by CNPJ (digits only OR formatted — try both)
FIND_CLIENT_BY_CNPJ = """
    SELECT CODIGO, RAZAO_SOCIAL FROM CADASTRO
    WHERE REPLACE(REPLACE(REPLACE(REPLACE(CPF_CNPJ, '.', ''), '/', ''), '-', ''), ' ', '') = ?
      AND RELAC_CLIENTE = 'Sim'
    ROWS 1
"""

# Client lookup by primary key (CADASTRO.CODIGO). Used to validate manual
# overrides before passing the FK into CAB_VENDAS — guards against codigos
# that were inactivated between selection (HTTP) and send-to-fire (button).
FIND_CLIENT_BY_CODIGO = """
    SELECT CODIGO, RAZAO_SOCIAL, CPF_CNPJ FROM CADASTRO
    WHERE CODIGO = ?
      AND RELAC_CLIENTE = 'Sim'
    ROWS 1
"""

# Manual client picker search: matches RAZAO_SOCIAL substring (case-insensitive)
# OR CPF_CNPJ digits substring. Bind:
#   1. razao_pattern  — e.g. '%FOO%' (caller upper-cases)
#   2. cnpj_pattern   — e.g. '%12345%' (caller strips non-digits; pass a
#      sentinel like '%__never_matches__%' when q has no digits)
# FIRST 50 baked in (Firebird is happier with constant LIMITs than parameterized
# ROWS). TODO: add FANTASIA / CIDADE / UF columns once schema is reconfirmed
# via tools/explore_firebird.py against a fresh production copy.
SEARCH_CLIENTS = """
    SELECT FIRST 50 CODIGO, RAZAO_SOCIAL, CPF_CNPJ
    FROM CADASTRO
    WHERE RELAC_CLIENTE = 'Sim'
      AND (
        UPPER(RAZAO_SOCIAL) LIKE ?
        OR REPLACE(REPLACE(REPLACE(REPLACE(CPF_CNPJ, '.', ''), '/', ''), '-', ''), ' ', '') LIKE ?
      )
    ORDER BY RAZAO_SOCIAL
"""

# Product lookup by EAN-13. UNIDADE alimenta CORPO_VENDAS.UNID no insert
# (cadastro do produto, nao mais cravado "UN" — ver app/erp/mapper.py).
#
# NAO INTERPERMUTAVEL com find_products_by_eans_sql (abaixo), mesmo com
# mesma aridade (4 colunas) e bind posicional: aqui col[0]=SEQ e col[3]=UNIDADE;
# la col[0]=CODIGO_EAN13_TRIM (a chave, string) e col[3]=PRECO_VENDA. Ler um
# resultado como se fosse o outro le tipo/campo errado em silencio (ex.:
# UNIDADE onde se esperava PRECO_VENDA).
FIND_PRODUCT_BY_EAN = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA, UNIDADE FROM PRODUTOS
    WHERE CODIGO_EAN13 = ?
    ROWS 1
"""

# Product lookup by alternative code (CODPROD_ALTERN). UNIDADE — ver acima.
# NAO INTERPERMUTAVEL com find_products_by_codes_sql — mesmo aviso do
# FIND_PRODUCT_BY_EAN acima.
FIND_PRODUCT_BY_CODE = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA, UNIDADE FROM PRODUTOS
    WHERE TRIM(CODPROD_ALTERN) = ?
    ROWS 1
"""

# Product lookup by SEQ (usado na resolução de de-para).
FIND_PRODUCT_BY_SEQ = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS
    WHERE SEQ = ?
    ROWS 1
"""


def find_products_by_eans_sql(n: int) -> str:
    """SELECT batelado por EAN. Retorna (CODIGO_EAN13_TRIM, SEQ, DESCRICAO, PRECO_VENDA).

    TRIM em ambos os lados (SELECT e WHERE) — CODIGO_EAN13 é CHAR(n) no Firebird,
    então sem TRIM a chave do map fica blank-padded e o compare em Python
    (`it.ean in ean_map`) nunca bate (EAN do parser vem sem padding). ORDER BY SEQ
    torna o primeiro resultado determinístico quando o catálogo tem EAN duplicado.

    NÃO INTERPERMUTÁVEL com `FIND_PRODUCT_BY_EAN` (singular, acima) — mesma
    aridade (4 colunas), significado incompatível por posição: aqui col[0] é
    a chave (string) e col[3] é PRECO_VENDA; lá col[0] é SEQ e col[3] é
    UNIDADE. Bind posicional confundido lê o campo errado sem erro nenhum.
    """
    placeholders = ", ".join(["?"] * n)
    return (
        "SELECT TRIM(CODIGO_EAN13), SEQ, DESCRICAO, PRECO_VENDA "
        f"FROM PRODUTOS WHERE TRIM(CODIGO_EAN13) IN ({placeholders}) ORDER BY SEQ"
    )


def find_products_by_codes_sql(n: int) -> str:
    """SELECT batelado por CODPROD_ALTERN. Retorna (CODPROD_ALTERN_TRIM, SEQ, DESCRICAO, PRECO_VENDA).

    ORDER BY SEQ torna o primeiro resultado determinístico quando o catálogo tem
    CODPROD_ALTERN duplicado (mesmo motivo do find_products_by_eans_sql).

    NÃO INTERPERMUTÁVEL com `FIND_PRODUCT_BY_CODE` (singular, acima) — mesmo
    aviso de `find_products_by_eans_sql`: mesma aridade, col[0]/col[3]
    trocados de significado.
    """
    placeholders = ", ".join(["?"] * n)
    return (
        "SELECT TRIM(CODPROD_ALTERN), SEQ, DESCRICAO, PRECO_VENDA "
        f"FROM PRODUTOS WHERE TRIM(CODPROD_ALTERN) IN ({placeholders}) ORDER BY SEQ"
    )


def prever_import_fire_sql(n: int) -> str:
    """Palpite do importador de Excel do Fire. Retorna (CODPROD_ALTERN, SEQ, DESCRICAO).

    O importador do Fire (`MECANICO=99`) não busca o código exato: casa por
    PREFIXO e fica com o primeiro registro da ordem física. Esta query reproduz
    isso em lote: sem ORDER BY de propósito, o chamador fica com a primeira
    linha que começa com cada código. Conferido na Fire viva contra
    `SELECT FIRST 1 ... STARTING WITH ?` um a um: 620/620 iguais (24/09/2026,
    MM e Nasmar). Ver BACKLOG 2.15.
    """
    return "SELECT CODPROD_ALTERN, SEQ, DESCRICAO FROM PRODUTOS WHERE " + " OR ".join(
        ["CODPROD_ALTERN STARTING WITH ?"] * n
    )


def find_products_by_seqs_sql(n: int) -> str:
    """SELECT batelado por SEQ. Retorna (SEQ, DESCRICAO, PRECO_VENDA)."""
    placeholders = ", ".join(["?"] * n)
    return f"SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS WHERE SEQ IN ({placeholders})"


# Insert sales order header (CAB_VENDAS).
#
# Colunas e valores conferidos contra 373 pedidos do .7 e 90 do .4 na Fire
# viva (2026-08-24, todos de 2026-06-01 em diante). As 14 primeiras estao
# preenchidas em 100% dos pedidos digitados pela operacao; CLASSIF_FAT,
# CODFIGFISCAL, DT_BASE_FAT e MECANICO em 82% a 100%.
#
# CODPED_PAI (auto-referencia = CODIGO) NAO entra aqui: o valor so existe
# depois do INSERT. Vai por UPDATE_CODPED_PAI logo em seguida, na mesma
# transacao.
INSERT_CAB_VENDAS = """
    INSERT INTO CAB_VENDAS (
        CODIGO, CODEMPRESA, DATA_PEDIDO,
        CLIENTE, STATUS, PEDIDO_CLIENTE,
        OBS, DT_ENTREGA, DT_BASE_FAT,
        VALOR_TOTAL, TOTAL_PRODUTO, DESCONTO,
        TIPO_COB, COD_CLASS_FINAN, DESC_CLASS_FINAN,
        CLASSIF_FAT, CODFIGFISCAL, MECANICO,
        SEM_IMP, PED_ZF, EH_VENDACONSUMIDOR, VENDEDOR_COMI,
        ULT_INS_USER, ULT_ALT_USER,
        ULT_INS_DTHR, ULT_ALT_DTHR, DTHORA_PEDIDO
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?,
        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    )
"""

# CODPED_PAI aponta pro proprio pedido em 100% dos pedidos medidos.
UPDATE_CODPED_PAI = """
    UPDATE CAB_VENDAS SET CODPED_PAI = CODIGO WHERE CODIGO = ?
"""

# ── Poll Worker (Fase 5) ──────────────────────────────────────────────────────

# Fetch current STATUS for a single order by its CODIGO (PK in CAB_VENDAS).
# Called once per order per poll cycle — individual queries avoid dynamic
# IN-clause generation and are well within latency budget (<100 orders/day).
GET_ORDER_STATUS_BY_CODE = """
    SELECT CODIGO, STATUS
    FROM CAB_VENDAS
    WHERE CODIGO = ?
    ROWS 1
"""

# Insert order item (CORPO_VENDAS).
#
# Conferido contra 1.285 linhas de item no .7 (2026-08). ICMS_PORC, ICMS_BASE,
# REDUCAO, DESC_SOBRE_TOTAL, PESO_BRUTO e PESO_LIQUIDO estao em 100% das linhas;
# CFOP_PRINCIPAL em 96%.
INSERT_CORPO_VENDAS = """
    INSERT INTO CORPO_VENDAS (
        CODIGO, CODVENDA, CODPRODUTO,
        DESCRICAO, QTD, PRECO_UNITARIO, TOTAL,
        UNID, DT_ENTREGA_ITEM,
        ICMS_PORC, ICMS_BASE, REDUCAO,
        DESC_SOBRE_TOTAL, PESO_BRUTO, PESO_LIQUIDO,
        CFOP_PRINCIPAL
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?,
        ?, ?, ?,
        ?, ?, ?,
        ?
    )
"""

# Reconciliação FlowPCP → Fire: atualiza data de entrega do pedido.
# Chave = PEDIDO_CLIENTE (ref do varejista) + CLIENTE (FK CADASTRO.CODIGO).
UPDATE_DT_ENTREGA = """
    UPDATE CAB_VENDAS
       SET DT_ENTREGA = ?
     WHERE TRIM(PEDIDO_CLIENTE) = ?
       AND CLIENTE = ?
"""

# ── Catálogo de produtos (sync Fire→Flow, Fase 0) ─────────────────────────────
# Subconjunto de IDENTIDADE de PRODUTOS. SEQ é a PK durável (imutável) E o código
# que o cliente usa (codigo = str(SEQ)). BLOQUEADO ∈ {'Sim','Nao'} → ativo =
# (BLOQUEADO <> 'Sim'). IS_KIT = 1 se o SEQ é pai em PRODUTOS_KIT; o tipo final
# (pack/kit/simples) sai de catalog_extract._tipo, que também olha unidade e nome.
# 12 colunas, nesta ordem (catalog_extract lê por posição): SEQ, DESCRICAO,
# UNIDADE, CODIGO_EAN13, BLOQUEADO, IS_KIT, CODPROD_ALTERN (código do par no
# Apontaê), e as 5 fiscais/cadastrais CLASSIF_FISCAL, CODITEMSPED, GRUPO,
# GRUPO_NOME, CODPARANFE (catalogo_fiscal valida). DESCRICAO2 não é lida: é a
# "Descrição Interna" da tela do Fire, não cor (17/10/2026). GRUPO_NOME vem de
# LEFT JOIN GRUPO_PRODUTOS: produto sem grupo (ou com grupo apagado) não some.
LIST_PRODUTOS_CATALOGO = """
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
           CASE WHEN EXISTS (
               SELECT 1 FROM PRODUTOS_KIT K WHERE K.CODPRODUTO_PAI = P.SEQ
           ) THEN 1 ELSE 0 END AS IS_KIT,
           P.CODPROD_ALTERN,
           P.CLASSIF_FISCAL, P.CODITEMSPED, P.GRUPO, G.GRUPO AS GRUPO_NOME,
           P.CODPARANFE
    FROM PRODUTOS P
    LEFT JOIN GRUPO_PRODUTOS G ON G.CODIGO = P.GRUPO
    ORDER BY P.SEQ
"""

# Variante filtrada (flowpcp_catalogo_apenas_meias): as meias que a fábrica
# vende — subgrupo MEIAS, mais todo produto que aparece em pedido aberto ou em
# pedido dos últimos 12 meses, menos cueca (decisão do Samuel, 15/10/2026: cueca
# e kit de cueca ficam fora do Flow). O Fire não tem campo que separe cueca;
# o nome tem (CUECA/BOXER, CONTAINING ignora caixa). Só o subgrupo deixava de
# fora meias vendidas sem a marcação (ACTVITTA infantil, Kings, Oxer): 45 dos
# 424 produtos dos pedidos abertos da MM em 15/10, e o pedido chegava ao Flow
# como "produto desconhecido". Fora continua o que nunca vendeu (tecido cru,
# cadastro morto).
# Mais os componentes, em qualquer nível (PACK → KIT → par): o par não é
# vendido, é o que o Flow produz; sem isto os pares dos kits ficavam só no Flow
# (181 na MM em 15/10, com OP e vínculo ao Apontaê). 1.026 produtos na MM.
# NIVEL < 5 trava ciclo em PRODUTOS_KIT (A→B→A): sem ele o Firebird aborta no
# nível 1024 e o catálogo inteiro para por uma linha ruim (hoje a profundidade
# máxima é 2). Componente cueca também fica fora.
# O DISTINCT + JOIN no fim é de propósito: `IN (SELECT ... FROM RECORTE)`
# reavalia a CTE por linha no Firebird 2.5 (passou de 2 min; o JOIN leva 1 s).
LIST_PRODUTOS_CATALOGO_MEIAS = """
    WITH RECURSIVE RECORTE (SEQ, NIVEL) AS (
        SELECT P.SEQ, 0 FROM PRODUTOS P
        WHERE P.CODSUBGRUPO IN (
            SELECT CODIGO FROM GRUPO_PRODUTOS_SUB WHERE UPPER(TRIM(DESCRICAO)) = 'MEIAS'
        )
           OR (EXISTS (
                   SELECT 1 FROM CORPO_VENDAS I JOIN CAB_VENDAS V ON V.CODIGO = I.CODVENDA
                   WHERE I.CODPRODUTO = P.SEQ
                     AND (TRIM(V.STATUS) = 'PEDIDO'
                          OR V.DATA_PEDIDO >= DATEADD(-12 MONTH TO CURRENT_DATE))
               )
               AND P.DESCRICAO NOT CONTAINING 'CUECA'
               AND P.DESCRICAO NOT CONTAINING 'BOXER')
        UNION ALL
        SELECT K.CODPRODUTO, R.NIVEL + 1
        FROM PRODUTOS_KIT K
        JOIN RECORTE R ON K.CODPRODUTO_PAI = R.SEQ
        JOIN PRODUTOS F ON F.SEQ = K.CODPRODUTO
        WHERE R.NIVEL < 5
          AND K.CODPRODUTO <> K.CODPRODUTO_PAI
          AND F.DESCRICAO NOT CONTAINING 'CUECA'
          AND F.DESCRICAO NOT CONTAINING 'BOXER'
    )
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
           CASE WHEN EXISTS (
               SELECT 1 FROM PRODUTOS_KIT K WHERE K.CODPRODUTO_PAI = P.SEQ
           ) THEN 1 ELSE 0 END AS IS_KIT,
           P.CODPROD_ALTERN,
           P.CLASSIF_FISCAL, P.CODITEMSPED, P.GRUPO, G.GRUPO AS GRUPO_NOME,
           P.CODPARANFE
    FROM (SELECT DISTINCT SEQ FROM RECORTE) R
    JOIN PRODUTOS P ON P.SEQ = R.SEQ
    LEFT JOIN GRUPO_PRODUTOS G ON G.CODIGO = P.GRUPO
    ORDER BY P.SEQ
"""

# Composição (PRODUTOS_KIT) para o catálogo enviado ao Flow: pai, filho e
# quantidade, um nível (o Flow explode recursivo). Sem filtro: são centenas de linhas na
# MM, e quem decide o que vai é catalogo_composicao. ORDER BY CODIGO mantém a
# ordem de cadastro, que vira a posição do filho no Flow.
LIST_PRODUTOS_KIT_COMPOSICAO = """
    SELECT K.CODPRODUTO_PAI, K.CODPRODUTO, K.QTD
    FROM PRODUTOS_KIT K
    ORDER BY K.CODPRODUTO_PAI, K.CODIGO
"""

# Marcas do Fire e produtos que já têm nota fiscal, para a ida do catálogo
# (marcasErp / temNotaFiscal). Sem filtro: quem decide o que vai é catalogo_marcas.
LIST_PRODUTOS_MARCA = "SELECT CODIGO, MARCA FROM PRODUTOS_MARCA ORDER BY CODIGO"
LIST_PRODUTOS_COM_NOTA = "SELECT DISTINCT CODPRODUTO FROM NOTAPROD"

# ── Clientes ativos (carga Fire→Flow) ─────────────────────────────────────────
# Clientes (RELAC_CLIENTE='Sim', não BLOQUEADO) com pelo menos um pedido em
# CAB_VENDAS dentro da janela (bind = data de corte, calculada no Python).
# Verificado na Fire viva MM (192.168.15.4, MM_CONFECCAO.FDB) em 2026-07-17:
#   - CODGRUPO existe (gate I1 OK) mas está NULL em 100% do CADASTRO → grupo_codigo
#     vem None na prática; a coluna fica p/ quando a MM popular a marca no Fire.
#   - BLOQUEADO ∈ {'Sim','Nao'} (null-free); filtramos <> 'Sim' (mesmo padrão do
#     catálogo). Hoje 0 ativos bloqueados, mas é a definição correta de "vendável".
LIST_CLIENTES_ATIVOS = """
    SELECT C.CODIGO, C.RAZAO_SOCIAL, C.CPF_CNPJ, C.CODGRUPO
    FROM CADASTRO C
    WHERE C.RELAC_CLIENTE = 'Sim'
      AND C.BLOQUEADO <> 'Sim'
      AND EXISTS (
          SELECT 1 FROM CAB_VENDAS V
          WHERE V.CLIENTE = C.CODIGO
            AND V.DATA_PEDIDO >= ?
      )
    ORDER BY C.CODIGO
"""

# ── De-para de cliente intercompany (Nasmar → cliente real) ───────────────────
# Lido no Firebird da REVENDA (.4). A chave é o PEDIDO_CLIENTE (número do pedido
# de compra do cliente final), o mesmo valor que o .7 guarda no pedido faturado
# contra a Nasmar. TRIM simétrico: o bind chega .strip()ado do chamador.
# Verificado na Fire viva 2026-07-25: 939 pedidos Nasmar no .7, 0 ambiguidade.
FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE = """
    SELECT V.CODIGO, V.STATUS, V.CODNF,
           C.CODIGO, TRIM(C.NOME), TRIM(C.RAZAO_SOCIAL), TRIM(C.CPF_CNPJ)
    FROM CAB_VENDAS V
    JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
    WHERE TRIM(V.PEDIDO_CLIENTE) = ?
"""


# ── Reconciliação de pedidos (portal × Fire) ───────────────────────────────────
def FIND_ORDERS_BY_PEDIDO_CLIENTE(n: int) -> str:
    """Pedidos do Fire por lista de números, com cliente e data.

    Generaliza a `FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE` para receber N números
    de uma vez — 308 pedidos viram 2 idas ao banco em vez de 308.

    As colunas duplicadas NÃO têm alias no SQL — quem lê o cursor por posição
    (índice 0-based) deve usar: índice 1 = V.CODIGO (pedido no Fire),
    índice 4 = C.CODIGO (cliente no Fire). São nomes iguais (`CODIGO`) em
    tabelas diferentes; a posição é o que distingue, não o nome.

    `DATA_PEDIDO` sai junto porque a guarda temporal precisa dela: número curto
    e sequencial (K01, MF048, AF198) se repete entre anos no MESMO cliente, e a
    chave dupla não fecha esse caso sozinha.
    """
    if n <= 0:
        raise ValueError(
            "FIND_ORDERS_BY_PEDIDO_CLIENTE requer n >= 1 "
            "(IN () é SQL inválida no Firebird)"
        )
    marcadores = ", ".join("?" for _ in range(n))
    return f"""
    SELECT TRIM(V.PEDIDO_CLIENTE), V.CODIGO, V.STATUS, V.DATA_PEDIDO,
           C.CODIGO, TRIM(C.CPF_CNPJ)
    FROM CAB_VENDAS V
    JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
    WHERE TRIM(V.PEDIDO_CLIENTE) IN ({marcadores})
    """


# ── Roteamento: histórico do cliente (degrau 2) ───────────────────────────────
# "Este cliente já comprou desta empresa nos últimos N meses?" — a resposta
# que decide o ambiente quando o documento não traz o CNPJ do fornecedor.
# CPF_CNPJ é limpo do mesmo jeito que em FIND_CLIENT_BY_CNPJ; o bind chega em
# dígitos. Aceita N CNPJs porque uma planilha de desmembramento identifica o
# comprador pelas colunas de loja, não pelo cabeçalho.
def count_pedidos_cliente_desde_sql(n: int) -> str:
    """SQL com N placeholders de CNPJ. Bind: (desde, cnpj1, ..., cnpjN)."""
    if n < 1:
        raise ValueError("count_pedidos_cliente_desde_sql exige ao menos 1 CNPJ")
    marks = ", ".join("?" for _ in range(n))
    return f"""
        SELECT COUNT(*), MAX(V.DATA_PEDIDO)
        FROM CAB_VENDAS V
        JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
        WHERE V.DATA_PEDIDO >= ?
          AND REPLACE(REPLACE(REPLACE(REPLACE(
                C.CPF_CNPJ, '.', ''), '/', ''), '-', ''), ' ', '') IN ({marks})
    """


# ── SP1: vigia de pedidos do ERP (SÓ LEITURA) ─────────────────────────────────
# Lidas dentro de `FirebirdConnection.connect_somente_leitura` (transação
# READ ONLY + SNAPSHOT): mesmo que alguém troque um SELECT por UPDATE aqui, o
# Firebird recusa. `tests/test_erp_eventos_origem.py::test_todo_sql_da_vigia_e_select`
# também garante que todo SQL deste bloco começa com SELECT.
#
# "Aberto" = qualquer STATUS fora de FATURADO/CANCELADO — a mesma regra do
# Flow para reabertura (spec SP1 §4.1) e de `modelo.esta_aberto`: sem caixa
# (UPPER) e STATUS nulo conta como aberto (COALESCE), senão o pedido sairia da
# lista sem nunca fechar. Medido: PEDIDO, FATURADO, CANCELADO e 1 "EM ANÁLISE".
_CAB_VENDAS_ERP = """
    SELECT V.CODIGO, TRIM(V.PEDIDO_CLIENTE), V.CLIENTE, TRIM(C.CPF_CNPJ),
           TRIM(V.STATUS), V.DATA_PEDIDO, V.DT_ENTREGA
    FROM CAB_VENDAS V
    LEFT JOIN CADASTRO C ON C.CODIGO = V.CLIENTE
"""

LIST_PEDIDOS_ABERTOS_ERP = (
    _CAB_VENDAS_ERP
    + "    WHERE COALESCE(UPPER(TRIM(V.STATUS)), '') NOT IN ('FATURADO', 'CANCELADO')\n"
)


def _marcas(n: int) -> str:
    if n < 1:
        raise ValueError("IN () é SQL inválida no Firebird: n >= 1")
    return ", ".join("?" for _ in range(n))


def pedidos_erp_por_codigo_sql(n: int) -> str:
    return _CAB_VENDAS_ERP + f"    WHERE V.CODIGO IN ({_marcas(n)})\n"


def itens_erp_por_pedido_sql(n: int) -> str:
    """(CODIGO, CODVENDA, CODPRODUTO, DESCRICAO, QTD, PRECO_UNITARIO, UNID, DT_ENTREGA_ITEM)."""
    return (
        "SELECT I.CODIGO, I.CODVENDA, I.CODPRODUTO, TRIM(I.DESCRICAO), I.QTD, "
        "I.PRECO_UNITARIO, TRIM(I.UNID), I.DT_ENTREGA_ITEM "
        f"FROM CORPO_VENDAS I WHERE I.CODVENDA IN ({_marcas(n)})"
    )


def faturado_erp_por_item_sql(n: int) -> str:
    """Quantidade faturada ACUMULADA por item (spec SP1 §11: CORPO_VENDAS_NFS,
    não os campos de NF do item, que são vazios). (COD_ITEMVENDA, SUM(QTD), MAX(CODNF))."""
    return (
        "SELECT N.COD_ITEMVENDA, SUM(N.QTD), MAX(N.CODNF) "
        f"FROM CORPO_VENDAS_NFS N WHERE N.COD_ITEMVENDA IN ({_marcas(n)}) "
        "GROUP BY N.COD_ITEMVENDA"
    )


def clientes_reais_por_pedidos_cliente_sql(n: int) -> str:
    """Versão em lote de FIND_CLIENTE_REAL_BY_PEDIDO_CLIENTE: mesma ordem de
    colunas, com o PEDIDO_CLIENTE (TRIM) na frente para agrupar."""
    return (
        "SELECT TRIM(V.PEDIDO_CLIENTE), V.CODIGO, V.STATUS, V.CODNF, "
        "C.CODIGO, TRIM(C.NOME), TRIM(C.RAZAO_SOCIAL), TRIM(C.CPF_CNPJ) "
        "FROM CAB_VENDAS V JOIN CADASTRO C ON C.CODIGO = V.CLIENTE "
        f"WHERE TRIM(V.PEDIDO_CLIENTE) IN ({_marcas(n)})"
    )


# Eco: quais pedidos o UPDATE_DT_ENTREGA vai tocar (mesma chave dele).
CODIGOS_POR_PEDIDO_CLIENTE_E_CLIENTE = """
    SELECT CODIGO FROM CAB_VENDAS
    WHERE TRIM(PEDIDO_CLIENTE) = ?
      AND CLIENTE = ?
"""


# Carga de material (spec 2026-10-16-flow-fonte-do-cadastro §6.2): tudo que não é
# CONFECCIONADO (grupo 8). Vai com BLOQUEADO para o Flow saber o ativo; o Flow não
# cria insumo inativo novo. Em 16/10/2026: ~381 na MM (.7).
LIST_MATERIAIS = """
    SELECT P.SEQ, P.DESCRICAO, P.UNIDADE, P.CODIGO_EAN13, P.BLOQUEADO,
           P.CLASSIF_FISCAL, P.CODITEMSPED, P.GRUPO, G.GRUPO AS GRUPO_NOME
    FROM PRODUTOS P
    LEFT JOIN GRUPO_PRODUTOS G ON G.CODIGO = P.GRUPO
    WHERE P.GRUPO IS NOT NULL AND P.GRUPO <> 8
    ORDER BY P.SEQ
"""

# NCM das notas de ENTRADA (CFOP 1xxx/2xxx/3xxx) de material nos últimos 24
# meses; a mais recente por produto é escolhida em Python (Firebird 2.5 não tem
# janela). Referência para o fiscal, nunca valor do cadastro.
LIST_ENTRADAS_NCM = """
    SELECT I.CODPRODUTO, N.NF_EMISSAO, I.NCM
    FROM NOTAPROD I
    JOIN NOTA N ON N.CODIGO = I.CODNOTA
    JOIN PRODUTOS P ON P.SEQ = I.CODPRODUTO
    WHERE P.GRUPO <> 8
      AND N.NF_EMISSAO >= DATEADD(-24 MONTH TO CURRENT_DATE)
      AND SUBSTRING(TRIM(I.CFOP) FROM 1 FOR 1) IN ('1', '2', '3')
      AND I.NCM IS NOT NULL
    ORDER BY N.NF_EMISSAO, N.CODIGO
"""
