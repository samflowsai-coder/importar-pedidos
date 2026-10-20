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

    def fake(order, *, env=None, escolhas=None):
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


CLIENTE_REAL = "61.585.865/0240-90"


@pytest.fixture
def env_intercompany(monkeypatch):
    """Ambiente com slug no request (como o middleware/env_do_pedido poriam) e
    `resolucao_para` controlável. Devolve o setter da resolução."""
    from app.erp import preco_trava
    from app.web import server

    env = {"id": "test-env-id", "slug": "test", "produto_chave_modo": "desligado"}
    monkeypatch.setattr(server, "_request_environment", lambda request: env)
    monkeypatch.setattr(preco_trava, "check_preco_para_gravar", lambda *a, **k: None)
    estado = {"r": None}
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: estado["r"])

    def definir(r):
        estado["r"] = r

    return definir


def _nada_gravado(cnpj_header):
    assert _vigentes() == []
    ckey = produto_depara_repo.client_key(cnpj_header, "RIACHUELO")
    with db.connect() as conn:
        assert produto_depara_repo.list_for_client(conn, ckey) == []


def test_vincular_intercompany_erro_conexao_503(check_fake, env_intercompany):
    from app.erp.depara_cliente import ResolucaoCliente

    env_intercompany(ResolucaoCliente(False, motivo="erro_conexao"))
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    r = _vincular(entry_id)

    assert r.status_code == 503
    assert r.json()["detail"] == (
        "Não consegui confirmar o cliente real deste pedido agora; tente de novo."
    )
    _nada_gravado(RIACHUELO_LOJA_1)


@pytest.mark.parametrize("motivo", ["nao_encontrado", "sem_chave"])
def test_vincular_intercompany_sem_cliente_real_422(check_fake, env_intercompany, motivo):
    from app.erp.depara_cliente import ResolucaoCliente

    env_intercompany(ResolucaoCliente(False, motivo=motivo))
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    r = _vincular(entry_id)

    assert r.status_code == 422
    assert r.json()["detail"] == (
        "Não sei qual é o cliente real deste pedido (pedido da revenda); "
        "o vínculo não pode ser salvo."
    )
    _nada_gravado(RIACHUELO_LOJA_1)


def test_vincular_intercompany_resolvido_grava_no_escopo_do_cliente_real(
    check_fake, env_intercompany
):
    from app.erp.depara_cliente import ResolucaoCliente

    env_intercompany(ResolucaoCliente(True, cnpj=CLIENTE_REAL, motivo="ok"))
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    r = _vincular(entry_id)

    assert r.status_code == 200, r.text
    assert {v.escopo for v in _vigentes()} == {"raiz:61585865"}
    assert {v["env"] for v in r.json()["vinculos"]} == {"test"}
    # R13: sem v1 (chaveado pela revenda) em pedido intercompany
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    with db.connect() as conn:
        assert produto_depara_repo.list_for_client(conn, ckey) == []
        assert conn.execute("SELECT COUNT(*) FROM produto_depara").fetchone()[0] == 0
    assert _audits(entry_id, "produto_vinculo_criado")[0]["detail"]["v1"] is False


def test_vincular_nao_intercompany_em_desligado_grava_v1(check_fake, env_intercompany):
    env_intercompany(None)  # resolucao_para None: pedido não é intercompany
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    assert _vincular(entry_id).status_code == 200
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    with db.connect() as conn:
        assert len(produto_depara_repo.list_for_client(conn, ckey)) == 2
    assert _audits(entry_id, "produto_vinculo_criado")[0]["detail"]["v1"] is True


def test_vincular_nao_recheca_com_fire_fora(monkeypatch):
    chamados: list[str] = []

    def fora(order, *, env=None, escolhas=None):
        chamados.append(order.header.order_number)
        return {"available": False, "items": []}

    monkeypatch.setattr(product_check, "check_order", fora)
    _seed_catalogo()
    p1 = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    _seed_pedido(cnpj=RIACHUELO_LOJA_2, numero="R-2", itens=[_item()])

    r = _vincular(p1)

    assert r.status_code == 200, r.text
    assert r.json()["rechecados"] == []
    assert chamados == ["R-1"]


