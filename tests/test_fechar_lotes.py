"""Fechamento do lote intercompany: o espelho nasce no ambiente MM, em revisão.

Sem Firebird: o fechamento só lê SQLite (lote, pernas, snapshots de origem) e
escreve a linha de `imports` do espelho no banco do ambiente MM.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.consolidators import fechamento, integracao
from app.exporters.firebird_exporter import FirebirdExportResult
from app.models.order import Order, OrderHeader
from app.persistence import context as env_context
from app.persistence import environments_repo, erp_vinculo_repo, lotes_repo, repo, router

NASMAR = "34513679000134"
MM = "35394871000111"
SEGUNDA_8H = datetime(2026, 8, 24, 8, 0)  # fecha a S34 (17/08–23/08)


@pytest.fixture
def portal_lote(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    nasmar = environments_repo.create(
        slug="nasmar",
        name="Nasmar",
        cnpj=NASMAR,
        watch_dir=str(tmp_path / "n_in"),
        output_dir=str(tmp_path / "n_out"),
        fb_path=str(tmp_path / "n.fdb"),
    )
    mm = environments_repo.create(
        slug="mm",
        name="MM Americanense",
        cnpj=MM,
        watch_dir=str(tmp_path / "m_in"),
        output_dir=str(tmp_path / "m_out"),
        fb_path=str(tmp_path / "m.fdb"),
    )
    _config(ativo=True)
    yield {"nasmar": nasmar, "mm": mm}


@pytest.fixture
def portal_lote_com_perna(portal_lote):
    _fator()
    portal_lote["lote_id"] = _perna("N1", fire=1157)
    return portal_lote


def _config(*, ativo: bool) -> None:
    with router.shared_connect() as conn:
        lotes_repo.save_config(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            cnpj_revenda=NASMAR,
            nome_revenda="Nasmar",
            janela="semanal",
            dia_inicio=0,
            hora_fechamento=8,
            ativo=ativo,
            por="teste",
            agora=datetime(2026, 8, 1, 9, 0),
        )


def _fator(valor: str = "0.07", desde: date = date(2026, 8, 3)) -> None:
    with router.shared_connect() as conn:
        lotes_repo.registrar_fator(
            conn,
            env_origem_slug="nasmar",
            fator=Decimal(valor),
            modo="desconto",
            vigente_desde=desde,
            por="teste",
            agora=datetime(2026, 8, 1, 9, 0),
        )


def _grava(
    slug: str,
    ident: str,
    *,
    imported_at: str = "2026-08-20T10:00:00",
    snapshot: dict | None = None,
    **extra,
) -> None:
    env = environments_repo.get_by_slug(slug)
    dados = {
        "id": ident,
        "source_filename": f"{ident}.pdf",
        "imported_at": imported_at,
        "order_number": ident,
        "customer_name": "DAJU",
        "status": "success",
        "portal_status": "parsed",
        "snapshot": snapshot
        if snapshot is not None
        else {
            "header": {
                "order_number": ident,
                "customer_name": "DAJU",
                "customer_cnpj": "11222333000181",
            },
            "items": [{"description": "MEIA", "quantity": 2.0, "unit_price": 16.12, "ean": "7891"}],
            "source_file": "",
        },
        **extra,
    }
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(dados)


def _perna(ident: str, *, fire: int, snapshot: dict | None = None) -> int:
    _grava("nasmar", ident, snapshot=snapshot)
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=date(2026, 8, 17),
            fim=date(2026, 8, 23),
            modo_janela="semanal",
            agora=datetime(2026, 8, 20, 10, 0),
        )
        lotes_repo.anexar_perna(
            conn,
            lote_id=lote["id"],
            import_id=ident,
            env_origem_slug="nasmar",
            fire_codigo_origem=fire,
            itens_fire=[(9001, 0)],
            pedido_cliente=ident,
            cnpj_cliente_final="11222333000181",
            razao_cliente_final="DAJU",
            agora=datetime(2026, 8, 20, 10, 0),
        )
    return lote["id"]


def _lote(lote_id: int) -> dict:
    with router.shared_connect() as conn:
        return lotes_repo.get(conn, lote_id)


def _imports(slug: str) -> list[dict]:
    with router.env_connect(slug) as conn:
        rows = conn.execute(
            "SELECT id, portal_status, customer_cnpj, customer_name, environment_id, "
            "order_number, source_filename, snapshot_json FROM imports ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


def _eventos_imported(slug: str, import_id: str) -> int:
    with router.env_connect(slug) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM order_lifecycle_events WHERE import_id = ? AND event_type = ?",
            (import_id, "imported"),
        ).fetchone()[0]


# --- transcritos da Task 11 de 25/08 -------------------------------------------


def test_lote_da_janela_vencida_fecha(portal_lote_com_perna):
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["fechados"] == 1
    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["status"] in ("fechado", "enviado")
    assert lote["import_id_espelho"] == "lote-NAS-2026-S34"
    assert lote["composicao"] == [[{"import_id": "N1", "indice": 0, "quantidade": "2.0"}]]


def test_lote_da_janela_corrente_nao_fecha(portal_lote_com_perna):
    """Só fecha janela vencida. Fechar a corrente perderia pedidos do dia."""
    r = fechamento.fechar_vencidos(agora=datetime(2026, 8, 21, 12, 0))
    assert r["fechados"] == 0
    assert _lote(portal_lote_com_perna["lote_id"])["status"] == "aberto"


def test_sem_fator_nao_fecha_e_marca_aguardando(portal_lote):
    """Sem fator, não escreve nada: preço inferido em nota fiscal é imposto errado."""
    lote_id = _perna("N1", fire=1157)
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["fechados"] == 0
    assert r["aguardando_fator"] == 1
    lote = _lote(lote_id)
    assert lote["status"] == "aguardando_fator"
    assert lote["import_id_espelho"] is None
    assert _imports("mm") == []


def test_lote_vazio_e_ignorado_sem_erro(portal_lote):
    """Semana sem pedido nenhum: nada a fazer, não explode, não cria pedido."""
    _fator()
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["fechados"] == 0
    assert r["erros"] == 0


def test_lote_sem_pernas_devolve_vazio_e_nao_muda_status(portal_lote):
    _fator()
    with router.shared_connect() as conn:
        lote = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            inicio=date(2026, 8, 17),
            fim=date(2026, 8, 23),
            modo_janela="semanal",
            agora=datetime(2026, 8, 20, 10, 0),
        )
    r = fechamento.fechar_lote(lote["id"], por="teste", agora=SEGUNDA_8H)
    assert r == {"resultado": "vazio", "import_id_espelho": None, "motivo": None}
    assert _lote(lote["id"])["status"] == "aberto"
    assert _imports("mm") == []


def test_pedido_atrasado_vai_para_o_proximo_lote(portal_lote_com_perna):
    """Pedido importado na semana fechada entra no lote seguinte: lote fechado nunca reabre."""
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    _grava("nasmar", "N9", imported_at="2026-08-21T15:00:00")
    order = Order(header=OrderHeader(order_number="N9"), items=[])
    result = FirebirdExportResult(order_number="N9", items_inserted=1, fire_codigo=1190)

    novo_id = integracao.anexar_perna_de_origem(
        "N9",
        {"imported_at": "2026-08-21T15:00:00"},
        order,
        result,
        {"slug": "nasmar"},
        agora=datetime(2026, 8, 24, 9, 0),
    )

    assert novo_id != portal_lote_com_perna["lote_id"]
    novo = _lote(novo_id)
    assert novo["chave_lote"] == "NAS-2026-S35"
    assert novo["status"] == "aberto"


def test_fator_congelado_no_fechamento(portal_lote_com_perna):
    """Mudar o parâmetro depois não reescreve lote fechado."""
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    _fator("0.12", desde=date(2026, 8, 17))
    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["fator_preco_aplicado"] == "0.07"


# --- novos ----------------------------------------------------------------------


def test_nao_fecha_antes_das_8h(portal_lote_com_perna):
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 7, 59))["fechados"] == 0


def test_fecha_atrasado_se_o_servidor_estava_fora(portal_lote_com_perna):
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 26, 10, 0))["fechados"] == 1


def test_espelho_nasce_no_ambiente_mm_em_revisao(portal_lote_com_perna):
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    (esp,) = _imports("mm")
    assert esp["id"] == "lote-NAS-2026-S34"
    assert esp["portal_status"] == "parsed"
    assert esp["customer_cnpj"] == NASMAR
    assert esp["customer_name"] == "Nasmar"
    assert esp["environment_id"] == portal_lote_com_perna["mm"]["id"]
    assert esp["order_number"] == "NAS-2026-S34"
    assert esp["source_filename"] == "Lote NAS-2026-S34.lote"
    snap = json.loads(esp["snapshot_json"])
    assert snap["header"]["customer_cnpj"] == NASMAR
    assert snap["items"][0]["unit_price"] == pytest.approx(14.9916)
    assert snap["items"][0]["quantity"] == pytest.approx(2.0)
    assert [i["id"] for i in _imports("nasmar")] == ["N1"]
    assert _eventos_imported("mm", "lote-NAS-2026-S34") == 1


def test_fechar_duas_vezes_cria_um_espelho(portal_lote_com_perna):
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    r = fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))

    assert r["fechados"] == 0
    assert len(_imports("mm")) == 1
    assert _eventos_imported("mm", "lote-NAS-2026-S34") == 1
    again = fechamento.fechar_lote(portal_lote_com_perna["lote_id"], por="teste", agora=SEGUNDA_8H)
    assert again["resultado"] == "ja_fechado"
    assert again["import_id_espelho"] == "lote-NAS-2026-S34"


def test_snapshot_ilegivel_marca_erro_e_nao_cria_espelho(portal_lote):
    _fator()
    lote_id = _perna("N1", fire=1157, snapshot={"header": {"order_number": "N1"}, "items": "lixo"})

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["erros"] == 1
    lote = _lote(lote_id)
    assert lote["status"] == "erro"
    assert "N1" in lote["erro_motivo"]
    assert _imports("mm") == []


def test_fechamento_nao_chama_o_fire(portal_lote_com_perna, monkeypatch):
    from app.exporters import firebird_exporter as fb_mod

    def _proibido(*a, **kw):
        raise AssertionError("fechamento não pode gravar no Fire")

    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _proibido)

    assert fechamento.fechar_vencidos(agora=SEGUNDA_8H)["fechados"] == 1


def test_config_inativa_ainda_fecha_lote_vencido(portal_lote_com_perna):
    """`ativo` só barra perna nova; lote que já tem perna fecha quando vence."""
    _config(ativo=False)
    assert fechamento.fechar_vencidos(agora=SEGUNDA_8H)["fechados"] == 1
    assert _lote(portal_lote_com_perna["lote_id"])["status"] == "fechado"


def test_perna_nova_durante_o_fechamento_nao_fecha_nem_marca_erro(
    portal_lote_com_perna, monkeypatch
):
    """`fechar` confere a contagem de pernas: perna que chegou entre ler e fechar
    deixa o lote aberto para o próximo ciclo, sem `erro`."""
    original = lotes_repo.fechar

    def _chega_perna_antes(conn, **kw):
        lotes_repo.anexar_perna(
            conn,
            lote_id=kw["lote_id"],
            import_id="N2",
            env_origem_slug="nasmar",
            fire_codigo_origem=1158,
            itens_fire=[],
            pedido_cliente="N2",
            cnpj_cliente_final=None,
            razao_cliente_final=None,
            agora=SEGUNDA_8H,
        )
        return original(conn, **kw)

    monkeypatch.setattr(lotes_repo, "fechar", _chega_perna_antes)

    r = fechamento.fechar_lote(portal_lote_com_perna["lote_id"], por="teste", agora=SEGUNDA_8H)

    assert r["resultado"] == "erro"
    assert "perna nova" in r["motivo"]
    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["status"] == "aberto"
    assert lote["erro_motivo"] is None
    assert lote["import_id_espelho"] is None
    assert _imports("mm") == []  # fechar recusou: espelho nenhum nesta tentativa


def test_falha_num_lote_nao_impede_os_outros(portal_lote_com_perna, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("banco caiu")

    monkeypatch.setattr(fechamento, "fechar_lote", _explode)
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["erros"] == 1


def test_falha_ao_gravar_espelho_depois_de_fechar_repara_no_proximo_ciclo(
    portal_lote_com_perna, monkeypatch
):
    original = repo.insert_import

    def _mm_fora(entry):
        if entry["id"].startswith("lote-"):
            raise RuntimeError("disco do MM cheio")
        return original(entry)

    monkeypatch.setattr(repo, "insert_import", _mm_fora)
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["fechados"] == 1
    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["status"] == "fechado"
    assert lote["import_id_espelho"] == "lote-NAS-2026-S34"
    assert lote["erro_motivo"] is None
    assert _imports("mm") == []

    monkeypatch.setattr(repo, "insert_import", original)
    r = fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))

    assert r["materializados"] == 1
    (esp,) = _imports("mm")
    assert esp["id"] == "lote-NAS-2026-S34"
    assert json.loads(esp["snapshot_json"])["items"][0]["unit_price"] == pytest.approx(14.9916)
    assert _eventos_imported("mm", "lote-NAS-2026-S34") == 1
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 10))["materializados"] == 0


def test_reparo_nao_duplica_o_evento_imported(portal_lote_com_perna, monkeypatch):
    """Linha gravada mas evento não: o reparo transiciona uma vez só."""
    original = fechamento.transition

    def _falha(*a, **kw):
        raise RuntimeError("caiu antes do evento")

    monkeypatch.setattr(fechamento, "transition", _falha)
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert len(_imports("mm")) == 1
    assert _eventos_imported("mm", "lote-NAS-2026-S34") == 0

    monkeypatch.setattr(fechamento, "transition", original)
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))["materializados"] == 1
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 10))["materializados"] == 0
    assert _eventos_imported("mm", "lote-NAS-2026-S34") == 1
    assert len(_imports("mm")) == 1


def test_fator_corrompido_marca_erro(portal_lote_com_perna):
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE intercompany_lote SET fator_preco_aplicado = 'abc' WHERE id = ?",
            (portal_lote_com_perna["lote_id"],),
        )
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["erros"] == 1
    assert _lote(portal_lote_com_perna["lote_id"])["status"] == "erro"
    assert _imports("mm") == []


# --- varredura de recuperação ---------------------------------------------------


def _enviada_sem_lote(ident: str, *, fire: int, sent: str = "2026-08-21T11:00:00") -> None:
    _grava(
        "nasmar",
        ident,
        portal_status="sent_to_fire",
        fire_codigo=fire,
        sent_to_fire_at=sent,
    )


def test_varredura_recupera_perna_enviada_que_ficou_fora_do_lote(portal_lote_com_perna):
    _enviada_sem_lote("N2", fire=1160)
    with router.env_connect("nasmar") as conn:
        erp_vinculo_repo.gravar_vinculos(
            conn,
            base="nasmar",
            erp_codigo=1160,
            import_id="N2",
            pares=[(9100, 0)],
            origem="insert",
            agora="2026-08-21T11:00:00",
        )

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 1
    assert r["fechados"] == 1
    with router.shared_connect() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, portal_lote_com_perna["lote_id"])
    por_id = {p["import_id"]: p for p in pernas}
    assert set(por_id) == {"N1", "N2"}
    assert por_id["N2"]["fire_codigo_origem"] == 1160
    assert por_id["N2"]["itens_fire"] == [(9100, 0)]
    snap = json.loads(_imports("mm")[0]["snapshot_json"])
    assert snap["items"][0]["quantity"] == pytest.approx(4.0)


def test_varredura_sem_vinculo_anexa_com_itens_vazios(portal_lote_com_perna):
    _enviada_sem_lote("N2", fire=1160)
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    with router.shared_connect() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, portal_lote_com_perna["lote_id"])
    assert {p["import_id"]: p["itens_fire"] for p in pernas}["N2"] == []


def test_varredura_ignora_parsed_found_in_fire_e_anterior_a_ativacao(
    portal_lote_com_perna,
):
    _grava("nasmar", "P1")  # parsed, nunca foi ao Fire
    _grava("nasmar", "F1", portal_status="found_in_fire", fire_codigo=1170)
    _enviada_sem_lote("V1", fire=1171, sent="2026-07-30T09:00:00")  # antes da ativação

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    with router.shared_connect() as conn:
        pernas = lotes_repo.pernas_do_lote(conn, portal_lote_com_perna["lote_id"])
    assert [p["import_id"] for p in pernas] == ["N1"]


def test_varredura_sem_lote_da_origem_nao_faz_nada(portal_lote):
    _fator()
    # enviado antes da ativação (01/08 09h): abaixo do piso, sem perna anexada
    _enviada_sem_lote("N2", fire=1160, sent="2026-07-30T09:00:00")
    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["recuperadas"] == 0
    with router.shared_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM intercompany_lote").fetchone()[0] == 0


def test_varredura_que_falha_nao_impede_o_fechamento(portal_lote_com_perna, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("banco da origem sumiu")

    monkeypatch.setattr(erp_vinculo_repo, "vinculos_do_import", _explode)
    _enviada_sem_lote("N2", fire=1160)

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    assert r["fechados"] == 1


def test_vinculos_do_import_so_insert_em_ordem(portal_lote):
    with router.env_connect("nasmar") as conn:
        erp_vinculo_repo.gravar_vinculos(
            conn,
            base="nasmar",
            erp_codigo=1,
            import_id="X",
            pares=[(12, 1), (11, 0)],
            origem="insert",
            agora="t",
        )
        erp_vinculo_repo.gravar_vinculos(
            conn,
            base="nasmar",
            erp_codigo=2,
            import_id="X",
            pares=[(13, 2)],
            origem="xlsx",
            agora="t",
        )
        assert erp_vinculo_repo.vinculos_do_import(conn, "X") == [(11, 0), (12, 1)]
        assert erp_vinculo_repo.vinculos_do_import(conn, "Y") == []


def test_habilitado_respeita_portal_lotes(monkeypatch):
    monkeypatch.setenv("PORTAL_LOTES", "0")
    assert fechamento.habilitado() is False
    monkeypatch.delenv("PORTAL_LOTES")
    assert fechamento.habilitado() is True


def _pernas_de(lote_id: int) -> list[str]:
    with router.shared_connect() as conn:
        return sorted(p["import_id"] for p in lotes_repo.pernas_do_lote(conn, lote_id))


def test_varredura_ativacao_no_meio_da_semana_nao_puxa_envios_anteriores(
    portal_lote_com_perna,
):
    """Lote religado em 20/08 9h; envio de 19/08 (mesma janela) é anterior à ativação."""
    _salvar_config(ativo=False, agora=datetime(2026, 8, 20, 8, 0))
    _salvar_config(ativo=True, agora=datetime(2026, 8, 20, 9, 0))
    _enviada_sem_lote("PRE", fire=1001, sent="2026-08-19T09:00:00")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1"]


def test_varredura_recupera_envio_depois_da_ativacao_mesmo_com_perna_posterior_anexada(
    portal_lote_com_perna,
):
    """Ativado em 01/08; o anexo do envio de 19/08 falhou e a perna N1 (20/08 10h)
    anexou depois. O piso é a ativação, não a 1ª perna anexada: PRE volta."""
    _enviada_sem_lote("PRE", fire=1001, sent="2026-08-19T09:00:00")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 1
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1", "PRE"]


def test_varredura_sem_ativo_desde_usa_a_primeira_perna_como_piso(portal_lote_com_perna):
    """Config sem o carimbo (build antigo): o piso cai na 1ª perna anexada (20/08 10h)."""
    with router.shared_connect() as conn:
        conn.execute("UPDATE lote_config SET ativo_desde = NULL")
    _enviada_sem_lote("PRE", fire=1001, sent="2026-08-19T09:00:00")
    _enviada_sem_lote("POS", fire=1002, sent="2026-08-21T09:00:00")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 1
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1", "POS"]


def test_fechar_agora_por_pessoa_roda_a_varredura_antes(portal_lote_com_perna):
    _enviada_sem_lote("N2", fire=1160)

    r = fechamento.fechar_lote(
        portal_lote_com_perna["lote_id"], por="operador@mm", agora=SEGUNDA_8H
    )

    assert r["resultado"] == "fechado"
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1", "N2"]
    composicao = _lote(portal_lote_com_perna["lote_id"])["composicao"]
    assert {o["import_id"] for linha in composicao for o in linha} == {"N1", "N2"}


def test_varredura_lote_de_backlog_nao_recua_o_piso(portal_lote_com_perna):
    """Pedido de backlog (importado em 01/07) cria lote na S27; o piso continua
    sendo a ativação (01/08), não o início da janela mais antiga."""
    _enviada_sem_lote("PRE", fire=1001, sent="2026-07-30T09:00:00")
    _grava("nasmar", "OLD", imported_at="2026-07-01T10:00:00")
    order = Order(header=OrderHeader(order_number="OLD"), items=[])
    res = FirebirdExportResult(order_number="OLD", items_inserted=1, fire_codigo=1002)
    old_lote = integracao.anexar_perna_de_origem(
        "OLD",
        {"imported_at": "2026-07-01T10:00:00"},
        order,
        res,
        {"slug": "nasmar"},
        agora=datetime(2026, 8, 20, 11, 0),
    )
    assert _lote(old_lote)["chave_lote"] == "NAS-2026-S27"

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    with router.shared_connect() as conn:
        todas = [p for lt in lotes_repo.listar(conn) for p in _pernas_de(lt["id"])]
    assert "PRE" not in todas


def test_varredura_ignora_envio_recente(portal_lote_com_perna):
    """Enviado há menos de 10 min: o request path ainda pode estar anexando."""
    _enviada_sem_lote("N2", fire=1160, sent="2026-08-24T07:55:00")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1"]


def _salvar_config(*, ativo: bool, agora: datetime) -> None:
    with router.shared_connect() as conn:
        lotes_repo.save_config(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="mm",
            cnpj_revenda=NASMAR,
            nome_revenda="Nasmar",
            janela="semanal",
            dia_inicio=0,
            hora_fechamento=8,
            ativo=ativo,
            por="teste",
            agora=agora,
        )


def test_varredura_ignora_envio_feito_com_lote_desligado(portal_lote_com_perna):
    """T0 1ª perna (20/08 10h); T1 desliga; T2 envio sem anexo; T3 religa; T4 envio."""
    _salvar_config(ativo=False, agora=datetime(2026, 8, 21, 9, 0))  # T1
    _enviada_sem_lote("N2", fire=1160, sent="2026-08-21T11:00:00")  # T2
    _salvar_config(ativo=True, agora=datetime(2026, 8, 22, 9, 0))  # T3
    _enviada_sem_lote("N3", fire=1161, sent="2026-08-22T11:00:00")  # T4

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 1
    assert _pernas_de(portal_lote_com_perna["lote_id"]) == ["N1", "N3"]


def test_primeira_anexacao_falhou_e_recuperada_pelo_ativo_desde(portal_lote):
    """Config ativada em 01/08 09h, nenhuma perna anexada; envio de 21/08 11h ficou fora."""
    _fator()
    _enviada_sem_lote("N2", fire=1160, sent="2026-08-21T11:00:00")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 1
    with router.shared_connect() as conn:
        todas = [p for lt in lotes_repo.listar(conn) for p in _pernas_de(lt["id"])]
    assert todas == ["N2"]


def test_varredura_ignora_espelho_enviado_da_propria_origem(portal_lote_com_perna):
    _enviada_sem_lote("lote-NAS-2026-S33", fire=1170)
    with router.shared_connect() as conn:
        velho = lotes_repo.lote_para(
            conn,
            env_origem_slug="nasmar",
            env_espelho_slug="nasmar",
            inicio=date(2026, 8, 10),
            fim=date(2026, 8, 16),
            modo_janela="semanal",
            agora=datetime(2026, 8, 10, 9, 0),
        )
        conn.execute(
            "UPDATE intercompany_lote SET status = 'fechado', import_id_espelho = ? WHERE id = ?",
            ("lote-NAS-2026-S33", velho["id"]),
        )

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    assert r["recuperadas"] == 0
    with router.shared_connect() as conn:
        ids = {
            p["import_id"]
            for p in lotes_repo.pernas_do_lote(conn, portal_lote_com_perna["lote_id"])
        }
    assert "lote-NAS-2026-S33" not in ids


# --- lote em 'erro' tenta de novo a cada ciclo ------------------------------------


def test_lote_em_erro_por_falta_de_config_fecha_depois_de_corrigido(portal_lote_com_perna):
    lote_id = portal_lote_com_perna["lote_id"]
    with router.shared_connect() as conn:
        conn.execute("DELETE FROM lote_config")

    r = fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    assert r["erros"] == 1
    assert _lote(lote_id)["status"] == "erro"
    motivo = _lote(lote_id)["erro_motivo"]
    assert "lote_config" in motivo

    # mesmo problema: tenta de novo e grava o mesmo motivo
    r = fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))
    assert r["erros"] == 1
    assert _lote(lote_id)["status"] == "erro"
    assert _lote(lote_id)["erro_motivo"] == motivo

    _config(ativo=True)
    r = fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 10))

    assert r["fechados"] == 1
    lote = _lote(lote_id)
    assert lote["status"] == "fechado"
    assert lote["erro_motivo"] is None
    assert [i["id"] for i in _imports("mm")] == ["lote-NAS-2026-S34"]


def test_lote_em_erro_por_ambiente_espelho_ausente_fecha_depois_de_corrigido(
    portal_lote_com_perna,
):
    lote_id = portal_lote_com_perna["lote_id"]
    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE intercompany_lote SET env_espelho_slug = 'xx' WHERE id = ?", (lote_id,)
        )
    assert fechamento.fechar_vencidos(agora=SEGUNDA_8H)["erros"] == 1
    assert _lote(lote_id)["status"] == "erro"

    with router.shared_connect() as conn:
        conn.execute(
            "UPDATE intercompany_lote SET env_espelho_slug = 'mm' WHERE id = ?", (lote_id,)
        )
    assert fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))["fechados"] == 1
    assert _lote(lote_id)["status"] == "fechado"


def test_perna_anexa_em_lote_em_erro(portal_lote_com_perna):
    lote_id = portal_lote_com_perna["lote_id"]
    with router.shared_connect() as conn:
        lotes_repo.marcar_erro(conn, lote_id=lote_id, motivo="x", agora=SEGUNDA_8H)
    _grava("nasmar", "N9", imported_at="2026-08-21T15:00:00")
    order = Order(header=OrderHeader(order_number="N9"), items=[])
    result = FirebirdExportResult(order_number="N9", items_inserted=1, fire_codigo=1190)

    novo_id = integracao.anexar_perna_de_origem(
        "N9",
        {"imported_at": "2026-08-21T15:00:00"},
        order,
        result,
        {"slug": "nasmar"},
        agora=datetime(2026, 8, 21, 16, 0),
    )

    assert novo_id == lote_id
    assert _pernas_de(lote_id) == ["N1", "N9"]


# --- reparo: espelho que já foi ao Fire -------------------------------------------


def _espelho_no_fire(fire: int) -> None:
    with router.env_connect("mm") as conn:
        conn.execute(
            "UPDATE imports SET portal_status = 'sent_to_fire', fire_codigo = ?, "
            "sent_to_fire_at = '2026-08-24T09:00:00' WHERE id = 'lote-NAS-2026-S34'",
            (fire,),
        )


def test_reparo_marca_enviado_o_espelho_que_ja_foi_ao_fire_com_rastro(portal_lote_com_perna):
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    _espelho_no_fire(5083)
    with router.env_connect("mm") as conn:
        conn.execute(
            "INSERT INTO erp_lote_perna (base_origem, erp_item_codigo_origem, erp_codigo_origem, "
            "chave_lote, produto_produtor, quantidade, linha_lote, criado_em) "
            "VALUES ('nasmar', 9001, 1157, 'NAS-2026-S34', 77, '2', 900011, 't')"
        )

    fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 9, 5))

    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["status"] == "enviado"
    assert lote["fire_codigo_espelho"] == 5083
    assert lote["lote_fiscal_ok"] is True


def test_reparo_marca_enviado_sem_rastro_com_lote_fiscal_ok_falso(portal_lote_com_perna):
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    _espelho_no_fire(5084)

    fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 9, 5))

    lote = _lote(portal_lote_com_perna["lote_id"])
    assert lote["status"] == "enviado"
    assert lote["fire_codigo_espelho"] == 5084
    assert lote["lote_fiscal_ok"] is False


# --- "sem preço no Fire" dispensado no espelho (BACKLOG 3.8) --------------------


def _espelho_do_mm() -> dict:
    env = environments_repo.get_by_slug("mm")
    with env_context.active_env(env["id"], env["slug"]):
        return repo.get_import("lote-NAS-2026-S34")


def _audits_do_espelho(tipo: str) -> list[dict]:
    env = environments_repo.get_by_slug("mm")
    with env_context.active_env(env["id"], env["slug"]):
        return [a for a in repo.list_audit("lote-NAS-2026-S34") if a["event_type"] == tipo]


def test_espelho_nasce_com_o_sem_preco_confirmado_pelo_lote(portal_lote_com_perna):
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    esp = _espelho_do_mm()
    assert esp["sem_preco_ack_by"] == "lote:NAS-2026-S34"
    assert esp["sem_preco_ack_items"] == [
        {"ean": "7891", "product_code": None, "fire_product_id": None}
    ]
    (audit,) = _audits_do_espelho("sem_preco_acknowledged")
    assert audit["detail"]["origem"] == "lote"
    assert audit["detail"]["chave_lote"] == "NAS-2026-S34"
    assert audit["detail"]["items"] == esp["sem_preco_ack_items"]


def test_ack_do_espelho_libera_so_o_sem_preco_e_mantem_a_divergencia(portal_lote_com_perna):
    from app.erp.product_check import is_blocking

    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    ack = _espelho_do_mm()["sem_preco_ack_items"]

    sem_preco = {"available": True, "items": [{"ean": "7891", "price_status": "no_price_in_fire"}]}
    blocked, detail = is_blocking(sem_preco, ack_items=ack)
    assert blocked is False
    assert detail["items_no_price_unacked"] == []

    divergente = {
        "available": True,
        "items": [
            {"ean": "7891", "price_status": "mismatch", "unit_price_order": 14.99},
            {"ean": "7891", "price_status": "no_order_price"},
        ],
    }
    blocked, detail = is_blocking(divergente, ack_items=ack)
    assert blocked is True
    assert len(detail["items_mismatch"]) == 1
    assert len(detail["items_no_order_price"]) == 1


def test_pedido_comum_da_mm_segue_pedindo_a_confirmacao(portal_lote_com_perna):
    from app.erp.product_check import is_blocking

    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    _grava("mm", "P-MM-1")
    env = environments_repo.get_by_slug("mm")
    with env_context.active_env(env["id"], env["slug"]):
        comum = repo.get_import("P-MM-1")
    assert comum["sem_preco_ack_by"] is None
    assert comum["sem_preco_ack_items"] is None

    sem_preco = {"available": True, "items": [{"ean": "7891", "price_status": "no_price_in_fire"}]}
    blocked, detail = is_blocking(sem_preco, ack_items=comum["sem_preco_ack_items"])
    assert blocked is True
    assert len(detail["items_no_price_unacked"]) == 1


def test_espelho_reparado_depois_da_falha_tambem_nasce_com_o_ack(
    portal_lote_com_perna, monkeypatch
):
    original = repo.insert_import

    def _mm_fora(entry):
        if entry["id"].startswith("lote-"):
            raise RuntimeError("disco do MM cheio")
        return original(entry)

    monkeypatch.setattr(repo, "insert_import", _mm_fora)
    fechamento.fechar_vencidos(agora=SEGUNDA_8H)
    monkeypatch.setattr(repo, "insert_import", original)
    fechamento.fechar_vencidos(agora=datetime(2026, 8, 24, 8, 5))

    assert _espelho_do_mm()["sem_preco_ack_by"] == "lote:NAS-2026-S34"
    assert len(_audits_do_espelho("sem_preco_acknowledged")) == 1


def test_linha_do_espelho_sem_ean_nem_codigo_nao_entra_no_ack(portal_lote):
    _fator()
    snap = {
        "header": {
            "order_number": "N2",
            "customer_name": "DAJU",
            "customer_cnpj": "11222333000181",
        },
        "items": [
            {"description": "MEIA", "quantity": 2.0, "unit_price": 16.12, "ean": "7891"},
            {"description": "SEM CHAVE", "quantity": 1.0, "unit_price": 10.0},
        ],
        "source_file": "",
    }
    _perna("N2", fire=1158, snapshot=snap)

    fechamento.fechar_vencidos(agora=SEGUNDA_8H)

    ack = _espelho_do_mm()["sem_preco_ack_items"]
    assert [a["ean"] for a in ack] == ["7891"]
