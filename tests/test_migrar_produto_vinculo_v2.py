"""Migração do de-para v1 (produto_depara) para o v2 (produto_vinculo_v2)."""

from __future__ import annotations

import pytest

from app.persistence import produto_depara_repo, produto_vinculo_repo, router
from tools import migrar_produto_vinculo_v2 as mig

AGORA = "2026-10-20T09:00:00"
CNPJ_A = "11222333000181"  # raiz 11222333
CNPJ_B = "11222333000262"  # mesma raiz
CNPJ_C = "44555666000100"  # outra raiz
REVENDA = "99888777000155"


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _v1(conn, *, key, tipo="codigo", valor="KG02PR", seq="2077", codigo="2077", por="grazi@mm"):
    produto_depara_repo.upsert(
        conn,
        client_key=key,
        chave_tipo=tipo,
        chave_valor=valor,
        fire_produto_id=seq,
        fire_codigo=codigo,
        fire_ean=None,
        fire_nome="MEIA KIDS",
        criado_em="2026-09-01T10:00:00",
        criado_por=por,
    )
    conn.commit()


def _import(conn, *, id_, nome, cnpj):
    conn.execute(
        "INSERT INTO imports (id, environment_id, source_filename, imported_at, "
        "customer_cnpj, customer_name, status) VALUES (?, 'mm', ?, ?, ?, ?, 'parsed')",
        (id_, f"{id_}.pdf", "2026-09-01T10:00:00", cnpj, nome),
    )
    conn.commit()


def _vigentes(conn):
    return produto_vinculo_repo.listar(conn)


def test_migrar_dry_run_nao_escreve(env_conn):
    _v1(env_conn, key=CNPJ_A)

    r = mig.migrar(env_conn, slug="mm", aplicar=False, agora=AGORA, cnpjs_revenda=set())

    assert [m["chave_valor"] for m in r["migrados"]] == ["KG02PR"]
    assert r["migrados"][0]["escopo"] == "raiz:11222333"
    assert _vigentes(env_conn) == []


def test_migrar_cnpj_vira_raiz(env_conn):
    _v1(env_conn, key=CNPJ_A, valor="kg02pr")

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert len(r["migrados"]) == 1
    (v,) = _vigentes(env_conn)
    assert v.escopo == "raiz:11222333"
    assert (v.chave_tipo, v.chave_valor) == ("codigo", "KG02PR")
    assert v.fire_seq == 2077
    assert v.fire_codigo is None  # SEQ do v1 não é CODPROD_ALTERN
    assert v.fire_descricao == "MEIA KIDS"
    assert v.origem == "migrado_v1"
    assert v.criado_por == "grazi@mm"
    assert v.criado_em == AGORA


def test_migrar_cnpj_na_rede_grava_no_escopo_da_rede(env_conn):
    env_conn.execute("INSERT INTO preco_rede_v1 VALUES ('7', 'Rede Sete', 1, 0)")
    env_conn.execute("INSERT INTO preco_rede_chave_v1 VALUES ('7', 'raiz', '11222333')")
    env_conn.commit()
    _v1(env_conn, key=CNPJ_A)

    mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert [v.escopo for v in _vigentes(env_conn)] == ["rede:7"]


def test_migrar_fire_codigo_vem_do_catalogo_quando_existe(env_conn):
    env_conn.execute(
        "INSERT INTO catalogo_fire (fire_produto_id, codigo, nome, extraido_em, codprod_altern) "
        "VALUES ('2077', '2077', 'MEIA KIDS', '2026-10-01', ' kg02pr ')"
    )
    env_conn.commit()
    _v1(env_conn, key=CNPJ_A)

    mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert _vigentes(env_conn)[0].fire_codigo == "KG02PR"


def test_migrar_nome_com_raiz_unica(env_conn):
    _v1(env_conn, key="LOJAS EXEMPLO S.A.")
    # duas filiais da MESMA raiz: raiz única
    _import(env_conn, id_="i1", nome="Lojas  Exemplo s.a.", cnpj=CNPJ_A)
    _import(env_conn, id_="i2", nome="LOJAS EXEMPLO S.A.", cnpj=CNPJ_B)

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert len(r["migrados"]) == 1
    assert r["sem_cnpj_unico"] == []
    assert [v.escopo for v in _vigentes(env_conn)] == ["raiz:11222333"]


