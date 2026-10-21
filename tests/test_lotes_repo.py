from __future__ import annotations

import sqlite3
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.persistence import db, lotes_repo

AGORA = datetime(2026, 8, 19, 10, 0, 0)


@pytest.fixture()
def conn(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    with db.connect_shared() as c:
        yield c
    db.set_db_path(None)


def _lote(conn, inicio, slug="nasmar", modo="semanal"):
    fim = date.fromordinal(inicio.toordinal() + 6)
    return lotes_repo.lote_para(
        conn,
        env_origem_slug=slug,
        env_espelho_slug="mm",
        inicio=inicio,
        fim=fim,
        modo_janela=modo,
        agora=AGORA,
    )


def _fator(conn, valor="0.07", desde=date(2026, 8, 17), modo="desconto"):
    return lotes_repo.registrar_fator(
        conn,
        env_origem_slug="nasmar",
        fator=Decimal(valor),
        modo=modo,
        vigente_desde=desde,
        por="s",
        agora=AGORA,
    )


def _perna(conn, lote_id, import_id="imp-1", codigo=1157):
    lotes_repo.anexar_perna(
        conn,
        lote_id=lote_id,
        import_id=import_id,
        env_origem_slug="nasmar",
        fire_codigo_origem=codigo,
        itens_fire=[(10, 0), (11, 1)],
        pedido_cliente="AF179",
        cnpj_cliente_final="05055599002802",
        razao_cliente_final="H 2 S 4",
        agora=AGORA,
    )


def _fechar(conn, lote_id, pernas=0):
    return lotes_repo.fechar(
        conn,
        lote_id=lote_id,
        import_id_espelho="esp-1",
        composicao=[[{"a": 1}]],
        pernas_esperadas=pernas,
        por="job",
        agora=AGORA,
    )


def _config(conn, **kw):
    base = dict(
        env_origem_slug="nasmar",
        env_espelho_slug="mm",
        cnpj_revenda="123",
        nome_revenda="Nasmar",
        janela="semanal",
        dia_inicio=0,
        hora_fechamento=8,
        ativo=True,
        por="s",
        agora=AGORA,
    )
    base.update(kw)
    return lotes_repo.save_config(conn, **base)


def test_lote_e_criado_uma_vez_por_janela(conn):
    a = _lote(conn, date(2026, 8, 17))
    b = _lote(conn, date(2026, 8, 17))
    assert a["id"] == b["id"]
    assert a["status"] == "aberto"
    assert a["chave_lote"] == "NAS-2026-S34"


def test_chave_do_lote_e_unica_por_janela(conn):
    a = _lote(conn, date(2026, 8, 17))
    b = _lote(conn, date(2026, 8, 24))
    assert a["chave_lote"] != b["chave_lote"]
    assert lotes_repo.buscar_lote(conn, "nasmar", date(2026, 8, 24))["id"] == b["id"]


def test_anexar_perna_e_listar(conn):
    lote = _lote(conn, date(2026, 8, 17))
    _perna(conn, lote["id"])
    pernas = lotes_repo.pernas_do_lote(conn, lote["id"])
    assert len(pernas) == 1
    assert pernas[0]["fire_codigo_origem"] == 1157
    assert pernas[0]["itens_fire"] == [(10, 0), (11, 1)]
    assert lotes_repo.lote_do_espelho(conn, "esp-1") is None
    assert lotes_repo.get(conn, lote["id"])["status"] == "aberto"


def test_mesma_perna_duas_vezes_no_mesmo_lote_e_noop(conn):
    lote = _lote(conn, date(2026, 8, 17))
    _perna(conn, lote["id"])
    _perna(conn, lote["id"])
    assert len(lotes_repo.pernas_do_lote(conn, lote["id"])) == 1


def test_sem_fator_o_lote_vai_para_aguardando_fator(conn):
    """Sem fator cadastrado o lote nao fecha (preco inferido em NF = imposto errado)."""
    lote = _lote(conn, date(2026, 8, 17))
    lotes_repo.marcar_aguardando_fator(conn, lote["id"])
    atual = lotes_repo.get(conn, lote["id"])
    assert atual["status"] == "aguardando_fator"
    assert atual["fator_preco_aplicado"] is None


def test_informar_fator_grava_quem_e_quando(conn):
    lote = _lote(conn, date(2026, 8, 17))
    lotes_repo.marcar_aguardando_fator(conn, lote["id"])
    r = lotes_repo.informar_fator(
        conn,
        lote_id=lote["id"],
        fator=Decimal("0.07"),
        modo="divisor",
        por="samuel@x.com",
        agora=AGORA,
    )
    assert r["fator_preco_aplicado"] == "0.07"
    assert r["modo_preco_aplicado"] == "divisor"
    assert r["fator_informado_por"] == "samuel@x.com"
    assert r["fator_informado_em"] is not None
    assert r["status"] == "aberto"


def test_informar_fator_em_lote_fechado_levanta(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    assert _fechar(conn, lote["id"]) is True
    with pytest.raises(lotes_repo.LoteFechadoError):
        lotes_repo.informar_fator(
            conn,
            lote_id=lote["id"],
            fator=Decimal("0.1"),
            modo="desconto",
            por="s",
            agora=AGORA,
        )


def test_lote_fechado_nao_aceita_perna_nova(conn):
    """Pedido atrasado vai pro proximo lote. Lote fechado nunca reabre."""
    _fator(conn)  # vigente_desde <= janela_inicio
    lote = _lote(conn, date(2026, 8, 17))
    assert _fechar(conn, lote["id"]) is True
    with pytest.raises(lotes_repo.LoteFechadoError):
        _perna(conn, lote["id"], import_id="imp-2", codigo=1189)


def test_fator_vale_so_a_partir_da_janela_seguinte(conn):
    _fator(conn, "0.07", desde=date(2026, 8, 24))
    a = _lote(conn, date(2026, 8, 17))
    b = _lote(conn, date(2026, 8, 24))
    assert a["fator_preco_aplicado"] is None
    assert b["fator_preco_aplicado"] == "0.07" and b["modo_preco_aplicado"] == "desconto"


def test_mudar_o_fator_nao_reescreve_lote_ja_aberto(conn):
    _fator(conn, "0.07")
    lote = _lote(conn, date(2026, 8, 17))
    _fator(conn, "0.12")
    assert lotes_repo.get(conn, lote["id"])["fator_preco_aplicado"] == "0.07"


def test_fator_vigente_pega_o_mais_recente_e_desempata_pelo_ultimo(conn):
    assert lotes_repo.fator_vigente(conn, "nasmar", date(2026, 8, 17)) is None
    _fator(conn, "0.07", desde=date(2026, 8, 10))
    _fator(conn, "0.09", desde=date(2026, 8, 17))
    _fator(conn, "0.12", desde=date(2026, 8, 17))
    assert lotes_repo.fator_vigente(conn, "nasmar", date(2026, 8, 16)) == (
        Decimal("0.07"),
        "desconto",
    )
    assert lotes_repo.fator_vigente(conn, "nasmar", date(2026, 8, 20)) == (
        Decimal("0.12"),
        "desconto",
    )


def test_mesma_perna_em_dois_lotes_e_recusada(conn):
    a = _lote(conn, date(2026, 8, 17))
    b = _lote(conn, date(2026, 8, 24))
    _perna(conn, a["id"])
    with pytest.raises(sqlite3.IntegrityError):
        _perna(conn, b["id"])


def test_fechar_duas_vezes_so_vale_uma(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    assert _fechar(conn, lote["id"]) is True
    assert _fechar(conn, lote["id"]) is False
    g = lotes_repo.get(conn, lote["id"])
    assert g["status"] == "fechado"
    assert g["composicao"] == [[{"a": 1}]]
    assert g["closed_by"] == "job"
    assert lotes_repo.lote_do_espelho(conn, "esp-1")["id"] == lote["id"]


def test_fechar_sem_fator_nao_fecha(conn):
    lote = _lote(conn, date(2026, 8, 17))
    assert _fechar(conn, lote["id"]) is False
    assert lotes_repo.get(conn, lote["id"])["status"] == "aberto"


def test_marcar_erro(conn):
    lote = _lote(conn, date(2026, 8, 17))
    lotes_repo.marcar_erro(conn, lote_id=lote["id"], motivo="boom", agora=AGORA)
    g = lotes_repo.get(conn, lote["id"])
    assert g["status"] == "erro" and g["erro_motivo"] == "boom"


def test_marcar_erro_nao_reabre_lote_fechado(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    assert _fechar(conn, lote["id"]) is True
    lotes_repo.marcar_erro(conn, lote_id=lote["id"], motivo="atrasado", agora=AGORA)
    g = lotes_repo.get(conn, lote["id"])
    assert g["status"] == "fechado" and g["erro_motivo"] is None


def test_marcar_enviado_so_de_fechado_e_so_uma_vez(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))

    def enviar(codigo):
        return lotes_repo.marcar_enviado(
            conn, lote_id=lote["id"], fire_codigo_espelho=codigo, lote_fiscal_ok=True, agora=AGORA
        )

    assert enviar(77) is False  # ainda aberto
    assert _fechar(conn, lote["id"]) is True
    assert enviar(77) is True
    assert enviar(99) is False
    g = lotes_repo.get(conn, lote["id"])
    assert g["status"] == "enviado"
    assert g["fire_codigo_espelho"] == 77 and g["lote_fiscal_ok"] is True
    assert g["sent_at"] is not None


def test_fechar_recusa_se_chegou_perna_depois_da_leitura(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    _perna(conn, lote["id"])
    lidas = len(lotes_repo.pernas_do_lote(conn, lote["id"]))
    _perna(conn, lote["id"], import_id="imp-2", codigo=1189)
    assert _fechar(conn, lote["id"], pernas=lidas) is False
    assert lotes_repo.get(conn, lote["id"])["status"] == "aberto"
    assert _fechar(conn, lote["id"], pernas=2) is True


def test_reanexar_perna_em_lote_fechado_e_noop(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    _perna(conn, lote["id"])
    assert _fechar(conn, lote["id"], pernas=1) is True
    _perna(conn, lote["id"])  # já está lá: no-op, sem erro
    assert len(lotes_repo.pernas_do_lote(conn, lote["id"])) == 1


def test_anexar_nao_passa_se_fechamento_concorrente_comitou(conn, tmp_path):
    """Fechamento de outra conexão entre a leitura do chamador e o INSERT."""
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    conn.commit()
    with db.connect_shared() as outra:
        assert _fechar(outra, lote["id"]) is True
        outra.commit()
    with pytest.raises(lotes_repo.LoteFechadoError):
        _perna(conn, lote["id"], import_id="imp-late")
    assert lotes_repo.pernas_do_lote(conn, lote["id"]) == []


def test_chave_de_outra_origem_levanta(conn):
    _lote(conn, date(2026, 8, 17), slug="nasmar")
    with pytest.raises(lotes_repo.ChaveDeLoteEmUsoError, match="nasmar2"):
        _lote(conn, date(2026, 8, 17), slug="nasmar2")


def test_config_upsert_e_listagem(conn):
    assert lotes_repo.get_config(conn, "nasmar") is None
    c = _config(conn)
    assert c["ativo"] is True and c["hora_fechamento"] == 8
    c = _config(conn, hora_fechamento=9, ativo=False)
    assert c["hora_fechamento"] == 9 and c["ativo"] is False
    assert len(lotes_repo.listar_configs(conn, so_ativas=False)) == 1
    assert lotes_repo.listar_configs(conn, so_ativas=True) == []


def test_trocar_janela_com_lote_aberto_com_perna_levanta(conn):
    _config(conn)
    lote = _lote(conn, date(2026, 8, 17))
    _perna(conn, lote["id"])
    with pytest.raises(lotes_repo.JanelaComLoteAbertoError):
        _config(conn, janela="quinzenal")
    # sem trocar a janela, salvar segue valendo
    _config(conn, nome_revenda="Nasmar 2")


def test_trocar_janela_com_lote_aberto_sem_perna_passa(conn):
    _config(conn)
    _lote(conn, date(2026, 8, 17))
    assert _config(conn, janela="quinzenal")["janela"] == "quinzenal"


def test_lotes_vencidos_respeita_a_hora(conn):
    _config(conn, hora_fechamento=8)
    lote = _lote(conn, date(2026, 8, 17))  # 17-23/08
    assert lotes_repo.lotes_vencidos(conn, datetime(2026, 8, 24, 7, 59)) == []
    v = lotes_repo.lotes_vencidos(conn, datetime(2026, 8, 24, 8, 0))
    assert [x["id"] for x in v] == [lote["id"]]


def test_lotes_vencidos_sem_config_usa_8h_e_ignora_fechados(conn):
    _fator(conn)
    lote = _lote(conn, date(2026, 8, 17))
    assert len(lotes_repo.lotes_vencidos(conn, datetime(2026, 8, 24, 8, 0))) == 1
    _fechar(conn, lote["id"])
    assert lotes_repo.lotes_vencidos(conn, datetime(2026, 8, 24, 8, 0)) == []


def test_listar_ordena_do_mais_recente(conn):
    _lote(conn, date(2026, 8, 17))
    b = _lote(conn, date(2026, 8, 24))
    lst = lotes_repo.listar(conn, limite=1)
    assert [x["id"] for x in lst] == [b["id"]]


def test_ativo_desde_grava_na_ativacao_e_mantem_se_segue_ativo(conn):
    t1, t2, t3, t4 = (datetime(2026, 8, d, 9, 0, 0) for d in (1, 2, 3, 4))
    assert _config(conn, agora=t1)["ativo_desde"] == "2026-08-01T09:00:00"
    assert _config(conn, agora=t2)["ativo_desde"] == "2026-08-01T09:00:00"
    desligada = _config(conn, ativo=False, agora=t3)
    assert desligada["ativo_desde"] == "2026-08-01T09:00:00"
    assert _config(conn, agora=t4)["ativo_desde"] == "2026-08-04T09:00:00"
    ativas = lotes_repo.listar_configs(conn, so_ativas=True)
    assert ativas[0]["ativo_desde"] == "2026-08-04T09:00:00"


def test_ativo_desde_de_config_nascida_desligada_e_none(conn):
    assert _config(conn, ativo=False)["ativo_desde"] is None


# --- lote em 'erro' não está fechado: o próximo ciclo tenta de novo -------------


def _em_erro(conn, inicio=date(2026, 8, 17)):
    lote = _lote(conn, inicio)
    lotes_repo.marcar_erro(conn, lote_id=lote["id"], motivo="sem lote_config", agora=AGORA)
    return lote


def test_lote_em_erro_aceita_perna(conn):
    _fator(conn)
    lote = _em_erro(conn)
    _perna(conn, lote["id"])
    assert [p["import_id"] for p in lotes_repo.pernas_do_lote(conn, lote["id"])] == ["imp-1"]


def test_lote_em_erro_entra_nos_vencidos(conn):
    _fator(conn)
    lote = _em_erro(conn)
    v = lotes_repo.lotes_vencidos(conn, datetime(2026, 8, 24, 8, 0))
    assert [x["id"] for x in v] == [lote["id"]]


def test_lote_em_erro_fecha_e_limpa_o_motivo(conn):
    _fator(conn)
    lote = _em_erro(conn)
    assert _fechar(conn, lote["id"]) is True
    g = lotes_repo.get(conn, lote["id"])
    assert g["status"] == "fechado" and g["erro_motivo"] is None


def test_lote_em_erro_aceita_fator(conn):
    lote = _em_erro(conn)
    g = lotes_repo.informar_fator(
        conn, lote_id=lote["id"], fator=Decimal("0.07"), modo="desconto", por="s", agora=AGORA
    )
    assert g["status"] == "aberto" and g["fator_preco_aplicado"] == "0.07"


def test_trocar_janela_com_lote_em_erro_com_perna_levanta(conn):
    _config(conn)
    lote = _em_erro(conn)
    _perna(conn, lote["id"])
    with pytest.raises(lotes_repo.JanelaComLoteAbertoError):
        _config(conn, janela="quinzenal")


# --- migração de coluna ------------------------------------------------------------


def test_ativo_desde_entra_por_migracao_em_banco_antigo(tmp_path, monkeypatch):
    """Banco compartilhado criado por um build anterior da branch, sem `ativo_desde`."""
    from app.persistence import router

    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    legado = sqlite3.connect(router.shared_db_path())
    legado.executescript(
        """
        CREATE TABLE lote_config (
            env_origem_slug  TEXT PRIMARY KEY,
            env_espelho_slug TEXT NOT NULL,
            cnpj_revenda     TEXT NOT NULL,
            nome_revenda     TEXT NOT NULL,
            janela           TEXT NOT NULL DEFAULT 'semanal',
            dia_inicio       INTEGER NOT NULL DEFAULT 0,
            hora_fechamento  INTEGER NOT NULL DEFAULT 8,
            ativo            INTEGER NOT NULL DEFAULT 0,
            alterado_por     TEXT,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        );
        """
    )
    legado.close()

    with router.shared_connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(lote_config)")}
    assert "ativo_desde" in cols
    router.reset_init_cache()
