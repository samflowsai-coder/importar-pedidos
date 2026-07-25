from __future__ import annotations

from app.erp import product_ranking as pr

CATALOG = [
    {"fire_produto_id": "1", "codigo": "1", "nome": "TENIS CORRIDA AZUL", "ean": "7890001"},
    {"fire_produto_id": "2", "codigo": "2", "nome": "SANDALIA COURO PRETA", "ean": "7890002"},
    {"fire_produto_id": "3", "codigo": "3", "nome": "TENIS CAMINHADA CINZA", "ean": "7890003"},
]


def test_ranqueia_por_ean_parcial_primeiro():
    out = pr.rank_candidates(
        description="qualquer", product_code=None, ean="0001", catalog=CATALOG, limit=3
    )
    assert out[0]["fire_produto_id"] == "1"


def test_ranqueia_por_tokens_da_descricao():
    out = pr.rank_candidates(
        description="tenis azul corrida", product_code=None, ean=None, catalog=CATALOG, limit=3
    )
    assert out[0]["fire_produto_id"] == "1"
    assert all("score" in c for c in out)


def test_limita_resultados():
    out = pr.rank_candidates(
        description="tenis", product_code=None, ean=None, catalog=CATALOG, limit=1
    )
    assert len(out) == 1


def test_sem_sinal_retorna_vazio_ou_score_zero():
    out = pr.rank_candidates(description="", product_code=None, ean=None, catalog=CATALOG, limit=3)
    assert out == [] or all(c["score"] == 0 for c in out)