def test_migrar_nome_ambiguo_fica_de_fora(env_conn):
    _v1(env_conn, key="LOJAS EXEMPLO S.A.")
    _import(env_conn, id_="i1", nome="LOJAS EXEMPLO S.A.", cnpj=CNPJ_A)
    _import(env_conn, id_="i2", nome="LOJAS EXEMPLO S.A.", cnpj=CNPJ_C)
    _v1(env_conn, key="SEM HISTORICO LTDA", valor="X1")

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert r["migrados"] == []
    assert sorted(x["client_key"] for x in r["sem_cnpj_unico"]) == [
        "LOJAS EXEMPLO S.A.",
        "SEM HISTORICO LTDA",
    ]
    assert _vigentes(env_conn) == []


def test_migrar_idempotente(env_conn):
    _v1(env_conn, key=CNPJ_A)
    _v1(env_conn, key=CNPJ_C, valor="7890000000017", tipo="ean", seq="55")

    primeira = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())
    segunda = mig.migrar(env_conn, slug="mm", aplicar=True, agora="2026-10-21T09:00:00",
                         cnpjs_revenda=set())

    assert len(primeira["migrados"]) == 2
    assert segunda["migrados"] == []
    assert len(segunda["ja_existiam"]) == 2
    assert len(_vigentes(env_conn)) == 2


def test_migrar_dry_run_ja_existiam_reflete_o_que_existe(env_conn):
    _v1(env_conn, key=CNPJ_A)
    mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    r = mig.migrar(env_conn, slug="mm", aplicar=False, agora=AGORA, cnpjs_revenda=set())

    assert r["migrados"] == []
    assert len(r["ja_existiam"]) == 1


def test_migrar_conflito_nao_sobrescreve(env_conn):
    produto_vinculo_repo.vincular(
        env_conn, escopo="raiz:11222333", chave_tipo="codigo", chave_valor="KG02PR",
        fire_seq=999, fire_codigo=None, fire_descricao="OUTRO", por="grazi@mm",
        pedido_id=None, agora="2026-10-10T10:00:00",
    )
    _v1(env_conn, key=CNPJ_A)

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert r["migrados"] == []
    assert len(r["conflito"]) == 1
    assert [v.fire_seq for v in _vigentes(env_conn)] == [999]


def test_migrar_duas_filiais_da_mesma_raiz_nao_geram_conflito_falso(env_conn):
    # mesma chave nas duas filiais, mesmo produto: a segunda vira "já existia"
    _v1(env_conn, key=CNPJ_A)
    _v1(env_conn, key=CNPJ_B)

    r = mig.migrar(env_conn, slug="mm", aplicar=False, agora=AGORA, cnpjs_revenda=set())

    assert len(r["migrados"]) == 1
    assert len(r["ja_existiam"]) == 1


def test_migrar_revenda_intercompany_nao_migra(env_conn):
    _v1(env_conn, key=REVENDA)
    # nome que resolve para o CNPJ da revenda também não migra
    _v1(env_conn, key="NASMAR COMERCIO", valor="Y2")
    _import(env_conn, id_="i1", nome="NASMAR COMERCIO", cnpj=REVENDA)

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda={REVENDA})

    assert r["migrados"] == []
    assert len(r["revenda_intercompany"]) == 2
    assert _vigentes(env_conn) == []


def test_migrar_le_revenda_da_config_do_ambiente(env_conn, monkeypatch):
    from app.persistence import environments_repo

    monkeypatch.setattr(
        environments_repo, "get_by_slug", lambda slug: {"intercompany_cnpj": REVENDA}
    )
    _v1(env_conn, key=REVENDA)

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA)

    assert len(r["revenda_intercompany"]) == 1
    assert _vigentes(env_conn) == []


def test_migrar_seq_invalido_vai_para_invalidos(env_conn):
    _v1(env_conn, key=CNPJ_A, seq="abc")

    r = mig.migrar(env_conn, slug="mm", aplicar=True, agora=AGORA, cnpjs_revenda=set())

    assert len(r["invalidos"]) == 1
    assert _vigentes(env_conn) == []
