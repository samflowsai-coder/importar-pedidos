"""Portão único de preço no envio (app/erp/preco_trava.py)."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

import app.erp.product_check as pc
from app.erp import preco_trava
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db, environments_repo, precos_repo, repo
from app.persistence import preco_aprovacao_repo as aprov

AGORA = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
CNPJ_REDE = "11222333000144"
OUTRO_CNPJ = "99888777000166"


@pytest.fixture(autouse=True)
def ambiente(tmp_path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _order(precos=(54.0,), *, cnpj=CNPJ_REDE, delivery_cnpj=None):
    return Order(
        header=OrderHeader(order_number="P1", customer_cnpj=cnpj),
        items=[
            OrderItem(
                description="x",
                ean=f"789{i}",
                product_code=f"C{i}",
                quantity=10,
                unit_price=p,
                delivery_cnpj=delivery_cnpj,
            )
            for i, p in enumerate(precos)
        ],
    )


def _check(n=1, *, available=True, status="match", ids=None):
    ids = ids if ids is not None else [123 + i for i in range(n)]
    return {
        "available": available,
        "items": [
            {
                "ean": f"789{i}",
                "product_code": f"C{i}",
                "fire_product_id": pid,
                "price_status": status,
                "unit_price_order": 54.0,
                "fire_preco_venda": 50.0,
            }
            for i, pid in enumerate(ids)
        ],
        "summary": {},
    }


def _seed(order, **extra):
    import_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": import_id,
            "source_filename": "p.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "P1",
            "status": "success",
            "portal_status": "parsed",
            "snapshot": order.model_dump(),
        }
    )
    entry = repo.get_import(import_id)
    entry.update(extra)
    return import_id, entry


def _copia(preco_centavos=5000, *, sync=AGORA - timedelta(hours=1), produtos=("123",)):
    with db.connect() as conn:
        precos_repo.aplicar_lote(
            conn,
            redes=[{"id": "r1", "nome": "Rede X", "travada": False}],
            cnpjs=[{"cnpj": CNPJ_REDE[:8], "rede_id": "r1", "removido": False}],
            precos=[
                {
                    "rede_id": "r1",
                    "fire_produto_id": p,
                    "vigente_desde": date(2026, 1, 1),
                    "vigente_ate": None,
                    "preco_centavos": preco_centavos,
                    "removido": False,
                }
                for p in produtos
            ],
            cursor="c1",
            agora=sync,
            recursos=("precos", "redes"),
        )


def _env(modo):
    return {"id": "test-env-id", "slug": "test", "preco_trava_modo": modo}


def _live(monkeypatch, check):
    monkeypatch.setattr(pc, "check_order", lambda order, **kw: copy.deepcopy(check))


def _audits(import_id, tipo=None):
    out = repo.list_audit(import_id)
    return [a for a in out if tipo is None or a["event_type"] == tipo]


def _decidir(
    import_id, order, item_index=0, *, decisao="acordo", preco=None, motivo=None, rede=CNPJ_REDE
):
    with db.connect() as conn:
        ok = aprov.aplicar_decisao(
            conn,
            import_id=import_id,
            item_index=item_index,
            assinatura=aprov.assinatura(order.items[item_index], "123", rede),
            decisao=decisao,
            preco_final_centavos=preco,
            por="rafael",
            em=AGORA,
            motivo=motivo,
            flow_id="f1",
        )
    assert ok


def _linhas(import_id):
    with db.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM preco_aprovacao WHERE import_id = ? ORDER BY id", (import_id,)
            )
        ]


# ── Oráculo: o bloco que vivia em server.py (_send_one_to_fire/_export_one_xlsx) ──


def _hoje(check, ack_items):
    blocked, block_detail = pc.is_blocking(check, ack_items=ack_items)
    if not blocked:
        return False, None, block_detail, []
    labels = []
    if block_detail["items_mismatch"]:
        labels.append("price_mismatch")
    if block_detail["items_no_order_price"]:
        labels.append("missing_order_price")
    if block_detail["items_no_price_unacked"]:
        labels.append("no_price_unacked")
    parts = []
    if block_detail["items_mismatch"]:
        parts.append(f"{len(block_detail['items_mismatch'])} item(ns) com preço divergente do Fire")
    if block_detail["items_no_order_price"]:
        parts.append(f"{len(block_detail['items_no_order_price'])} item(ns) sem preço no pedido")
    if block_detail["items_no_price_unacked"]:
        parts.append(
            f"{len(block_detail['items_no_price_unacked'])} item(ns) sem preço no cadastro do Fire (sem confirmação)"
        )
    return True, "Pedido bloqueado: " + "; ".join(parts) + ".", block_detail, labels


def _misto():
    c = _check(3)
    c["items"][0]["price_status"] = "mismatch"
    c["items"][1]["price_status"] = "no_order_price"
    c["items"][2]["price_status"] = "no_price_in_fire"
    return c


CENARIOS_HOJE = {
    "mismatch": _check(status="mismatch"),
    "no_order_price": _check(status="no_order_price"),
    "no_price_unacked": _check(status="no_price_in_fire"),
    "os_tres": _misto(),
}


@pytest.mark.parametrize("modo", [None, "desligado"])
@pytest.mark.parametrize("nome", list(CENARIOS_HOJE))
def test_desligado_e_bit_a_bit_hoje(monkeypatch, modo, nome):
    check = CENARIOS_HOJE[nome]
    order = _order((54.0,) * len(check["items"]))
    import_id, entry = _seed(order)
    _live(monkeypatch, check)
    env = None if modo is None else _env(modo)

    d = preco_trava.decidir_envio(import_id, order, entry, env=env, agora=AGORA)

    blocked, detalhe, block_detail, labels = _hoje(check, [])
    assert blocked is True
    assert d.bloqueado is True
    assert d.reason == "price_check_failed"
    assert d.detalhe == detalhe
    assert d.block_detail == block_detail
    assert d.metric_labels == labels
    assert d.precos_aplicar == {}
    assert d.avaliacao is None
    assert _audits(import_id) == []  # o chamador grava o audit, não o portão
    assert _linhas(import_id) == []


def test_desligado_respeita_ack_e_check_indisponivel(monkeypatch):
    order = _order()
    import_id, entry = _seed(order, sem_preco_ack_items=[{"ean": "7890", "product_code": None}])
    _live(monkeypatch, _check(status="no_price_in_fire"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("desligado"), agora=AGORA)
    assert d.bloqueado is False

    _live(monkeypatch, _check(available=False, status="mismatch"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("desligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.precos_aplicar == {}


def test_observando_nao_bloqueia_e_audita(monkeypatch):
    _copia(5000)
    order = _order((54.0,))  # 54 ≠ 50 da tabela → fila
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)

    assert d.bloqueado is False
    assert d.avaliacao is not None and len(d.avaliacao.pendentes) == 1
    obs = _audits(import_id, "preco_observado")
    assert len(obs) == 1
    assert obs[0]["detail"]["itens"][0]["situacao"] == "fila"
    assert _linhas(import_id) == []  # observando não cria pendência (nada vai ao Flow)


def test_observando_mantem_a_regra_de_hoje(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check(status="mismatch")
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)

    _, detalhe, block_detail, labels = _hoje(check, [])
    assert d.bloqueado is True
    assert (d.reason, d.detalhe, d.block_detail, d.metric_labels) == (
        "price_check_failed",
        detalhe,
        block_detail,
        labels,
    )
    assert len(_audits(import_id, "preco_observado")) == 1


def test_observando_com_falha_na_avaliacao_nao_derruba(monkeypatch):
    order = _order()
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    def _boom(*a, **kw):
        raise RuntimeError("sqlite caiu")

    monkeypatch.setattr(precos_repo, "ultima_sync_ok", _boom)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("observando"), agora=AGORA)
    assert d.bloqueado is False


def test_ligado_item_na_fila_bloqueia(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(status="mismatch"))  # regra de hoje NÃO é usada em ligado

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    assert d.detalhe == (
        "Pedido aguardando preço: 1 item(ns) na fila de aprovação do Flow (Rafael/Erica)."
    )
    assert d.metric_labels == ["preco_aguardando"]
    assert d.block_detail["reason"] == "preco_aguardando"
    linhas = _linhas(import_id)
    assert len(linhas) == 1
    assert linhas[0]["situacao"] == "pendente"
    assert linhas[0]["motivo_fila"] == "acima"


def test_ligado_preco_igual_a_tabela_libera_mesmo_com_regra_de_hoje_bloqueando(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(status="mismatch"))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {}
    assert _linhas(import_id) == []


def test_ligado_aprovado_libera(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    assert preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA
    ).bloqueado

    _decidir(import_id, order, decisao="acordo", preco=5400)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {}  # o pedido tinha preço: vai o do pedido
    assert _audits(import_id, "preco_aplicado") == []


def test_ligado_recusado_bloqueia_com_motivo(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, order, decisao="recusado", motivo="fora da política da rede")

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_recusado"
    assert d.detalhe == "Preço recusado no Flow: fora da política da rede."


def test_ligado_sem_preco_no_pedido_aplica_preco_do_aprovador(monkeypatch):
    _copia(5000)
    order = _order((None,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    first = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert first.bloqueado
    assert _linhas(import_id)[0]["motivo_fila"] == "sem_preco_pedido"
    _decidir(import_id, order, decisao="preco_inicial", preco=4800)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.precos_aplicar == {0: 48.0}
    assert d.aplicados == [{"item_index": 0, "preco": 48.0, "flow_id": "f1"}]
    # o portão não afirma que o preço foi aplicado: quem grava é o chamador, após sair
    assert _audits(import_id, "preco_aplicado") == []


def test_ligado_aprovado_sem_preco_final_num_item_sem_preco_nao_libera(monkeypatch):
    _copia(5000)
    order = _order((None,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, order, decisao="acordo", preco=None)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"


def test_aplicar_precos_muta_o_preco_do_item():
    order = _order((None, 10.0))
    preco_trava.aplicar_precos(order, {0: 48.0})
    assert order.items[0].unit_price == 48.0
    assert order.items[1].unit_price == 10.0
    preco_trava.aplicar_precos(order, {})
    assert order.items[0].unit_price == 48.0


def test_fire_fora_usa_check_salvo(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "ok"


def test_fire_fora_sem_check_salvo_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=None)
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "1 item(ns) sem avaliação de preço: produto não identificado no Fire."
    assert d.avaliacao.itens[0].motivo == "produto_nao_identificado"


def test_fire_fora_com_check_salvo_de_outra_versao_do_pedido_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0, 54.0))
    import_id, entry = _seed(order, check=_check(1))  # salvo com 1 item, pedido tem 2
    _live(monkeypatch, _check(available=False, ids=[None, None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"


def test_override_de_cliente_define_a_rede(monkeypatch):
    _copia(5400)
    order = _order((54.0,), cnpj=OUTRO_CNPJ)
    _live(monkeypatch, _check())

    import_id, entry = _seed(order)
    sem = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert sem.bloqueado is True  # OUTRO_CNPJ não tem rede → fila

    import_id, entry = _seed(
        order, cliente_override_codigo=4242, cliente_override_cnpj="11.222.333/0001-44"
    )
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.cnpj_rede == CNPJ_REDE


def test_override_antigo_sem_cnpj_nao_avalia(monkeypatch):
    _copia(5400)
    # entrega na rede: sem o curto-circuito, o avaliador cairia na raiz das lojas
    order = _order((54.0,), cnpj=CNPJ_REDE, delivery_cnpj=CNPJ_REDE)
    import_id, entry = _seed(order, cliente_override_codigo=4242, cliente_override_cnpj=None)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert {i.motivo for i in d.avaliacao.itens} == {"cliente_sem_cnpj"}
    assert d.detalhe == "1 item(ns) sem avaliação de preço: cliente sem CNPJ."


def _env_grupo(slug, *, cnpj=None, intercompany=None):
    e = environments_repo.create(
        slug=slug,
        name=slug.upper(),
        watch_dir="/tmp/in",
        output_dir="/tmp/out",
        fb_path="",
        cnpj=cnpj,
    )
    if intercompany:
        environments_repo.set_intercompany_config(e["id"], cnpj=intercompany, revenda_slug=slug)
    return e


def test_cnpjs_grupo_vem_de_todos_os_ambientes():
    _env_grupo("mm", cnpj="55.666.777/0001-88")
    _env_grupo("nasmar", intercompany="44.333.222/0001-11")
    _env_grupo("vazio")
    assert preco_trava.cnpjs_grupo() == {"55666777", "44333222"}


def test_intercompany_em_ligado_ainda_passa_pela_regra_de_hoje(monkeypatch):
    _copia(5400)
    _env_grupo("nasmar", intercompany=CNPJ_REDE)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    check = _check(status="mismatch")
    _live(monkeypatch, check)

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    _, detalhe, block_detail, labels = _hoje(check, [])
    assert d.bloqueado is True
    assert (d.reason, d.detalhe, d.block_detail, d.metric_labels) == (
        "price_check_failed",
        detalhe,
        block_detail,
        labels,
    )
    assert len(_audits(import_id, "preco_intercompany_fora_da_trava")) == 1

    _live(monkeypatch, _check(status="match"))
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "fora_intercompany"


def test_intercompany_com_empresa_do_proprio_grupo(monkeypatch):
    _copia(5400)
    _env_grupo("mm", cnpj=CNPJ_REDE)
    order = _order((99.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is False
    assert d.avaliacao.itens[0].situacao == "fora_intercompany"


def test_ligado_pedido_sem_itens_bloqueia(monkeypatch):
    _copia(5400)
    order = _order(())
    import_id, entry = _seed(order, check=_check(0))
    _live(monkeypatch, _check(0))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "Pedido sem itens: nada para avaliar."


def test_intercompany_com_fire_fora_e_sem_check_salvo_bloqueia_com_mensagem_propria(monkeypatch):
    _copia(5400)
    _env_grupo("nasmar", intercompany=CNPJ_REDE)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=None)
    _live(monkeypatch, _check(available=False, ids=[None]))

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert d.detalhe == "Pedido intercompany: não foi possível checar no Fire agora; tente de novo."


def test_aprovacao_de_uma_rede_nao_vale_depois_do_override_para_outra(monkeypatch):
    _copia(5000)
    with db.connect() as conn:
        precos_repo.aplicar_lote(
            conn,
            redes=[{"id": "r2", "nome": "Rede Y", "travada": False}],
            cnpjs=[{"cnpj": "55666777", "rede_id": "r2", "removido": False}],
            precos=[
                {
                    "rede_id": "r2",
                    "fire_produto_id": "123",
                    "vigente_desde": date(2026, 1, 1),
                    "vigente_ate": None,
                    "preco_centavos": 5000,
                    "removido": False,
                }
            ],
            cursor="c2",
            agora=AGORA - timedelta(hours=1),
            recursos=("precos", "redes"),
        )
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, order, decisao="acordo", preco=5400)
    assert not preco_trava.decidir_envio(
        import_id, order, entry, env=_env("ligado"), agora=AGORA
    ).bloqueado

    entry["cliente_override_codigo"] = 777
    entry["cliente_override_cnpj"] = "55666777000188"
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    assert d.reason == "preco_aguardando"
    linhas = _linhas(import_id)
    assert linhas[0]["motivo_cancelamento"] == "substituido"
    assert linhas[1]["situacao"] == "pendente"


def test_ligado_substitui_obsoletas_com_o_mapa_completo(monkeypatch):
    _copia(5000, produtos=("123", "124"))
    order = _order((50.0, 54.0))  # item 0 ok, item 1 na fila
    import_id, entry = _seed(order)
    _live(monkeypatch, _check(2))

    chamadas = []
    original = aprov.substituir_obsoletas

    def _spy(conn, imp, mapa, *, em):
        chamadas.append(dict(mapa))
        return original(conn, imp, mapa, em=em)

    monkeypatch.setattr(aprov, "substituir_obsoletas", _spy)
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)

    assert len(chamadas) == 1
    assert chamadas[0] == {
        0: aprov.assinatura(order.items[0], "123", CNPJ_REDE),
        1: aprov.assinatura(order.items[1], "124", CNPJ_REDE),
    }


def test_ligado_item_editado_substitui_a_pendencia(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())
    preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    _decidir(import_id, order, decisao="acordo", preco=5400)

    editado = _order((55.0,))
    d = preco_trava.decidir_envio(import_id, editado, entry, env=_env("ligado"), agora=AGORA)

    assert d.bloqueado is True
    linhas = _linhas(import_id)
    assert linhas[0]["motivo_cancelamento"] == "substituido"
    assert linhas[1]["situacao"] == "pendente"


def test_ligado_copia_desatualizada_bloqueia(monkeypatch):
    _copia(5400, sync=AGORA - timedelta(hours=25))
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"
    assert "mais de 24 h" in d.detalhe


def test_ligado_falha_na_avaliacao_bloqueia(monkeypatch):
    _copia(5400)
    order = _order((54.0,))
    import_id, entry = _seed(order)
    _live(monkeypatch, _check())

    def _boom(*a, **kw):
        raise RuntimeError("sqlite caiu")

    monkeypatch.setattr(precos_repo, "ultima_sync_ok", _boom)
    d = preco_trava.decidir_envio(import_id, order, entry, env=_env("ligado"), agora=AGORA)
    assert d.bloqueado is True
    assert d.reason == "preco_nao_avaliado"


def test_avaliar_entry_sem_fire_sem_audit_e_sem_pendencia(monkeypatch):
    _copia(5000)
    order = _order((54.0,))
    import_id, entry = _seed(order, check=_check())

    def _sem_fire(*a, **kw):
        raise AssertionError("avaliar_entry não pode consultar o Fire")

    monkeypatch.setattr(pc, "check_order", _sem_fire)

    assert preco_trava.avaliar_entry(entry, order, env=_env("desligado"), agora=AGORA) is None
    assert preco_trava.avaliar_entry(entry, order, env=None, agora=AGORA) is None
    av = preco_trava.avaliar_entry(entry, order, env=_env("ligado"), agora=AGORA)
    assert av is not None and av.itens[0].situacao == "fila"
    assert _audits(import_id) == []
    assert _linhas(import_id) == []


# ── Caminho web: audit e métrica idênticos aos de hoje em `desligado` ──────────


def _contadores():
    from app.observability import metrics

    return {
        r: metrics.price_check_blocks_total.labels(reason=r)._value.get()
        for r in ("price_mismatch", "missing_order_price", "no_price_unacked")
    }


@pytest.mark.parametrize(
    ("rota", "evento"),
    [("send-to-fire", "send_to_fire_blocked"), ("export-xlsx", "xlsx_export_blocked")],
)
def test_desligado_grava_mesmo_audit_e_metrica(monkeypatch, rota, evento):
    from fastapi.testclient import TestClient

    from app.web.server import app

    client = TestClient(app, raise_server_exceptions=False)
    check = _misto()
    order = _order((54.0, 54.0, 54.0))
    import_id, _ = _seed(order)
    _live(monkeypatch, check)
    antes = _contadores()

    r = client.post(f"/api/imported/{import_id}/{rota}")

    _, detalhe, block_detail, _labels = _hoje(check, [])
    assert r.status_code == 409
    assert r.json()["detail"] == detalhe
    bloqueios = _audits(import_id, evento)
    assert len(bloqueios) == 1
    assert bloqueios[0]["detail"] == block_detail
    depois = _contadores()
    assert {k: depois[k] - antes[k] for k in depois} == {
        "price_mismatch": 1,
        "missing_order_price": 1,
        "no_price_unacked": 1,
    }
