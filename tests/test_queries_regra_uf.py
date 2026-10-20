from __future__ import annotations

from app.erp import queries


def test_sql_comeca_com_select_e_tem_um_bind():
    assert queries.UF_CLIENTE_POR_CNPJ.lstrip().upper().startswith("SELECT")
    assert queries.UF_CLIENTE_POR_CNPJ.count("?") == 1
