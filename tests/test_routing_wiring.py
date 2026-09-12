"""O wiring do roteamento no /api/commit, nos tres modos.

O teste que protege a adocao e o primeiro: em 'desligado' nada muda.

Os testes substituem `ambiente.deps_padrao` por uma versao sem Firebird:
`env_por_cnpj` e `memoria` continuam reais (SQLite puro, sem risco), mas
`historico` vira uma tupla vazia — nenhum destes cenarios depende do degrau
2 para resolver (documento resolve direto, ou cai para 'perguntar'), e a
cerca de `tests/conftest.py::_no_real_firebird` bloqueia qualquer teste que
deixe `deps_padrao()` real tentar `historico.consultar` (que abriria
Firebird via `environments_repo.list_active()`).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_repo, router
from app.routing import ambiente as routing
from app.web.server import app

NASMAR = "34513679000134"
MM = "35394871000111"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
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

    # Substitui só o degrau que faria I/O real (Firebird). `env_por_cnpj` e
    # `memoria` de `deps_padrao()` são SQLite puro — seguros de reusar.
    real = routing.deps_padrao()

    def _deps_sem_firebird():
        return routing.Deps(
            env_por_cnpj=real.env_por_cnpj,
            historico=lambda cnpjs: (),  # noqa: ARG005 — nenhum cenário depende do degrau 2
            memoria=real.memoria,
        )

    monkeypatch.setattr(routing, "deps_padrao", _deps_sem_firebird)

    yield {"client": TestClient(app), "nasmar": nasmar, "mm": mm}


def _order(supplier_cnpj):
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(
            order_number="4711",
            customer_cnpj="11222333000181",
            customer_name="DAJU",
            supplier_cnpj=supplier_cnpj,
        ),
        items=[OrderItem(description="Meia Kit 3", quantity=10, unit_price=11.96)],
    )


def _preview_com_fornecedor(portal, supplier_cnpj, **kw):
    """Poe um Order no cache de preview e devolve o preview_id.

    `put()` exige os bytes e a extensao do arquivo original (assinatura em
    app/web/preview_cache.py:50) e devolve o PreviewEntry, nao o id.
    """
    from app.web.preview_cache import get_cache

    entry = get_cache().put(
        order=_order(supplier_cnpj),
        source_filename="PEDIDO.pdf",
        source_bytes=b"%PDF-1.4 fake",
        source_ext=".pdf",
        check=None,
        **kw,
    )
    return entry.preview_id


def _import_do_ambiente(slug):
    from app.persistence import context as env_context
    from app.persistence import repo

    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        return repo.list_imports(limit=50)


def test_desligado_nao_muda_nada(portal):
    """Cookie da MM, pedido com fornecedor NASMAR: entra na MM, como hoje."""
    roteamento_repo.set_modo("desligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    r = c.post("/api/commit", json={"preview_id": pid})
    assert r.status_code == 200
    assert len(_import_do_ambiente("mm")) == 1
    assert _import_do_ambiente("nasmar") == []
    assert roteamento_repo.taxa()["total"] == 0  # nem sombra grava


def test_observando_grava_sombra_e_a_escolha_do_operador_prevalece(portal):
    roteamento_repo.set_modo("observando", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    assert len(_import_do_ambiente("mm")) == 1  # o operador venceu
    assert _import_do_ambiente("nasmar") == []
    t = roteamento_repo.taxa()
    assert t["total"] == 1
    assert t["bateu"] == 0  # e a divergencia ficou registrada
    assert t["divergiu"] == 1


def test_ligado_manda_o_pedido_para_o_ambiente_do_documento(portal):
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    nas = _import_do_ambiente("nasmar")
    assert len(nas) == 1
    assert nas[0]["order_number"] == "4711"
    assert _import_do_ambiente("mm") == []


def test_ligado_sem_resposta_pede_escolha_em_vez_de_chutar(portal):
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, None)
    r = c.post("/api/commit", json={"preview_id": pid})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["precisa_escolher"] is True
    assert {o["slug"] for o in body["opcoes"]} == {"nasmar", "mm"}
    assert _import_do_ambiente("mm") == []
    assert _import_do_ambiente("nasmar") == []


def test_escolha_do_operador_e_gravada_com_autor(portal):
    from app.persistence import decisao_ambiente_repo as memoria

    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, None)
    r = c.post("/api/commit", json={"preview_id": pid, "environment_slug": "nasmar"})
    assert r.status_code == 200
    assert len(_import_do_ambiente("nasmar")) == 1
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "nasmar"
    assert d["decidido_por"]


def test_arquivo_original_segue_o_ambiente_roteado_nao_o_cookie(portal, tmp_path):
    """Armadilha 2: o `shutil.move` final tem que usar as pastas do ambiente
    ROTEADO — senão o arquivo da Nasmar vai parar em `Pedidos importados`
    da MM."""
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])

    src = tmp_path / "n_in" / "PEDIDO.pdf"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"%PDF-1.4 fake")
    pid = _preview_com_fornecedor(portal, NASMAR, source_path=str(src))

    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    assert not src.exists()
    assert (tmp_path / "n_in" / "Pedidos importados" / "PEDIDO.pdf").is_file()
    assert not (tmp_path / "m_in" / "Pedidos importados" / "PEDIDO.pdf").exists()


def test_bloco_de_roteamento_do_preview(portal):
    """O payload do preview carrega a decisao — a UI nunca roteia em silencio.

    Testado na funcao, nao numa rota: o bloco `roteamento` e adicionado ao
    payload explicitamente pelos dois handlers de preview FRESCO (POST
    /api/preview e POST /api/preview-pending) — nao dentro de
    `_build_preview_payload`, que tambem alimenta `GET
    /api/imported/{id}/preview` (revisao de pedido JA commitado). Naquele
    terceiro caso o ambiente ja e definitivo e nao ha decisao a tomar, entao
    o bloco so custaria I/O a toa contra o Firebird quando o modo nao for
    'desligado' (fix round 1, Achado 4).
    """
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("observando", por="t")
    rot = _roteamento_para_preview(_order(NASMAR))
    assert rot["degrau"] == "documento"
    assert rot["env_slug"] == "nasmar"
    assert rot["precisa_escolher"] is False
    assert "34.513.679/0001-34" in rot["explicacao"]


def test_bloco_de_roteamento_e_none_quando_desligado(portal):
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("desligado", por="t")
    assert _roteamento_para_preview(_order(NASMAR)) is None


def test_bloco_de_roteamento_pede_escolha_quando_ligado_e_mudo(portal):
    from app.web.server import _roteamento_para_preview

    roteamento_repo.set_modo("ligado", por="t")
    rot = _roteamento_para_preview(_order(None))
    assert rot["precisa_escolher"] is True
    assert {o["slug"] for o in rot["opcoes"]} == {"nasmar", "mm"}


def test_decidir_ambiente_em_observando_engole_excecao_e_pedido_segue(portal, monkeypatch):
    """Contrato do coordenador: exceção do roteador em 'observando' vira log
    e o commit segue com a escolha do operador — sem gravar sombra."""
    from app.web.server import _decidir_ambiente

    roteamento_repo.set_modo("observando", por="t")

    def _explode():
        raise RuntimeError("Firebird fora do ar")

    monkeypatch.setattr(routing, "deps_padrao", _explode)
    modo, decisao = _decidir_ambiente(_order(NASMAR))
    assert modo == "observando"
    assert decisao is None

    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200
    assert len(_import_do_ambiente("mm")) == 1
    assert roteamento_repo.taxa()["total"] == 0  # sem sombra


def test_decidir_ambiente_em_ligado_pede_escolha_quando_o_roteador_falha(portal, monkeypatch):
    """Contrato do coordenador: exceção do roteador em 'ligado' NÃO cai pro
    cookie em silêncio — vira 'não soube responder', pede escolha."""
    from app.web.server import _decidir_ambiente

    roteamento_repo.set_modo("ligado", por="t")

    def _explode():
        raise RuntimeError("Firebird fora do ar")

    monkeypatch.setattr(routing, "deps_padrao", _explode)
    modo, decisao = _decidir_ambiente(_order(NASMAR))
    assert modo == "ligado"
    assert decisao.resolveu is False
    assert "falha" in decisao.explicacao.lower()

    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)
    r = c.post("/api/commit", json={"preview_id": pid})
    assert r.status_code == 409
    assert r.json()["detail"]["precisa_escolher"] is True
    assert _import_do_ambiente("mm") == []


def test_preview_nao_e_queimado_quando_o_commit_falha_e_a_escolha_do_commit_vale(
    portal, monkeypatch
):
    """Cenario B do fix round 1 (Achado 1, revisao): o roteador resolve no
    PREVIEW (documento -> nasmar) e falha bem no COMMIT (Firebird caiu no
    meio). Antes do fix, `get_cache().consume()` rodava ANTES da decisao de
    ambiente — o 409 que pede escolha de novo queimava o preview, e a
    segunda tentativa do operador batia em "Preview ja foi importado" com as
    duas DBs vazias (a mentira que a revisao reproduziu por HTTP real).

    Prova tambem o contrato mais geral que a propria revisao usou pra achar
    o bug: a decisao que vale e a do MOMENTO DO COMMIT, nunca a que o
    preview mostrou antes — aqui o operador escolhe "mm", ambiente
    DIFERENTE do "nasmar" que o preview tinha sugerido, e essa escolha e a
    que entra.
    """
    roteamento_repo.set_modo("ligado", por="t")
    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, NASMAR)

    # O preview veria o degrau documento resolvendo pra nasmar...
    from app.web.server import _roteamento_para_preview

    rot = _roteamento_para_preview(_order(NASMAR))
    assert rot["env_slug"] == "nasmar"

    # ...mas o roteador falha bem no momento do commit.
    def _explode():
        raise RuntimeError("Firebird fora do ar")

    monkeypatch.setattr(routing, "deps_padrao", _explode)

    r1 = c.post("/api/commit", json={"preview_id": pid})
    assert r1.status_code == 409
    assert r1.json()["detail"]["precisa_escolher"] is True

    # O preview NAO foi queimado: a segunda tentativa, com uma escolha
    # DIFERENTE da que o preview tinha sugerido, funciona de verdade.
    r2 = c.post("/api/commit", json={"preview_id": pid, "environment_slug": "mm"})
    assert r2.status_code == 200
    assert len(_import_do_ambiente("mm")) == 1
    assert _import_do_ambiente("nasmar") == []


def test_divergencia_contra_a_memoria_e_gravada_no_commit(portal):
    """A memoria registrada pra este cliente era 'nasmar'; o documento deste
    pedido aponta pra 'mm' — um degrau mais forte contradisse o julgamento
    humano anterior, e isso tem que ficar marcado
    (`decisao_ambiente_repo.marcar_divergencia`), nao sobrescrito em
    silencio. Sem este teste, o ramo `if decisao.divergiu_de and
    cnpj_cliente: memoria.marcar_divergencia(...)` de `commit_preview` nunca
    era exercitado (Minor 8 do fix round 1)."""
    from app.persistence import decisao_ambiente_repo as memoria

    roteamento_repo.set_modo("ligado", por="t")
    memoria.lembrar(cnpj_cliente="11222333000181", env_slug="nasmar", por="alguem")

    c = portal["client"]
    c.cookies.set("portal_env", portal["mm"]["id"])
    pid = _preview_com_fornecedor(portal, MM)
    assert c.post("/api/commit", json={"preview_id": pid}).status_code == 200

    assert len(_import_do_ambiente("mm")) == 1
    assert _import_do_ambiente("nasmar") == []
    d = memoria.lembrada("11222333000181")
    assert d["env_slug"] == "nasmar"  # a memoria em si nao muda
    assert d["divergiu_de"] == "nasmar"  # mas a divergencia fica marcada
    assert d["divergiu_em"]
