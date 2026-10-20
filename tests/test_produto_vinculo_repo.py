"""De-para v2 (produto_vinculo_v2, db do ambiente): escopo, revogação, conflito."""

from __future__ import annotations

import sqlite3

import pytest

from app.persistence import produto_vinculo_repo as repo
from app.persistence import router


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


def _vincular(conn, **over):
    base = dict(
        escopo="rede:7",
        chave_tipo="codigo",
        chave_valor="KG02PR",
        fire_seq=2077,
        fire_codigo="KG02PR",
        fire_descricao="MEIA KIDS",
        por="grazi@mm",
        pedido_id="imp-1",
        agora="2026-10-19T10:00:00",
    )
    base.update(over)
    return repo.vincular(conn, **base)


def test_norm_chave():
    assert repo.norm_chave("ean", " 07898-686 ") == "07898686"
    assert repo.norm_chave("codigo", "KG 07 004") == "KG 07 004"
    assert repo.norm_chave("codigo", " kg02pr ") == "KG02PR"
    assert repo.norm_chave("codigo", None) == ""
    assert repo.norm_chave("ean", None) == ""


def test_vincular_e_lookup_por_escopo(env_conn):
    v = _vincular(env_conn, chave_valor="kg02pr ")
    assert v.chave_valor == "KG02PR"
    assert v.origem == "manual"
    assert v.criado_por == "grazi@mm"

    achados = repo.lookup(env_conn, ["rede:7", "raiz:12345678"], codigos=["KG02PR"], eans=[])

    assert set(achados) == {("codigo", "KG02PR")}
    assert achados[("codigo", "KG02PR")].fire_seq == 2077


def test_lookup_normaliza_a_chave_pedida(env_conn):
    _vincular(env_conn)
    achados = repo.lookup(env_conn, ["rede:7"], codigos=[" kg02pr"], eans=[])
    assert ("codigo", "KG02PR") in achados


def test_lookup_ean_so_digitos(env_conn):
    _vincular(env_conn, chave_tipo="ean", chave_valor="789-8686", fire_seq=5)
    achados = repo.lookup(env_conn, ["rede:7"], codigos=[], eans=["7898686 "])
    assert achados[("ean", "7898686")].fire_seq == 5


def test_lookup_sem_chaves_ou_escopos_devolve_vazio(env_conn):
    _vincular(env_conn)
    assert repo.lookup(env_conn, [], codigos=["KG02PR"], eans=[]) == {}
    assert repo.lookup(env_conn, ["rede:7"], codigos=[], eans=[]) == {}
    assert repo.lookup(env_conn, ["rede:7"], codigos=["", None], eans=[""]) == {}


def test_lookup_ignora_outro_escopo(env_conn):
    _vincular(env_conn, escopo="rede:9")
    assert repo.lookup(env_conn, ["rede:7", "raiz:1"], codigos=["KG02PR"], eans=[]) == {}


def test_lookup_rede_vence_raiz(env_conn):
    _vincular(env_conn, escopo="rede:7", fire_seq=1)
    _vincular(env_conn, escopo="raiz:12345678", fire_seq=2)

    achados = repo.lookup(env_conn, ["rede:7", "raiz:12345678"], codigos=["KG02PR"], eans=[])
    assert achados[("codigo", "KG02PR")].fire_seq == 1

    # a ordem da lista manda, não a ordem de gravação
    achados = repo.lookup(env_conn, ["raiz:12345678", "rede:7"], codigos=["KG02PR"], eans=[])
    assert achados[("codigo", "KG02PR")].fire_seq == 2


def test_vincular_mesmo_seq_devolve_o_vigente(env_conn):
    primeiro = _vincular(env_conn)
    de_novo = _vincular(env_conn, agora="2026-10-19T11:00:00", por="outra")
    assert de_novo.id == primeiro.id
    assert len(repo.listar(env_conn, incluir_revogados=True)) == 1


def test_vincular_conflito_sem_substituir(env_conn):
    _vincular(env_conn, fire_seq=10)
    with pytest.raises(repo.VinculoConflito) as exc:
        _vincular(env_conn, fire_seq=11)
    assert exc.value.atual.fire_seq == 10
    assert len(repo.listar(env_conn, incluir_revogados=True)) == 1


