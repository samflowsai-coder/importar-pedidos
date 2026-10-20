"""Task 3b (R18): chave repetida no mesmo pedido não discrimina — escolha pontual.

Regressão real (20/10): a Beira Rio manda o mesmo código numa linha por cor, e o
Fire tem um só produto-modelo com esse código. A chave exata dava `ok` com o
modelo em todas as linhas. Agora a chave que se repete com descrições diferentes
não decide sozinha, e a pessoa escolhe o produto de cada linha (só naquele pedido).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.erp import preco_trava, product_check, produto_trava
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import db, produto_depara_repo, produto_vinculo_repo, repo
from app.web.server import app
from tests.test_product_check import _make_fb_ctx_chave

client = TestClient(app, raise_server_exceptions=True)

CODIGO = "1000660704"
DESC_BRANCO = "CHINELO LISA BRANCO/BRANCO/BRANCO 23/27"
DESC_PRETO = "CHINELO LISA BRANCO/BRANCO/PRETO 23/27"
MODELO = (CODIGO, 2000, "CHINELO LISA", 10.0, "PAR", "Nao", CODIGO)
MOTIVO = (
    f"O código {CODIGO} aparece em 2 linhas com descrições diferentes; "
    "escolha o produto de cada linha."
)
LIGADO = {"slug": "mm", "produto_chave_modo": "ligado"}
# O check de verdade (com Fire fake), mesmo quando o teste troca o check_order.
_CHECK_REAL = product_check.check_order


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


def _order(*itens: dict) -> Order:
    return Order(
        header=OrderHeader(
            order_number="14330456", customer_cnpj="00000000000100", customer_name="BR"
        ),
        items=[OrderItem(quantity=1.0, unit_price=10.0, **kw) for kw in itens],
    )


def _beira_rio(**extra_branco) -> Order:
    return _order(
        {"product_code": CODIGO, "description": DESC_BRANCO, **extra_branco},
        {"product_code": CODIGO, "description": DESC_PRETO},
    )


def _check(order, *, escolhas=None, **rows):
    with patch("app.erp.product_check.FirebirdConnection") as mock_fb:
        mock_fb.return_value.is_configured.return_value = True
        ctx, _ = _make_fb_ctx_chave(**rows)
        mock_fb.return_value.connect.return_value = ctx
        return _CHECK_REAL(order, escolhas=escolhas)


# ── check: a chave repetida não discrimina ───────────────────────────────────


def test_codigo_repetido_com_descricoes_diferentes_vira_chave_repetida():
    report = _check(_beira_rio(), code_rows=[MODELO])
    for item in report["items"]:
        produto = item["produto"]
        assert produto["estado"] == "chave_repetida"
        assert produto["fire_seq"] is None
        assert produto["fonte"] is None
        assert produto["motivo"] == MOTIVO
        assert [c["seq"] for c in produto["candidatos"]] == [2000]
        # campos legados intocados
        assert item["match"] is True
        assert item["fire_product_id"] == 2000
    assert report["summary"]["items_produto_pendente"] == 2


def test_codigo_repetido_com_descricao_igual_continua_ok():
    order = _order(
        {"product_code": CODIGO, "description": "chinelo  lisá branco"},
        {"product_code": CODIGO, "description": "CHINELO LISA BRANCO"},
    )
    report = _check(order, code_rows=[MODELO])
    for item in report["items"]:
        assert (item["produto"]["estado"], item["produto"]["fire_seq"]) == ("ok", 2000)
        assert item["produto"]["fonte"] == "codprod_altern"


def test_ean_distinto_por_linha_mantem_ok_mesmo_com_codigo_repetido():
    order = _order(
        {"product_code": CODIGO, "ean": "7890000000011", "description": DESC_BRANCO},
        {"product_code": CODIGO, "ean": "7890000000028", "description": DESC_PRETO},
    )
    report = _check(
        order,
        code_rows=[MODELO],
        ean_rows=[
            ("7890000000011", 2000, "CHINELO LISA", 10.0, "PAR", "Nao", CODIGO),
            ("7890000000028", 2000, "CHINELO LISA", 10.0, "PAR", "Nao", CODIGO),
        ],
    )
    for item in report["items"]:
        produto = item["produto"]
        assert (produto["estado"], produto["fire_seq"], produto["fonte"]) == ("ok", 2000, "ean")


def test_ean_repetido_com_descricoes_diferentes_vira_chave_repetida():
    order = _order(
        {"ean": "7890000000011", "description": DESC_BRANCO},
        {"ean": "7890000000011", "description": DESC_PRETO},
    )
    report = _check(
        order, ean_rows=[("7890000000011", 2000, "CHINELO LISA", 10.0, "PAR", "Nao", CODIGO)]
    )
    assert report["items"][0]["produto"]["estado"] == "chave_repetida"
    assert report["items"][0]["produto"]["motivo"] == (
        "O EAN 7890000000011 aparece em 2 linhas com descrições diferentes; "
        "escolha o produto de cada linha."
    )


def test_codigo_repetido_nao_mexe_em_quem_ja_nao_era_ok():
    report = _check(_beira_rio())  # código fora do Fire
    assert {i["produto"]["estado"] for i in report["items"]} == {"a_identificar"}


# ── check: escolha pontual ───────────────────────────────────────────────────


BRANCO = (2010, 2010, "CHINELO LISA BRANCO", 10.0, "PAR", "Nao", "1000660710")


def test_escolha_pontual_vira_ok_so_naquele_import():
    order = _beira_rio()
    com = _check(order, escolhas={0: 2010}, code_rows=[MODELO], seq_rows=[BRANCO])
    p0, p1 = (i["produto"] for i in com["items"])
    assert (p0["estado"], p0["fire_seq"], p0["fonte"]) == ("ok", 2010, "escolha_pontual")
    assert [c["seq"] for c in p0["candidatos"]] == [2010]
    assert p1["estado"] == "chave_repetida"  # cada linha é uma escolha

    sem = _check(order, code_rows=[MODELO], seq_rows=[BRANCO])
    assert {i["produto"]["estado"] for i in sem["items"]} == {"chave_repetida"}

    # outro import com o mesmo pedido não herda a escolha
    a = _seed_import(order)
    b = _seed_import(order)
    repo.set_produto_escolha(a, 0, fire_seq=2010, por="x@y", descricao="CHINELO LISA BRANCO")
    assert product_check.escolhas_do_import(repo.get_import(a)) == {0: 2010}
    assert product_check.escolhas_do_import(repo.get_import(b)) == {}


def test_escolha_pontual_para_seq_bloqueado_e_vinculo_invalido():
    bloqueado = (2010, 2010, "CHINELO LISA BRANCO", 10.0, "PAR", "Sim", "1000660710")
    report = _check(_beira_rio(), escolhas={0: 2010}, code_rows=[MODELO], seq_rows=[bloqueado])
    produto = report["items"][0]["produto"]
    assert produto["estado"] == "vinculo_invalido"
    assert produto["fire_seq"] is None
    assert produto["motivo"] == (
        "A escolha desta linha aponta para o produto 2010, que não existe mais ou está bloqueado."
    )

    sumido = _check(_beira_rio(), escolhas={0: 2010}, code_rows=[MODELO])
    assert sumido["items"][0]["produto"]["estado"] == "vinculo_invalido"


def test_escolhas_do_import_ignora_lixo():
    assert product_check.escolhas_do_import(None) == {}
    assert product_check.escolhas_do_import({"produto_escolhido": None}) == {}
    entry = {
        "produto_escolhido": {
            "0": {"fire_seq": 10},
            "x": {"fire_seq": 11},
            "2": {"fire_seq": "abc"},
            "3": "lixo",
            "4": {"fire_seq": "12"},
        }
    }
    assert product_check.escolhas_do_import(entry) == {0: 10, 4: 12}


# ── trava e saída ────────────────────────────────────────────────────────────


def _chave_repetida():
    return {
        "estado": "chave_repetida",
        "fire_seq": None,
        "fonte": None,
        "motivo": MOTIVO,
        "candidatos": [],
    }


def test_trava_ligada_bloqueia_chave_repetida():
    check = {
        "available": True,
        "produto_conferencia_incompleta": False,
        "items": [{"product_code": CODIGO, "produto": _chave_repetida()}],
    }
    tp = produto_trava.avaliar(check, env=LIGADO, export_mode="db")
    assert tp.bloqueado is True
    assert tp.reason == "produto_a_identificar"
    assert tp.itens == [{"index": 0, "estado": "chave_repetida", "motivo": MOTIVO}]
    # desligado: nada muda
    assert produto_trava.avaliar(check, env=None, export_mode="db").bloqueado is False


def test_saida_usa_seq_da_escolha_pontual(monkeypatch):
    order = _beira_rio()
    vistos: list = []

    def fake(o, *, env=None, escolhas=None):
        vistos.append(escolhas)
        return _check(o, escolhas=escolhas, code_rows=[MODELO], seq_rows=[BRANCO])

    monkeypatch.setattr(product_check, "check_order", fake)
    monkeypatch.setattr(preco_trava, "_export_mode", lambda: "db")
    entry = {"produto_escolhido": {"0": {"fire_seq": 2010}, "1": {"fire_seq": 2010}}}
    d = preco_trava.decidir_envio(
        "id", order, entry, env=LIGADO, agora=datetime.now(UTC), export_mode="db"
    )
    assert vistos == [{0: 2010, 1: 2010}]
    assert d.bloqueado is False
    vinculos, mapa = produto_trava.produtos_decididos(d.check)
    assert mapa == {0: 2010, 1: 2010}
    assert vinculos[0]["fire_codigo"] == "1000660710"


# ── rota: vincular em chave repetida grava a escolha pontual ─────────────────


def _seed_import(order: Order, produtos: list[dict] | None = None) -> str:
    entry_id = str(uuid.uuid4())
    snap = order.model_dump()
    repo.insert_import(
        {
            "id": entry_id,
            "source_filename": "br.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": order.header.order_number,
            "customer": order.header.customer_name,
            "customer_cnpj": order.header.customer_cnpj,
            "status": "success",
            "portal_status": "parsed",
            "snapshot": snap,
            "check": {
                "available": True,
                "items": [
                    {"product_code": it.product_code, "ean": it.ean, "produto": p}
                    for it, p in zip(
                        order.items, produtos or [_chave_repetida()] * len(order.items), strict=True
                    )
                ],
            },
        }
    )
    return entry_id


def _seed_catalogo():
    from app.erp.catalog_extract import ProdutoFireDTO
    from app.persistence import catalogo_fire_repo

    dto = ProdutoFireDTO(
        fire_produto_id="2010",
        codigo="2010",
        nome="CHINELO LISA BRANCO",
        unidade="PAR",
        ean=None,
        ativo=True,
        tipo="simples",
        codigo_alternativo="1000660710",
    )
    with db.connect() as conn:
        catalogo_fire_repo.replace_all(conn, [dto], extraido_em="2026-10-20T00:00:00")


def test_vincular_em_chave_repetida_grava_pontual_e_nao_grava_v1_v2(monkeypatch):
    chamadas: list = []

    def fake(o, *, env=None, escolhas=None):
        chamadas.append(escolhas)
        return _check(o, escolhas=escolhas, code_rows=[MODELO], seq_rows=[BRANCO])

    monkeypatch.setattr(product_check, "check_order", fake)
    _seed_catalogo()
    entry_id = _seed_import(_beira_rio())

    r = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": "2010"},
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pontual"] is True
    assert body["vinculos"] == []
    assert "produto_trava" in body
    p0, p1 = (i["produto"] for i in body["check"]["items"])
    assert (p0["estado"], p0["fire_seq"], p0["fonte"]) == ("ok", 2010, "escolha_pontual")
    assert p1["estado"] == "chave_repetida"
    assert chamadas == [{0: 2010}]

    with db.connect() as conn:
        assert produto_vinculo_repo.listar(conn) == []
        ckey = produto_depara_repo.client_key("00000000000100", "BR")
        assert produto_depara_repo.list_for_client(conn, ckey) == []

    salvo = repo.get_import(entry_id)
    escolha = salvo["produto_escolhido"]["0"]
    assert escolha["fire_seq"] == 2010
    assert escolha["descricao"] == "CHINELO LISA BRANCO"
    assert escolha["por"]
    assert escolha["em"]
    # o check guardado reflete a escolha (re-open e fila)
    assert salvo["check"]["items"][0]["produto"]["fonte"] == "escolha_pontual"

    (audit,) = [
        a for a in repo.list_audit(entry_id) if a["event_type"] == "produto_escolha_pontual"
    ]
    assert audit["detail"]["item_index"] == 0
    assert audit["detail"]["fire_produto_id"] == 2010
    assert audit["detail"]["pedido"] == "14330456"
    assert not [
        a for a in repo.list_audit(entry_id) if a["event_type"].startswith("produto_vinculo")
    ]


def test_vincular_com_chave_repetida_no_pedido_e_pontual_mesmo_com_check_antigo(monkeypatch):
    """Check guardado de antes da regra (item `ok`): a chave se repete no pedido com
    descrições diferentes, então a escolha é pontual sem recalcular nada antes."""
    chamadas: list = []

    def fake(o, *, env=None, escolhas=None):
        chamadas.append(escolhas)
        return _check(o, escolhas=escolhas, code_rows=[MODELO], seq_rows=[BRANCO])

    monkeypatch.setattr(product_check, "check_order", fake)
    _seed_catalogo()
    ok = {
        "estado": "ok",
        "fire_seq": 2000,
        "fonte": "codprod_altern",
        "motivo": "",
        "candidatos": [],
    }
    entry_id = _seed_import(_beira_rio(), produtos=[ok, ok])

    r = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 1, "fire_produto_id": "2010"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["pontual"] is True
    assert chamadas == [{1: 2010}]
    with db.connect() as conn:
        assert produto_vinculo_repo.listar(conn) == []


@pytest.mark.parametrize("estado", ["a_identificar", "ambiguo", "chaves_discordam"])
def test_vincular_com_chave_repetida_em_qualquer_estado_nao_grava_vinculo(monkeypatch, estado):
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda o, *, env=None, escolhas=None: _check(o, escolhas=escolhas, seq_rows=[BRANCO]),
    )
    _seed_catalogo()
    pendente = {"estado": estado, "fire_seq": None, "fonte": None, "motivo": "x", "candidatos": []}
    entry_id = _seed_import(_beira_rio(), produtos=[pendente, pendente])

    r = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": "2010"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["pontual"] is True
    with db.connect() as conn:
        assert produto_vinculo_repo.listar(conn) == []
        ckey = produto_depara_repo.client_key("00000000000100", "BR")
        assert produto_depara_repo.list_for_client(conn, ckey) == []
    assert product_check.escolhas_do_import(repo.get_import(entry_id)) == {0: 2010}


def test_trocar_escolha_audita_o_seq_antigo(monkeypatch):
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda o, *, env=None, escolhas=None: {"available": False, "items": []},
    )
    _seed_catalogo()
    entry_id = _seed_import(_beira_rio())
    repo.set_produto_escolha(entry_id, 0, fire_seq=2008, por="x@y", descricao="SORTIDO")

    r = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": "2010"},
    )

    assert r.status_code == 200, r.text
    (audit,) = [
        a for a in repo.list_audit(entry_id) if a["event_type"] == "produto_escolha_pontual"
    ]
    assert audit["detail"]["fire_seq_antigo"] == 2008
    assert audit["detail"]["fire_produto_id"] == 2010
    escolha = repo.get_import(entry_id)["produto_escolhido"]["0"]
    assert (escolha["fire_seq"], escolha["codigo"]) == (2010, "1000660710")


def test_vincular_normal_mantem_a_escolha_pontual_de_outra_linha(monkeypatch):
    """Escolhe a linha 0 (pontual) e vincula a linha 2 (outro código, normal): o
    check regravado continua com a linha 0 escolhida."""
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda o, *, env=None, escolhas=None: _check(
            o, escolhas=escolhas, code_rows=[MODELO], seq_rows=[BRANCO]
        ),
    )
    _seed_catalogo()
    order = _order(
        {"product_code": CODIGO, "description": DESC_BRANCO},
        {"product_code": CODIGO, "description": DESC_PRETO},
        {"product_code": "OUTRO-1", "description": "SANDALIA"},
    )
    entry_id = _seed_import(
        order, produtos=[_chave_repetida(), _chave_repetida(), _chave_repetida()]
    )
    r0 = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": "2010"},
    )
    assert r0.json()["pontual"] is True

    r2 = client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 2, "fire_produto_id": "2010"},
    )

    assert r2.status_code == 200, r2.text
    assert "pontual" not in r2.json()
    salvo = repo.get_import(entry_id)["check"]["items"][0]["produto"]
    assert (salvo["estado"], salvo["fonte"]) == ("ok", "escolha_pontual")


def test_escolha_pontual_vai_nos_campos_legados():
    report = _check(_beira_rio(), escolhas={0: 2010}, code_rows=[MODELO], seq_rows=[BRANCO])
    escolhido, outro = report["items"]
    assert escolhido["match"] is True
    assert escolhido["match_source"] == "escolha_pontual"
    assert escolhido["fire_product_id"] == 2010
    assert escolhido["fire_description"] == "CHINELO LISA BRANCO"
    assert escolhido["price_status"] == "match"
    assert (outro["fire_product_id"], outro["match_source"]) == (2000, "codprod_altern")
    assert report["summary"]["items_matched"] == 2
    # sem escolha: campos legados de sempre
    sem = _check(_beira_rio(), code_rows=[MODELO], seq_rows=[BRANCO])
    assert sem["items"][0]["fire_product_id"] == 2000


def test_produto_escolhido_podre_nao_derruba_e_e_regravado():
    entry_id = _seed_import(_beira_rio())
    with db.connect() as conn:
        conn.execute(
            "UPDATE imports SET produto_escolhido_json = ? WHERE id = ?", ("{podre", entry_id)
        )
    assert repo.get_import(entry_id)["produto_escolhido"] == {}
    assert [e["produto_escolhido"] for e in repo.list_imports(portal_status="parsed")] == [{}]

    repo.set_produto_escolha(entry_id, 1, fire_seq=2010, por="x@y", descricao="B")
    assert product_check.escolhas_do_import(repo.get_import(entry_id)) == {1: 2010}

    with db.connect() as conn:
        conn.execute("UPDATE imports SET produto_escolhido_json = '[1]' WHERE id = ?", (entry_id,))
    assert repo.get_import(entry_id)["produto_escolhido"] == {}
    repo.set_produto_escolha(entry_id, 0, fire_seq=2010, por="x@y", descricao="B")
    assert product_check.escolhas_do_import(repo.get_import(entry_id)) == {0: 2010}


# ── R20: fora de `ligado` a saída também usa a escolha pontual ────────────────


def _saida_fora_de_ligado(monkeypatch, tmp_path, modo: str, export_mode: str):
    from app import config as app_config
    from app.exporters import erp_exporter as erp_mod
    from app.exporters import firebird_exporter as fb_mod

    monkeypatch.setattr(preco_trava, "env_trava_produto", lambda env: {"produto_chave_modo": modo})
    monkeypatch.setattr(product_check, "check_order", lambda o, **kw: {"available": False})
    monkeypatch.setattr(product_check, "is_blocking", lambda check, ack_items=None: (False, {}))
    monkeypatch.setattr(
        app_config,
        "load",
        lambda: {
            "watch_dir": str(tmp_path),
            "output_dir": str(tmp_path),
            "export_mode": export_mode,
        },
    )
    recebido: dict = {}

    def _fb(self, order, *, override_client_id=None, produtos_por_item=None):
        recebido["seq"] = produtos_por_item
        recebido["fb_itens"] = [(it.product_code, it.obs) for it in order.items]
        return fb_mod.FirebirdExportResult(
            order_number=order.header.order_number, items_inserted=2, fire_codigo=4242
        )

    def _xlsx(self, order, output_dir):
        recebido["xlsx_itens"] = [(it.product_code, it.ean, it.obs) for it in order.items]
        return []

    monkeypatch.setattr(fb_mod.FirebirdExporter, "export", _fb)
    monkeypatch.setattr(erp_mod.ERPExporter, "export", _xlsx)
    entry_id = _seed_import(_beira_rio(ean="7890000000011"))
    repo.set_produto_escolha(
        entry_id, 0, fire_seq=2010, por="x@y", descricao="BRANCO", codigo="1000660710"
    )
    return entry_id, recebido


@pytest.mark.parametrize("modo", ["desligado", "observando"])
@pytest.mark.parametrize("export_mode", ["db", "both"])
def test_send_to_fire_fora_de_ligado_usa_a_escolha_pontual(
    monkeypatch, tmp_path, modo, export_mode
):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, export_mode)

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == 200, r.text
    assert recebido["seq"] == {0: 2010}  # linha não escolhida: busca normal
    assert recebido["fb_itens"][0] == ("1000660710", f"ref cliente: {CODIGO}")
    assert recebido["fb_itens"][1] == (CODIGO, None)
    if export_mode == "both":
        assert recebido["xlsx_itens"][0] == ("1000660710", None, f"ref cliente: {CODIGO}")
        assert recebido["xlsx_itens"][1] == (CODIGO, None, None)


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_export_xlsx_fora_de_ligado_usa_a_escolha_pontual(monkeypatch, tmp_path, modo):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, "xlsx")

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")

    assert r.status_code == 200, r.text
    assert recebido["xlsx_itens"][0] == ("1000660710", None, f"ref cliente: {CODIGO}")
    assert recebido["xlsx_itens"][1] == (CODIGO, None, None)


def test_recheck_e_ack_passam_as_escolhas_do_import(monkeypatch):
    from app.web import server

    vistos: list = []

    def fake(o, *, env=None, escolhas=None):
        vistos.append(escolhas)
        return {"available": True, "items": [], "produto_conferencia_incompleta": False}

    monkeypatch.setattr(product_check, "check_order", fake)
    order = _beira_rio()
    entry_id = _seed_import(order)
    repo.set_produto_escolha(entry_id, 1, fire_seq=2010, por="x@y", descricao="B")

    server._rechecar_pedidos_do_escopo("outro", "raiz:00000000", [("codigo", CODIGO)], None)
    assert vistos == [{1: 2010}]


# ── R21: escolha pontual sem CODPROD_ALTERN nunca sai em planilha ─────────────

MOTIVO_SEM_CODIGO = (
    "O produto 2010 escolhido para a linha 1 não tem código no Fire; a planilha não "
    "consegue apontá-lo. Use o envio direto ou ajuste o cadastro."
)


def _sem_codigo_na_escolha(entry_id):
    repo.set_produto_escolha(entry_id, 0, fire_seq=2010, por="x@y", descricao="BRANCO")


@pytest.mark.parametrize("modo", ["desligado", "observando"])
@pytest.mark.parametrize("export_mode", ["xlsx", "both"])
def test_send_planilha_recusa_escolha_sem_codigo_fora_de_ligado(
    monkeypatch, tmp_path, modo, export_mode
):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, export_mode)
    _sem_codigo_na_escolha(entry_id)

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == MOTIVO_SEM_CODIGO
    assert recebido == {}  # nada saiu
    (audit,) = [a for a in repo.list_audit(entry_id) if a["event_type"] == "send_to_fire_blocked"]
    assert audit["detail"]["produto"][0]["estado"] == "sem_codigo_fire"


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_export_xlsx_recusa_escolha_sem_codigo_fora_de_ligado(monkeypatch, tmp_path, modo):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, "xlsx")
    _sem_codigo_na_escolha(entry_id)

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == MOTIVO_SEM_CODIGO
    assert "xlsx_itens" not in recebido


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_send_db_aceita_escolha_sem_codigo_pelo_seq(monkeypatch, tmp_path, modo):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, "db")
    _sem_codigo_na_escolha(entry_id)

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == 200, r.text
    assert recebido["seq"] == {0: 2010}
    # nunca o SEQ em texto: fica o código do cliente (o insert entra pelo SEQ)
    assert recebido["fb_itens"][0] == (CODIGO, f"ref cliente: {CODIGO}")


def _check_vivo_com(codprod):
    def fake(o, **kw):
        return {
            "available": True,
            "items": [
                {
                    "product_code": CODIGO,
                    "produto": {
                        "estado": "ok",
                        "fire_seq": 2010,
                        "fonte": "escolha_pontual",
                        "motivo": "",
                        "candidatos": [
                            {
                                "seq": 2010,
                                "descricao": "B",
                                "codprod_altern": codprod,
                                "bloqueado": False,
                                "unidade": "PAR",
                                "ean": None,
                            }
                        ],
                    },
                },
                {"product_code": CODIGO, "produto": _chave_repetida()},
            ],
        }

    return fake


def test_planilha_prefere_o_codigo_vivo_do_fire(monkeypatch, tmp_path):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, "desligado", "xlsx")
    monkeypatch.setattr(product_check, "check_order", _check_vivo_com("VIVO-10"))

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")

    assert r.status_code == 200, r.text
    assert recebido["xlsx_itens"][0][0] == "VIVO-10"


def test_planilha_recusa_quando_o_fire_vivo_nao_tem_codigo(monkeypatch, tmp_path):
    """O catálogo guardado tinha código, mas o Fire agora não tem: vale o Fire."""
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, "desligado", "xlsx")
    monkeypatch.setattr(product_check, "check_order", _check_vivo_com(None))

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == MOTIVO_SEM_CODIGO


@pytest.mark.parametrize(("export_mode", "status"), [("xlsx", 409), ("both", 409), ("db", 200)])
def test_ligado_escolha_sem_codigo_trava_planilha_e_passa_no_insert(
    monkeypatch, tmp_path, export_mode, status
):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, "ligado", export_mode)
    sem_codigo = (2010, 2010, "CHINELO LISA BRANCO", 10.0, "PAR", "Nao", None)
    monkeypatch.setattr(
        product_check,
        "check_order",
        lambda o, *, env=None, escolhas=None: _check(
            o, escolhas=escolhas, code_rows=[MODELO], seq_rows=[sem_codigo]
        ),
    )
    _sem_codigo_na_escolha(entry_id)
    repo.set_produto_escolha(entry_id, 1, fire_seq=2010, por="x@y", descricao="BRANCO")

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == status, r.text
    if status == 409:
        assert recebido == {}
        (audit,) = [
            a for a in repo.list_audit(entry_id) if a["event_type"] == "send_to_fire_blocked"
        ]
        assert {x["estado"] for x in audit["detail"]["produto"]} == {"sem_codigo_fire"}
    else:
        assert recebido["seq"] == {0: 2010, 1: 2010}


# ── M1: escolha pontual cujo SEQ ficou bloqueado/sumiu não sai em nenhum modo ──

MOTIVO_ESCOLHA_INATIVA = (
    "O produto 2010 escolhido para a linha 1 não está mais ativo no Fire; escolha outro."
)


def _check_vivo_escolha_invalida(o, **kw):  # noqa: ARG001
    return {
        "available": True,
        "items": [
            {
                "product_code": CODIGO,
                "produto": {
                    "estado": "vinculo_invalido",
                    "fire_seq": None,
                    "fonte": None,
                    "motivo": "A escolha desta linha aponta para o produto 2010, que não existe "
                    "mais ou está bloqueado.",
                    "candidatos": [],
                },
            },
            {"product_code": CODIGO, "produto": _chave_repetida()},
        ],
    }


@pytest.mark.parametrize("modo", ["desligado", "observando"])
@pytest.mark.parametrize("export_mode", ["db", "xlsx", "both"])
def test_send_recusa_escolha_com_produto_inativo_fora_de_ligado(
    monkeypatch, tmp_path, modo, export_mode
):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, export_mode)
    monkeypatch.setattr(product_check, "check_order", _check_vivo_escolha_invalida)

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == MOTIVO_ESCOLHA_INATIVA
    assert recebido == {}  # nada saiu
    (audit,) = [a for a in repo.list_audit(entry_id) if a["event_type"] == "send_to_fire_blocked"]
    assert audit["detail"]["produto"][0]["estado"] == "vinculo_invalido"


@pytest.mark.parametrize("modo", ["desligado", "observando"])
def test_export_xlsx_recusa_escolha_com_produto_inativo(monkeypatch, tmp_path, modo):
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, modo, "xlsx")
    monkeypatch.setattr(product_check, "check_order", _check_vivo_escolha_invalida)

    r = client.post(f"/api/imported/{entry_id}/export-xlsx")

    assert r.status_code == 409, r.text
    assert r.json()["detail"] == MOTIVO_ESCOLHA_INATIVA
    assert "xlsx_itens" not in recebido


def test_send_db_com_fire_fora_nao_recusa_a_escolha(monkeypatch, tmp_path):
    """Fire fora (check indisponível) não afirma que o produto sumiu: segue como hoje."""
    entry_id, recebido = _saida_fora_de_ligado(monkeypatch, tmp_path, "desligado", "db")

    r = client.post(f"/api/imported/{entry_id}/send-to-fire")

    assert r.status_code == 200, r.text
    assert recebido["seq"] == {0: 2010}
