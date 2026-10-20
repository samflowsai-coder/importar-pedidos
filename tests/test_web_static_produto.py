"""Task 8: a tela do produto por chave exata.

Sem infra de JS no repo: o teste lê o `index.html` servido em `/` e trava os ids,
as funções e os textos fixos que a tela promete (rótulos do estado do item, as 3
seções do picker, o painel de conflito e a fila). Mais o `produto_chave_modo` que
o preview e a fila expõem para a tela saber se mostra o estado novo.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, repo
from app.web.server import app

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dbstate")
    db.set_db_path(tmp / "app_state.db")
    db.reset_init_cache()
    yield
    db.set_db_path(None)
    db.reset_init_cache()


@pytest.fixture(scope="module")
def html() -> str:
    r = client.get("/")
    assert r.status_code == 200
    return r.text


@pytest.mark.parametrize(
    "trecho",
    [
        "function produtoBadge(",
        "function divergenciasAtributos(",
        # rótulos fixos do estado do item
        "'Produto confirmado'",
        "'Escolher produto'",
        "'Mais de um produto com essa chave'",
        "'Produto bloqueado'",
        "'Vínculo inválido'",
        "'Código e EAN apontam para produtos diferentes'",
        "'Fire grava outro produto'",
        "'Fire sem resposta'",
        # picker: as 3 seções, nesta ordem no código
        "Candidatos exatos",
        "Sugestões pela descrição",
        ">sugestão<",
        # confirmação lado a lado
        'id="ppConfirmacao"',
        "Linha do cliente",
        "Produto escolhido",
        # conflito 409: painel na página, sem confirm()
        'id="ppConflito"',
        "já aponta para",
        "Trocar para o produto escolhido?",
        ">Trocar vínculo<",
        ">Manter<",
        "Cliente pediu",
        "Vínculo atual",
        "Você escolheu",
        # confirmação: botão próprio, divergência rebaixa o botão
        'id="ppVincularBtn"',
        "Vincular mesmo assim",
        'id="ppConfErro"',
        # chips de atributo nas linhas do picker
        "≠ tamanho",
        "tamanho, pares e cor batem",
        # Fire sem resposta
        "Não deu para conferir os produtos: o Fire não respondeu.",
        "Recarregar",
        "Recarrega o que está salvo. Ao enviar, o Portal confere de novo no Fire.",
        "'Sem código para a planilha'",
        "Esse produto não está mais no catálogo do Fire. Escolha outro.",
        "Nenhum produto além dos",
        "Aguardando o Fire",
        "substituir: true",
        # fila
        "Itens a identificar",
        'id="tab-identificar"',
        "/api/produtos/a-identificar",
    ],
)
def test_html_tem_trecho(html, trecho):
    assert trecho in html, f"trecho ausente do index.html: {trecho}"


def test_secoes_do_picker_na_ordem(html):
    a = html.index("Candidatos exatos")
    b = html.index("Sugestões pela descrição")
    c = html.index("Buscar no catálogo")
    assert a < b < c


def test_regras_da_auditoria_no_js(html):
    for trecho in (
        r"\d{2}\s*(-|A|\/)\s*\d{2}",
        r"(\d+)\s*PARES",
        r"(\d)\s*X\s*\d",
        "'BRANC', 'PRET', 'CINZA', 'SORTID', 'MESCLA', 'AZUL', 'MARINHO', 'ROSA', "
        "'VERDE', 'VERMELH', 'BEGE', 'NUDE', 'AMAREL'",
    ):
        assert trecho in html, trecho


def test_conflito_sem_confirm_nativo(html):
    i = html.index("function mostrarConflitoVinculo(")
    fim = html.index("\n}\n", i)
    assert "confirm(" not in html[i:fim]


def _seed(produto: dict) -> str:
    entry_id = str(uuid.uuid4())
    repo.insert_import(
        {
            "id": entry_id,
            "source_filename": "R-1.pdf",
            "imported_at": datetime.now().isoformat(timespec="seconds"),
            "order_number": "R-1",
            "customer": "RIACHUELO",
            "customer_cnpj": "33.200.056/0001-49",
            "status": "success",
            "portal_status": "parsed",
            "snapshot": {
                "header": {"order_number": "R-1", "customer_name": "RIACHUELO"},
                "items": [{"description": "TENIS", "quantity": 1.0, "product_code": "KG02PR"}],
                "source_file": "",
            },
            "check": {"available": True, "items": [{"product_code": "KG02PR", "produto": produto}]},
        }
    )
    return entry_id


@pytest.fixture
def env_modo(monkeypatch):
    from app.web import server

    env = {"id": "test-env-id", "slug": "test", "produto_chave_modo": "desligado"}
    monkeypatch.setattr(server, "_request_environment", lambda request: env)

    def definir(modo):
        env["produto_chave_modo"] = modo

    return definir


@pytest.mark.parametrize("modo", ["desligado", "observando", "ligado"])
def test_preview_expoe_produto_chave_modo(env_modo, modo):
    env_modo(modo)
    entry_id = _seed({"estado": "a_identificar", "motivo": "x", "candidatos": []})

    r = client.get(f"/api/imported/{entry_id}/preview")

    assert r.status_code == 200, r.text
    assert r.json()["produto_chave_modo"] == modo


def test_preview_sem_ambiente_e_desligado(monkeypatch):
    from app.web import server

    monkeypatch.setattr(server, "_request_environment", lambda request: None)
    entry_id = _seed({"estado": "a_identificar", "motivo": "x", "candidatos": []})

    r = client.get(f"/api/imported/{entry_id}/preview")

    assert r.json()["produto_chave_modo"] == "desligado"


@pytest.mark.parametrize("modo", ["desligado", "observando", "ligado"])
def test_fila_expoe_modo(env_modo, modo):
    env_modo(modo)
    _seed({"estado": "a_identificar", "motivo": "x", "candidatos": []})

    r = client.get("/api/produtos/a-identificar")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["modo"] == modo
    assert len(body["itens"]) == 1


def test_confirmacao_nao_aceita_duplo_clique(html):
    """O 2º clique de um duplo clique não pode gravar: a etapa de confirmação usa
    outro botão, que nasce desabilitado e só libera depois de 400 ms."""
    i = html.index("function ppMostrarConfirmacao(")
    fim = html.index("\n}\n", i)
    corpo = html[i:fim]
    assert "ppVincularBtn" in corpo
    assert "disabled = true" in corpo
    assert "400" in corpo
    j = html.index("async function confirmProductLink(")
    corpo_confirm = html[j : html.index("\n}\n", j)]
    assert "ppVincular(" not in corpo_confirm.split("modoNovo", 1)[1].split("}", 1)[0]


def test_foco_inicial_sem_atalho_or(html):
    assert "?.focus()\n      || " not in html
    assert "?.focus() ||" not in html


def test_sem_texto_antigo(html):
    assert "Você pode vincular mesmo assim" not in html
    assert "Conferir de novo" not in html
    assert "Conferindo…" not in html
    assert "'Chaves discordam'" not in html


# ── Fix round 2: a tela espelha a trava do backend (export mode incluído) ────

_OK_COM_TROCA = {
    "estado": "ok",
    "fire_seq": 10,
    "fonte": "ean",
    "motivo": None,
    "candidatos": [{"seq": 10, "descricao": "TENIS", "codprod_altern": "TA-10"}],
    "troca_no_fire": {"seq_previsto": 99, "codigo": "TA-10"},
}
_OK_SEM_CODIGO = {
    "estado": "ok",
    "fire_seq": 11,
    "fonte": "ean",
    "motivo": None,
    "candidatos": [{"seq": 11, "descricao": "TENIS", "codprod_altern": None}],
    "troca_no_fire": None,
}


@pytest.fixture
def export_mode(monkeypatch):
    from app.web import server

    estado = {"m": "xlsx"}
    monkeypatch.setattr(server, "_export_mode_atual", lambda: estado["m"])

    def definir(m):
        estado["m"] = m

    return definir


@pytest.mark.parametrize(
    ("modo_export", "produto", "bloqueado", "reason"),
    [
        ("xlsx", _OK_COM_TROCA, True, "troca_no_fire"),
        ("both", _OK_COM_TROCA, True, "troca_no_fire"),
        ("db", _OK_COM_TROCA, False, None),
        ("xlsx", _OK_SEM_CODIGO, True, "sem_codigo_fire"),
        ("db", _OK_SEM_CODIGO, False, None),
    ],
)
def test_preview_expoe_trava_do_produto(
    env_modo, export_mode, modo_export, produto, bloqueado, reason
):
    env_modo("ligado")
    export_mode(modo_export)
    entry_id = _seed(produto)

    trava = client.get(f"/api/imported/{entry_id}/preview").json()["produto_trava"]

    assert trava["bloqueado"] is bloqueado
    assert trava["reason"] == reason
    assert trava["export_mode"] == modo_export


def test_preview_trava_em_observando_lista_sem_bloquear(env_modo, export_mode):
    env_modo("observando")
    entry_id = _seed(_OK_COM_TROCA)

    trava = client.get(f"/api/imported/{entry_id}/preview").json()["produto_trava"]

    assert trava["bloqueado"] is False
    assert [(i["index"], i["estado"]) for i in trava["itens"]] == [(0, "troca_no_fire")]


@pytest.mark.parametrize(("modo_export", "esperado"), [("xlsx", ["troca_no_fire"]), ("db", [])])
def test_fila_inclui_troca_no_fire_quando_sai_planilha(
    env_modo, export_mode, modo_export, esperado
):
    env_modo("observando")
    export_mode(modo_export)
    _seed(_OK_COM_TROCA)

    itens = client.get("/api/produtos/a-identificar").json()["itens"]

    assert [i["estado"] for i in itens] == esperado
    if esperado:
        assert itens[0]["motivo"] == "O importador do Fire trocaria este código pelo produto 99."


def test_tela_usa_a_trava_do_backend(html):
    assert "data.produto_trava" in html


def test_destaque_da_fila_nao_rola_ate_o_fim(html):
    i = html.index("function focarItemDoPreview(")
    corpo = html[i : html.index("\n}\n", i)]
    assert "inline: 'end'" not in corpo


def test_vincular_aplica_a_trava_da_resposta(html):
    """Depois do vínculo a tela usa a trava NOVA (senão o envio fica bloqueado)."""
    i = html.index("async function ppVincular(")
    corpo = html[i : html.index("\n}\n", i)]
    assert "currentPreview.produto_trava = body.produto_trava" in corpo
    assert corpo.index("currentPreview.produto_trava") < corpo.index(
        "renderPreview(currentPreview)"
    )


def test_erro_de_catalogo_trava_o_vincular_ate_trocar_de_produto(html):
    i = html.index("function _ppMostrarErro(")
    corpo = html[i : html.index("\n}\n", i)]
    assert "PP_ERRO_CATALOGO" in corpo
    assert "ppVincularBtn').disabled = true" in corpo
    assert "ppVoltarBtn').focus()" in corpo
    assert "ppState.produtoInvalido" in html[html.index("function selectProdutoRow(") :]


def test_fila_intro_nova(html):
    assert (
        "Itens dos pedidos em revisão que ainda precisam de atenção antes de ir para o Fire."
        in html
    )


def test_trava_do_preview_falha_fechada(monkeypatch, env_modo):
    from app.erp import produto_trava

    def explode(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(produto_trava, "avaliar", explode)
    env_modo("ligado")
    entry_id = _seed({"estado": "a_identificar", "motivo": "x", "candidatos": []})

    r = client.get(f"/api/imported/{entry_id}/preview")

    assert r.status_code == 200, r.text
    trava = r.json()["produto_trava"]
    assert trava["bloqueado"] is True
    assert trava["reason"] == "fire_indisponivel"