def test_vincular_recheca_no_maximo_50_mais_antigos(check_fake):
    _seed_catalogo()
    p1 = _seed_pedido(
        cnpj=RIACHUELO_LOJA_1, numero="R-0", itens=[_item()], imported_at="2026-10-01T00:00:00"
    )
    outros = [
        _seed_pedido(
            cnpj=RIACHUELO_LOJA_2,
            numero=f"R-{i}",
            itens=[_item()],
            imported_at=f"2026-10-02T00:{i:02d}:00",
        )
        for i in range(1, 53)
    ]

    r = _vincular(p1)

    assert r.status_code == 200, r.text
    assert r.json()["rechecados"] == outros[:50]


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


def _cria_env(slug, tmp_path):
    from app.persistence import environments_repo

    pasta = tmp_path / slug
    pasta.mkdir(parents=True, exist_ok=True)
    return environments_repo.create(
        slug=slug,
        name=slug.upper(),
        watch_dir=str(pasta),
        output_dir=str(pasta),
        fb_path=str(tmp_path / f"{slug}.fdb"),
    )


def test_revogar_audita_some_do_lookup_e_apaga_o_v1(check_fake, tmp_path):
    _cria_env("test", tmp_path)
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    r = _vincular(entry_id)
    assert r.status_code == 200
    vinc = next(v for v in r.json()["vinculos"] if v["chave_tipo"] == "codigo")
    assert vinc["env"] == "test"
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    # v1 do mesmo cliente apontando para OUTRO produto não é tocado
    with db.connect() as conn:
        produto_depara_repo.upsert(
            conn,
            client_key=ckey,
            chave_tipo="codigo",
            chave_valor="OUTRA",
            fire_produto_id="11",
            fire_codigo="11",
            fire_ean=None,
            fire_nome="TENIS PRETO",
            criado_em="2026-10-19T00:00:00",
            criado_por="x",
        )

    r = client.delete(f"/api/produtos/vinculo/{vinc['id']}?env={vinc['env']}")

    assert r.status_code == 200, r.text
    assert r.json() == {"revogado": vinc["id"]}
    with db.connect() as conn:
        achado = produto_vinculo_repo.lookup(conn, ["raiz:33200056"], codigos=["ABC123"], eans=[])
        v1 = produto_depara_repo.lookup(conn, ckey, codigos=["ABC123", "OUTRA"], eans=[])
    assert achado == {}
    assert set(v1) == {("codigo", "OUTRA")}  # o ABC123 -> 10 saiu, o EAN segue (outro vínculo)
    revogado = _audits(entry_id, "produto_vinculo_revogado")
    assert len(revogado) == 1
    d = revogado[0]["detail"]
    assert d["vinculo_id"] == vinc["id"]
    assert d["fire_seq"] == 10
    assert d["env"] == "test"
    assert [x["client_key"] for x in d["v1_apagados"]] == [ckey]


def test_revogar_em_ligado_nao_mexe_no_v1(check_fake, tmp_path):
    from app.persistence import environments_repo

    env = _cria_env("test", tmp_path)
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])
    vid = next(
        v["id"] for v in _vincular(entry_id).json()["vinculos"] if v["chave_tipo"] == "codigo"
    )
    environments_repo.set_produto_chave_modo(env["id"], "ligado", por="t")

    assert client.delete(f"/api/produtos/vinculo/{vid}?env=test").status_code == 200
    ckey = produto_depara_repo.client_key(RIACHUELO_LOJA_1, "RIACHUELO")
    with db.connect() as conn:
        assert produto_depara_repo.lookup(conn, ckey, codigos=["ABC123"], eans=[])


