"""Task 7: vincular por escopo (v2), trocar com confirmação, revogar e fila a identificar."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.erp import product_check
from app.persistence import db, produto_depara_repo, produto_vinculo_repo, repo
from app.web.server import app

client = TestClient(app, raise_server_exceptions=True)

RIACHUELO_LOJA_1 = "33.200.056/0001-49"
RIACHUELO_LOJA_2 = "33.200.056/0390-00"
OUTRO_CLIENTE = "11.222.333/0001-44"


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture
def check_fake(monkeypatch):
    """check_order sem Fire: item com vínculo v2 vigente no escopo fica `ok`,
    o resto `nao_encontrado`. Guarda quem foi checado."""
    from app.erp import produto_escopo

    chamados: list[str] = []

    def fake(order, *, env=None):
        chamados.append(order.header.order_number)
        items = []
        with db.connect() as conn:
            for i, it in enumerate(order.items):
                escopos = produto_escopo.escopos_para(conn, produto_escopo.cnpj_do_item(order, i))
                achado = produto_vinculo_repo.lookup(
                    conn,
                    escopos,
                    codigos=[it.product_code] if it.product_code else [],
                    eans=[it.ean] if it.ean else [],
                )
                v = next(iter(achado.values()), None)
                items.append(
                    {
                        "product_code": it.product_code,
                        "ean": it.ean,
                        "produto": {
                            "estado": "ok" if v else "nao_encontrado",
                            "fire_seq": v.fire_seq if v else None,
                            "fonte": "depara" if v else None,
                            "motivo": None if v else "Código não encontrado no Fire.",
                            "candidatos": [],
                        },
                    }
                )
        return {"available": True, "items": items, "produto_conferencia_incompleta": False}

    monkeypatch.setattr(product_check, "check_order", fake)
    return chamados


def _pendente(motivo="Código não encontrado no Fire."):
    return {
        "estado": "nao_encontrado",
        "fire_seq": None,
        "fonte": None,
        "motivo": motivo,
        "candidatos": [],
    }


def _seed_pedido(
    *,
    cnpj: str,
    numero: str,
    itens: list[dict],
    produtos: list[dict] | None = None,
    portal_status: str = "parsed",
    imported_at: str | None = None,
) -> str:
    entry_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": entry_id,
            "source_filename": f"{numero}.pdf",
            "imported_at": imported_at or datetime.now().isoformat(timespec="seconds"),
            "order_number": numero,
            "customer": "RIACHUELO",
            "customer_cnpj": cnpj,
            "status": "success",
            "portal_status": portal_status,
            "snapshot": {
                "header": {
                    "order_number": numero,
                    "customer_name": "RIACHUELO",
                    "customer_cnpj": cnpj,
                },
                "items": itens,
                "source_file": "",
            },
            "check": {
                "available": True,
                "items": [
                    {"product_code": it.get("product_code"), "ean": it.get("ean"), "produto": p}
                    for it, p in zip(itens, produtos or [_pendente()] * len(itens), strict=True)
                ],
            },
        }
    )
    return entry_id


def _item(codigo="ABC123", ean="7891234567890", desc="TENIS SEM MATCH"):
    return {"description": desc, "quantity": 1.0, "product_code": codigo, "ean": ean}


def _seed_catalogo():
    from app.erp.catalog_extract import ProdutoFireDTO
    from app.persistence import catalogo_fire_repo

    dtos = [
        ProdutoFireDTO(
            fire_produto_id=seq,
            codigo=seq,
            nome=nome,
            unidade="PAR",
            ean=None,
            ativo=True,
            tipo="simples",
            codigo_alternativo=alt,
        )
        for seq, nome, alt in (("10", "TENIS AZUL", "TA-10"), ("11", "TENIS PRETO", None))
    ]
    with db.connect() as conn:
        catalogo_fire_repo.replace_all(conn, dtos, extraido_em="2026-10-19T00:00:00")


def _vincular(entry_id, seq="10", **extra):
    return client.post(
        f"/api/imported/{entry_id}/vincular-produto",
        json={"item_index": 0, "fire_produto_id": seq, **extra},
    )


def _vigentes():
    with db.connect() as conn:
        return produto_vinculo_repo.listar(conn)


def _audits(entry_id, tipo):
    return [a for a in repo.list_audit(entry_id) if a["event_type"] == tipo]


def test_vincular_grava_v2_com_escopo_raiz(check_fake):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item(codigo=" abc123 ")])

    r = _vincular(entry_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entry_id"] == entry_id
    assert body["check"]["items"][0]["produto"]["estado"] == "ok"
    chaves = {(v["chave_tipo"], v["chave_valor"]) for v in body["vinculos"]}
    assert chaves == {("codigo", "ABC123"), ("ean", "7891234567890")}

    vig = {(v.chave_tipo, v.chave_valor): v for v in _vigentes()}
    assert set(vig) == chaves
    v = vig[("codigo", "ABC123")]
    assert v.escopo == "raiz:33200056"
    assert v.fire_seq == 10
    assert v.fire_codigo == "TA-10"
    assert v.fire_descricao == "TENIS AZUL"
    assert v.origem == "manual"

    # modo desligado: o v1 também é gravado para o check legado
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    with db.connect() as conn:
        v1 = produto_depara_repo.lookup(conn, ckey, codigos=["ABC123"], eans=[])
    assert v1[("codigo", "ABC123")]["fire_produto_id"] == "10"

    criado = _audits(entry_id, "produto_vinculo_criado")
    assert len(criado) == 1
    assert criado[0]["detail"]["escopo"] == "raiz:33200056"


def test_vincular_modo_ligado_nao_grava_v1(check_fake, monkeypatch):
    from app.web import server

    monkeypatch.setattr(server, "_trava_produto_ligada", lambda env: True)
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    assert _vincular(entry_id).status_code == 200
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    with db.connect() as conn:
        assert produto_depara_repo.lookup(conn, ckey, codigos=["ABC123"], eans=[]) == {}
    assert len(_vigentes()) == 2


def test_vincular_conflito_409_sem_substituir(check_fake):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    assert _vincular(entry_id, "10").status_code == 200

    r = _vincular(entry_id, "11")

    assert r.status_code == 409, r.text
    conflito = r.json()["conflito"]
    assert conflito["chave_tipo"] == "codigo"
    assert conflito["chave_valor"] == "ABC123"
    assert conflito["atual"]["fire_seq"] == 10
    assert conflito["atual"]["fire_descricao"] == "TENIS AZUL"
    assert conflito["atual"]["criado_por"]
    assert conflito["atual"]["criado_em"]
    # nada mudou: as duas chaves seguem no SEQ 10, sem gravação parcial
    assert {v.fire_seq for v in _vigentes()} == {10}
    assert len(_vigentes()) == 2


def test_vincular_substituir_revoga_e_audita_trocado(check_fake):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    assert _vincular(entry_id, "10").status_code == 200

    r = _vincular(entry_id, "11", substituir=True)

    assert r.status_code == 200, r.text
    assert {v.fire_seq for v in _vigentes()} == {11}
    with db.connect() as conn:
        todos = produto_vinculo_repo.listar(conn, incluir_revogados=True)
    assert len(todos) == 4  # os dois antigos seguem na tabela, revogados

    trocado = _audits(entry_id, "produto_vinculo_trocado")
    assert len(trocado) == 1
    d = trocado[0]["detail"]
    assert d["escopo"] == "raiz:33200056"
    assert d["user_email"]
    assert d["pedido"] == "R-1"
    por_chave = {c["chave_tipo"]: c for c in d["chaves"]}
    assert por_chave["codigo"]["chave_valor"] == "ABC123"
    assert por_chave["codigo"]["fire_seq_antigo"] == 10
    assert por_chave["codigo"]["fire_seq_novo"] == 11


def test_vincular_sem_cnpj_422(check_fake):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj="", numero="R-1", itens=[_item()])

    r = _vincular(entry_id)

    assert r.status_code == 422
    assert r.json()["detail"] == (
        "Este pedido não tem CNPJ do cliente; o vínculo não pode ser reaproveitado."
    )
    assert _vigentes() == []


def test_vincular_intercompany_incompleto_503(check_fake, monkeypatch):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    monkeypatch.setattr(product_check, "_cnpj_real", lambda order, env: (None, False))

    r = _vincular(entry_id)

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Não consegui confirmar o cliente real deste pedido agora; tente de novo."
    )
    assert _vigentes() == []


def test_vincular_recheca_outros_pedidos_do_escopo(check_fake):
    _seed_catalogo()
    p1 = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    p2 = _seed_pedido(
        cnpj=RIACHUELO_LOJA_2, numero="R-2", itens=[_item(desc="X"), _item(codigo="ABC123")]
    )
    outro = _seed_pedido(cnpj=OUTRO_CLIENTE, numero="O-1", itens=[_item()])
    ja_ok = _seed_pedido(
        cnpj=RIACHUELO_LOJA_2,
        numero="R-3",
        itens=[_item()],
        produtos=[{"estado": "ok", "fire_seq": 99, "candidatos": []}],
    )
    no_fire = _seed_pedido(
        cnpj=RIACHUELO_LOJA_2, numero="R-4", itens=[_item()], portal_status="sent_to_fire"
    )

    r = _vincular(p1)

    assert r.status_code == 200, r.text
    assert r.json()["rechecados"] == [p2]
    assert check_fake == ["R-1", "R-2"]
    salvo = repo.get_import(p2)["check"]
    assert [i["produto"]["estado"] for i in salvo["items"]] == ["ok", "ok"]
    assert repo.get_import(no_fire)["check"]["items"][0]["produto"]["estado"] == "nao_encontrado"
    assert repo.get_import(ja_ok)["check"]["items"][0]["produto"]["fire_seq"] == 99
    assert repo.get_import(outro)["check"]["items"][0]["produto"]["estado"] == "nao_encontrado"


def test_revogar_audita_e_some_do_lookup(check_fake):
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    assert _vincular(entry_id).status_code == 200
    vid = next(v.id for v in _vigentes() if v.chave_tipo == "codigo")

    r = client.delete(f"/api/produtos/vinculo/{vid}")

    assert r.status_code == 200, r.text
    assert r.json() == {"revogado": vid}
    with db.connect() as conn:
        achado = produto_vinculo_repo.lookup(conn, ["raiz:33200056"], codigos=["ABC123"], eans=[])
    assert achado == {}
    revogado = _audits(entry_id, "produto_vinculo_revogado")
    assert len(revogado) == 1
    assert revogado[0]["detail"]["vinculo_id"] == vid
    assert revogado[0]["detail"]["fire_seq"] == 10

    assert client.delete("/api/produtos/vinculo/999999").status_code == 404


def test_fila_a_identificar_lista_so_pendentes_do_ambiente():
    from app.persistence import context

    antigo = _seed_pedido(
        cnpj=RIACHUELO_LOJA_1,
        numero="R-1",
        itens=[_item(codigo="A1", ean=None, desc="TENIS A"), _item(codigo="B2", desc="TENIS B")],
        produtos=[
            {"estado": "ok", "fire_seq": 1, "candidatos": []},
            _pendente("Mais de um produto ativo com este código."),
        ],
        imported_at="2026-10-18T09:00:00",
    )
    novo = _seed_pedido(
        cnpj=OUTRO_CLIENTE,
        numero="O-1",
        itens=[_item(codigo="C3", ean=None, desc="SANDALIA")],
        imported_at="2026-10-19T09:00:00",
    )
    _seed_pedido(
        cnpj=OUTRO_CLIENTE,
        numero="O-2",
        itens=[_item()],
        portal_status="sent_to_fire",
        imported_at="2026-10-19T10:00:00",
    )
    # pedido de outro ambiente nunca aparece
    with context.active_env("outro-env-id", "outro"):
        _seed_pedido(cnpj=OUTRO_CLIENTE, numero="X-1", itens=[_item()])

    r = client.get("/api/produtos/a-identificar")

    assert r.status_code == 200, r.text
    linhas = r.json()["itens"]
    assert [(lin["import_id"], lin["item_index"]) for lin in linhas] == [(antigo, 1), (novo, 0)]
    assert linhas[0] == {
        "import_id": antigo,
        "pedido": "R-1",
        "cliente": "RIACHUELO",
        "item_index": 1,
        "codigo": "B2",
        "ean": "7891234567890",
        "descricao": "TENIS B",
        "estado": "nao_encontrado",
        "motivo": "Mais de um produto ativo com este código.",
        "importado_em": "2026-10-18T09:00:00",
    }
