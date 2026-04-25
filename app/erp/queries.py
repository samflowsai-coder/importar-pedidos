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

# Product lookup by EAN-13
FIND_PRODUCT_BY_EAN = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS
    WHERE CODIGO_EAN13 = ?
    ROWS 1
"""

# Product lookup by alternative code (CODPROD_ALTERN)
FIND_PRODUCT_BY_CODE = """
    SELECT SEQ, DESCRICAO, PRECO_VENDA FROM PRODUTOS
    WHERE TRIM(CODPROD_ALTERN) = ?
    ROWS 1
"""

# Insert sales order header (CAB_VENDAS).
#
# Production data pattern (verified against Americanense 2026-04-21 backup):
#   - STATUS = 'PEDIDO' for new orders (NOT 'Aberto')
#   - DOCUMENTO is usually NULL (retailer ref goes to PEDIDO_CLIENTE)
#   - CLINAOCAD is never used in practice (always NULL) — CLIENTE FK required
#   - DTHORA_PEDIDO holds the creation timestamp alongside ULT_INS_DTHR
INSERT_CAB_VENDAS = """
    INSERT INTO CAB_VENDAS (
        CODIGO, CODEMPRESA, DATA_PEDIDO,
        CLIENTE, STATUS, PEDIDO_CLIENTE,
        OBS, DT_ENTREGA,
        ULT_INS_USER, ULT_INS_DTHR, DTHORA_PEDIDO
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?,
        ?, ?,
        ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    )
"""

# Insert order item (CORPO_VENDAS)
INSERT_CORPO_VENDAS = """
    INSERT INTO CORPO_VENDAS (
        CODIGO, CODVENDA, CODPRODUTO,
        DESCRICAO, QTD, PRECO_UNITARIO, TOTAL,
        UNID, DT_ENTREGA_ITEM
    ) VALUES (
        ?, ?, ?,
        ?, ?, ?, ?,
        ?, ?
    )
"""