def test_substituir_revoga_antigo(env_conn):
    antigo = _vincular(env_conn, fire_seq=10)
    novo = _vincular(
        env_conn, fire_seq=11, substituir=True, por="bianca", agora="2026-10-19T12:00:00"
    )

    assert novo.id != antigo.id
    assert novo.fire_seq == 11

    vigentes = repo.listar(env_conn)
    assert [v.id for v in vigentes] == [novo.id]

    todos = {v.id: v for v in repo.listar(env_conn, incluir_revogados=True)}
    assert set(todos) == {antigo.id, novo.id}
    row = env_conn.execute(
        "SELECT revogado_em, revogado_por FROM produto_vinculo_v2 WHERE id = ?", (antigo.id,)
    ).fetchone()
    assert row[0] == "2026-10-19T12:00:00"
    assert row[1] == "bianca"
    assert (
        repo.lookup(env_conn, ["rede:7"], codigos=["KG02PR"], eans=[])[
            ("codigo", "KG02PR")
        ].fire_seq
        == 11
    )


def test_revogado_nao_aparece_no_lookup(env_conn):
    v = _vincular(env_conn)
    revogado = repo.revogar(env_conn, v.id, por="grazi", agora="2026-10-19T13:00:00")
    assert revogado.id == v.id
    assert repo.lookup(env_conn, ["rede:7"], codigos=["KG02PR"], eans=[]) == {}
    assert repo.listar(env_conn) == []
    assert len(repo.listar(env_conn, incluir_revogados=True)) == 1


def test_revogar_libera_a_chave_para_novo_vinculo(env_conn):
    v = _vincular(env_conn, fire_seq=10)
    repo.revogar(env_conn, v.id, por="grazi", agora="2026-10-19T13:00:00")
    novo = _vincular(env_conn, fire_seq=12)
    assert novo.fire_seq == 12


def test_revogar_inexistente_levanta(env_conn):
    with pytest.raises(LookupError):
        repo.revogar(env_conn, 999, por="x", agora="2026-10-19T13:00:00")


def test_listar_filtra_por_escopo(env_conn):
    _vincular(env_conn, escopo="rede:7")
    _vincular(env_conn, escopo="rede:9", chave_valor="OUTRA")
    assert [v.escopo for v in repo.listar(env_conn, escopo="rede:9")] == ["rede:9"]
    assert len(repo.listar(env_conn)) == 2


def test_chave_vazia_e_recusada(env_conn):
    with pytest.raises(ValueError):
        _vincular(env_conn, chave_valor="  ")
    with pytest.raises(ValueError):
        _vincular(env_conn, chave_tipo="descricao")


def test_origem_migrado_v1(env_conn):
    v = _vincular(env_conn, origem="migrado_v1")
    assert v.origem == "migrado_v1"


def test_transacao_aberta_do_chamador_nao_e_tocada(env_conn):
    env_conn.execute(
        "INSERT INTO produto_vinculo_v2 (escopo, chave_tipo, chave_valor, fire_seq, "
        "fire_descricao, origem, criado_em) VALUES ('rede:1','codigo','X',1,'d','manual','t')"
    )
    assert env_conn.in_transaction

    with pytest.raises(RuntimeError):
        _vincular(env_conn, chave_valor="OUTRA")
    with pytest.raises(RuntimeError):
        repo.revogar(env_conn, 1, por="x", agora="2026-10-19T13:00:00")

    # a escrita do chamador segue pendente: nem efetivada nem descartada
    assert env_conn.in_transaction
    env_conn.rollback()
    assert env_conn.execute("SELECT COUNT(*) FROM produto_vinculo_v2").fetchone()[0] == 0


def test_origem_invalida_e_recusada_sem_revogar_o_antigo(env_conn):
    _vincular(env_conn, fire_seq=10)
    with pytest.raises(ValueError):
        _vincular(env_conn, fire_seq=11, substituir=True, origem="inventada")
    assert [v.fire_seq for v in repo.listar(env_conn)] == [10]


def test_corrida_no_indice_unico_vira_conflito(env_conn, monkeypatch):
    existente = _vincular(env_conn, fire_seq=10)
    leitura_real = repo._vigente
    chamadas = []

    def vigente_cego_na_primeira(*args, **kwargs):
        chamadas.append(1)
        return None if len(chamadas) == 1 else leitura_real(*args, **kwargs)

    monkeypatch.setattr(repo, "_vigente", vigente_cego_na_primeira)

    with pytest.raises(repo.VinculoConflito) as exc:
        _vincular(env_conn, fire_seq=11)
    assert exc.value.atual == existente
    assert [v.fire_seq for v in repo.listar(env_conn)] == [10]


def test_integrity_error_que_nao_e_unico_sobe_e_faz_rollback(env_conn):
    antigo = _vincular(env_conn, fire_seq=10)
    with pytest.raises(sqlite3.IntegrityError) as exc:
        _vincular(env_conn, fire_seq=11, substituir=True, fire_descricao=None)
    assert not isinstance(exc.value, repo.VinculoConflito)
    # o rollback desfez a revogação do antigo
    assert [v.id for v in repo.listar(env_conn)] == [antigo.id]
    assert not env_conn.in_transaction
