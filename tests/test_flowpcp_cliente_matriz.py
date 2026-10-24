"""PORTAL_FLOWPCP_CLIENTE_MATRIZ_<SLUG>: pedido de rede sem CNPJ no header vai ao Flow como UM pedido,
de um cliente "matriz" fixo (decisão de 24/10/2026 para o Riachuelo).

O Fire continua dividindo por loja. O Flow liga item a item pela chave `import_id:posição`, que não
depende do cliente; o que o cliente fixo dá é `cliente_id` preenchido (preço e aprovação por rede) e
uma aprovação só, em vez de N pedidos por loja.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.erp import cnpj as cnpj_mod
from app.erp.depara_cliente import ResolucaoCliente
from app.integrations.flowpcp import cliente_matriz as cm
from app.models.order import Order, OrderHeader, OrderItem
from app.persistence import context as env_context
from app.persistence import db, environments_repo, outbox_repo, repo

VAR = "PORTAL_FLOWPCP_CLIENTE_MATRIZ_MM"
RIACHUELO = "riachuelo:33200056000149:Matriz Riachuelo"
LOJA_A = "33.200.056/0313-70"
LOJA_B = "33.200.056/0343-96"


def _order(nome: str = "LOJAS RIACHUELO SA", cnpj_header: str | None = None) -> Order:
    return Order(
        header=OrderHeader(
            order_number="6702667778",
            issue_date="02/10/2026",
            customer_name=nome,
            customer_cnpj=cnpj_header,
        ),
        items=[
            OrderItem(
                description=f"MEIA {i}",
                product_code=f"P{i}",
                quantity=12.0,
                unit_price=10.0,
                delivery_cnpj=cnpj,
            )
            for i, cnpj in enumerate([LOJA_A, LOJA_B, LOJA_A])
        ],
    )


# ── dígito verificador ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "valor", ["33200056000149", "33.200.056/0001-49", "33200056036097", "33200056034396"]
)
def test_cnpj_dv_ok_aceita_cnpj_de_verdade(valor):
    assert cnpj_mod.cnpj_dv_ok(valor) is True


@pytest.mark.parametrize(
    "valor",
    ["33200056000148", "11111111111111", "3320005600014", "", None, "abc", "332000560001499"],
)
def test_cnpj_dv_ok_recusa_o_resto(valor):
    assert cnpj_mod.cnpj_dv_ok(valor) is False


# ── a variável ────────────────────────────────────────────────────────────────


def test_ausente_ou_vazia_nao_tem_matriz(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert cm.configuradas("mm") == []
    monkeypatch.setenv(VAR, " ; ")
    assert cm.configuradas("mm") == []


def test_le_uma_entrada(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    [c] = cm.configuradas("mm")
    assert (c.termo, c.cnpj, c.nome) == ("riachuelo", "33200056000149", "Matriz Riachuelo")


def test_cnpj_com_mascara_vira_so_digitos_e_varias_entradas(monkeypatch):
    monkeypatch.setenv(
        VAR, "riachuelo:33.200.056/0001-49:Matriz Riachuelo; nba : 11222333000181 : Matriz NBA"
    )
    assert [(c.termo, c.cnpj, c.nome) for c in cm.configuradas("mm")] == [
        ("riachuelo", "33200056000149", "Matriz Riachuelo"),
        ("nba", "11222333000181", "Matriz NBA"),
    ]


def test_slug_com_hifen(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CLIENTE_MATRIZ_MM_AMER", RIACHUELO)
    assert len(cm.configuradas("mm-amer")) == 1


@pytest.mark.parametrize(
    "valor",
    [
        "riachuelo:33200056000148:Matriz Riachuelo",  # dígito verificador errado
        "riachuelo:33200056000149:",  # sem nome
        ":33200056000149:Matriz Riachuelo",  # sem termo
        "riachuelo:33200056000149",  # faltou o nome
        "riachuelo",
    ],
)
def test_entrada_invalida_e_ignorada_e_avisa(monkeypatch, valor):
    """Entrada ruim não derruba o envio do pedido: cai no comportamento de antes (e fica o WARNING)."""
    avisos = []
    monkeypatch.setattr(cm.logger, "warning", lambda msg: avisos.append(msg))
    monkeypatch.setenv(VAR, valor)
    assert cm.configuradas("mm") == []
    assert avisos and VAR in avisos[0]


def test_entrada_boa_ao_lado_de_ruim_continua_valendo(monkeypatch):
    monkeypatch.setattr(cm.logger, "warning", lambda msg: None)
    monkeypatch.setenv(VAR, "x:123:Y;" + RIACHUELO)
    assert [c.termo for c in cm.configuradas("mm")] == ["riachuelo"]


# ── quando vale para o pedido ─────────────────────────────────────────────────


def test_pedido_da_rede_sem_cnpj_no_header_casa(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    assert cm.para_pedido("mm", _order()).cnpj == "33200056000149"


def test_casa_sem_olhar_caixa_nem_acento(monkeypatch):
    monkeypatch.setenv(VAR, "guararapés:33200056000149:Matriz Riachuelo")
    assert cm.para_pedido("mm", _order("Lojas GUARARAPES S/A")) is not None


def test_outra_rede_nao_casa(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    assert cm.para_pedido("mm", _order("NBA STORE")) is None


def test_header_com_cnpj_nao_usa_a_matriz(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    assert cm.para_pedido("mm", _order(cnpj_header="12.345.678/0001-95")) is None


def test_intercompany_resolvido_nao_usa_a_matriz(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    resolucao = ResolucaoCliente(resolvido=True, cnpj="12345678000195", nome="Real")
    assert cm.para_pedido("mm", _order(), resolucao) is None


def test_sem_variavel_nao_usa(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert cm.para_pedido("mm", _order()) is None


def test_sem_slug_nao_usa(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    assert cm.para_pedido(None, _order()) is None


# ── o payload e o enfileiramento ──────────────────────────────────────────────


def test_payload_leva_o_cliente_da_matriz_e_mantem_fornecedor_e_itens():
    from app.integrations.flowpcp.mapper import build_recebimento_payload

    matriz = cm.ClienteMatriz(termo="riachuelo", cnpj="33200056000149", nome="Matriz Riachuelo")
    corpo = build_recebimento_payload(
        import_id="imp-1", order=_order(), tenant_id="t", cliente_matriz=matriz
    ).model_dump(by_alias=True)
    assert corpo["cliente"] == {"nome": "Matriz Riachuelo", "cnpj": "33200056000149"}
    assert corpo["fornecedor"] == "LOJAS RIACHUELO SA"
    assert corpo["externalId"] == "imp-1"
    assert len(corpo["itens"]) == 3
    assert all("indiceOriginal" not in i for i in corpo["itens"])


def test_payload_sem_matriz_continua_igual():
    from app.integrations.flowpcp.mapper import build_recebimento_payload

    corpo = build_recebimento_payload(import_id="imp-1", order=_order(), tenant_id="t").model_dump(
        by_alias=True
    )
    assert corpo["cliente"]["cnpj"] is None
    assert corpo["cliente"]["nome"] == "LOJAS RIACHUELO SA"


@pytest.fixture
def env(tmp_path: Path):
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    e = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="x",
    )
    with env_context.active_env(e["id"], e["slug"]):
        repo.insert_import(
            {"id": "imp-1", "source_filename": "a.xlsx", "imported_at": "2026-10-02T09:00:00"}
        )
        yield e
    db.set_db_path(None)
    db.reset_init_cache()


def _exporter():
    from app.integrations.flowpcp.exporter import FlowPCPExporter

    return FlowPCPExporter(tenant_id="t")


def test_exporter_com_matriz_enfileira_uma_linha_com_o_cliente_fixo(env, monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    assert _exporter().enqueue(_order(), import_id="imp-1", slug="mm") is True
    [linha] = outbox_repo.list_for_import("imp-1")
    assert linha.idempotency_key == "send-imp-1"
    assert linha.payload["cliente"] == {"nome": "Matriz Riachuelo", "cnpj": "33200056000149"}


def test_exporter_matriz_ganha_da_divisao_por_loja(env, monkeypatch):
    """Com os dois ligados, a decisão do Samuel (um pedido só) vale; a divisão por loja fica para
    quem não tem matriz configurada."""
    monkeypatch.setenv(VAR, RIACHUELO)
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")
    linhas = outbox_repo.list_for_import("imp-1")
    assert [linha.idempotency_key for linha in linhas] == ["send-imp-1"]


def test_exporter_outra_rede_com_a_divisao_ligada_segue_por_loja(env, monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order("NBA STORE"), import_id="imp-1", slug="mm")
    assert len(outbox_repo.list_for_import("imp-1")) == 2


def test_exporter_sem_variavel_e_como_sempre(env, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")
    [linha] = outbox_repo.list_for_import("imp-1")
    assert linha.payload["cliente"]["cnpj"] is None


# ── achados da revisão ────────────────────────────────────────────────────────


def test_cnpj_dv_ok_recusa_digito_nao_ascii():
    assert cnpj_mod.cnpj_dv_ok("٣٣٢٠٠٠٥٦٠٠٠١٤٩") is False


def test_termo_casa_so_palavra_inteira(monkeypatch):
    """`nba` no meio de outra palavra levaria o pedido de OUTRA rede para o cliente errado."""
    monkeypatch.setenv(VAR, "nba:11222333000181:Matriz NBA")
    assert cm.para_pedido("mm", _order("NBA STORE")) is not None
    assert cm.para_pedido("mm", _order("LOJAS BANBAS LTDA")) is None
    assert cm.para_pedido("mm", _order("CONNBAZ SA")) is None


def test_resolucao_nao_resolvida_nao_impede_a_matriz(monkeypatch):
    monkeypatch.setenv(VAR, RIACHUELO)
    nao = ResolucaoCliente(resolvido=False)
    assert cm.para_pedido("mm", _order(), nao) is not None


def test_nome_com_dois_pontos_fica_inteiro(monkeypatch):
    monkeypatch.setenv(VAR, "riachuelo:33200056000149:Matriz: Riachuelo")
    [c] = cm.configuradas("mm")
    assert c.nome == "Matriz: Riachuelo"


def test_hook_push_leva_a_matriz_ate_a_fila(env, monkeypatch):
    import app.integrations.flowpcp.hook as hook
    from app.integrations.flowpcp.config import FlowPCPConfig

    cfg = FlowPCPConfig(
        enabled=True, base_url="https://flow.test", service_token="t", tenant_id="t"
    )
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: cfg)
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: None)
    monkeypatch.setenv(VAR, RIACHUELO)
    assert hook.push_new_order(_order(), import_id="imp-1", slug="mm") is True
    [linha] = outbox_repo.list_for_import("imp-1")
    assert linha.payload["cliente"] == {"nome": "Matriz Riachuelo", "cnpj": "33200056000149"}


def test_reexport_depois_de_ligar_a_matriz_nao_manda_o_mesmo_pedido_nos_dois_formatos(
    env, monkeypatch
):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")  # saiu por loja
    assert len(outbox_repo.list_for_import("imp-1")) == 2
    monkeypatch.setenv(VAR, RIACHUELO)  # a matriz foi ligada depois
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")  # re-export do XLS
    chaves = {linha.idempotency_key for linha in outbox_repo.list_for_import("imp-1")}
    assert "send-imp-1" not in chaves
    assert len(chaves) == 2


def test_reexport_depois_de_ligar_a_divisao_por_loja_tambem_nao_duplica(env, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", raising=False)
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")  # saiu único
    monkeypatch.setenv("PORTAL_FLOWPCP_RECEBIMENTO_POR_LOJA", "mm")
    _exporter().enqueue(_order(), import_id="imp-1", slug="mm")
    assert [linha.idempotency_key for linha in outbox_repo.list_for_import("imp-1")] == [
        "send-imp-1"
    ]


@pytest.mark.parametrize(
    ("termo", "nome"),
    [("(rj)", "LOJA (RJ) LTDA"), ("c&a", "C&A MODAS"), ("riachuelo", "RIACHUELO_SP")],
)
def test_termo_com_borda_nao_alfanumerica_ou_sublinhado_casa(monkeypatch, termo, nome):
    monkeypatch.setenv(VAR, f"{termo}:33200056000149:Matriz X")
    assert cm.para_pedido("mm", _order(nome)) is not None
