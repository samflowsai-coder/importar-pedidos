from __future__ import annotations

from app.erp import queries


def test_sql_comeca_com_select_e_tem_dois_binds():
    assert queries.UF_CLIENTE_POR_CNPJ.lstrip().upper().startswith("SELECT")
    assert queries.UF_CLIENTE_POR_CNPJ.count("?") == 2


def test_sql_casa_cpf_cnpj_direto_sem_replace():
    """REPLACE na coluna varre o CADASTRO inteiro; IN (mascarado, dígitos) usa índice."""
    sql = " ".join(queries.UF_CLIENTE_POR_CNPJ.split()).upper()
    assert "REPLACE" not in sql
    assert "C.CPF_CNPJ IN (?, ?)" in sql