def _vinculo_direto(slug, tmp_env_id):
    from app.persistence import context

    with context.active_env(tmp_env_id, slug), db.connect() as conn:
        return produto_vinculo_repo.vincular(
            conn,
            escopo="raiz:33200056",
            chave_tipo="codigo",
            chave_valor="ABC123",
            fire_seq=10,
            fire_codigo=None,
            fire_descricao="TENIS AZUL",
            por="x",
            pedido_id=None,
            agora="2026-10-19T00:00:00",
        )


def test_revogar_usa_a_empresa_do_parametro_nao_a_do_cookie(tmp_path):
    from app.persistence import context

    mm = _cria_env("mm", tmp_path)
    nasmar = _cria_env("nasmar", tmp_path)
    v_mm = _vinculo_direto("mm", mm["id"])
    v_nasmar = _vinculo_direto("nasmar", nasmar["id"])
    assert v_mm.id == v_nasmar.id  # ids são por empresa

    cli = TestClient(app)
    cli.cookies.set("portal_env", mm["id"])
    r = cli.delete(f"/api/produtos/vinculo/{v_nasmar.id}?env=nasmar")

    assert r.status_code == 200, r.text
    with context.active_env(nasmar["id"], "nasmar"), db.connect() as conn:
        assert produto_vinculo_repo.listar(conn) == []
    with context.active_env(mm["id"], "mm"), db.connect() as conn:
        assert [v.id for v in produto_vinculo_repo.listar(conn)] == [v_mm.id]


def test_revogar_id_inexistente_404_e_empresa_inacessivel_403(tmp_path):
    from app.persistence import environments_repo

    _cria_env("mm", tmp_path)
    inativa = _cria_env("velha", tmp_path)
    environments_repo.soft_delete(inativa["id"])

    assert client.delete("/api/produtos/vinculo/999999?env=mm").status_code == 404
    assert client.delete("/api/produtos/vinculo/1?env=velha").status_code == 403
    assert client.delete("/api/produtos/vinculo/1?env=nao-existe").status_code == 403
    assert client.delete("/api/produtos/vinculo/1").status_code == 422


def test_fila_a_identificar_lista_so_pendentes_do_ambiente():
    from app.persistence import context

    antigo = _seed_pedido(
        cnpj=RIACHUELO_LOJA_1,
        numero="R-1",
        itens=[_item(codigo="A1", ean=None, desc="TENIS A"), _item(codigo="B2", desc="TENIS B")],
        produtos=[
            # ok de verdade: código no Fire e sem troca pela planilha (senão a
            # fila também lista o item quando sai planilha — ver test_web_static_produto)
            {
                "estado": "ok",
                "fire_seq": 1,
                "candidatos": [{"seq": 1, "codprod_altern": "A1"}],
                "troca_no_fire": None,
            },
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


@pytest.mark.parametrize(
    ("export_mode", "bloqueado", "reason"), [("db", False, None), ("xlsx", True, "sem_codigo_fire")]
)
def test_vincular_devolve_a_trava_do_check_novo(
    check_fake, monkeypatch, export_mode, bloqueado, reason
):
    """A tela troca a trava pela da resposta: ela precisa refletir o check DEPOIS
    do vínculo (antes: produto_a_identificar)."""
    from app.erp import preco_trava
    from app.web import server

    env = {"id": "test-env-id", "slug": "test", "produto_chave_modo": "ligado"}
    monkeypatch.setattr(server, "_request_environment", lambda request: env)
    monkeypatch.setattr(server, "_export_mode_atual", lambda: export_mode)
    monkeypatch.setattr(preco_trava, "check_preco_para_gravar", lambda *a, **k: None)
    monkeypatch.setattr(server, "resolucao_para", lambda order, *, slug: None)
    _seed_catalogo()
    entry_id = _seed_pedido(cnpj=RIACHUELO_LOJA_1, numero="R-1", itens=[_item()])

    r = _vincular(entry_id)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["check"]["items"][0]["produto"]["estado"] == "ok"
    trava = body["produto_trava"]
    assert trava["bloqueado"] is bloqueado
    assert trava["reason"] == reason
    assert trava["export_mode"] == export_mode
