# tests/test_flowpcp_intercompany.py
from __future__ import annotations

from unittest.mock import MagicMock

import app.integrations.flowpcp.intercompany as ic
from app.erp.depara_cliente import ResolucaoCliente
from app.models.order import Order, OrderHeader, OrderItem

_NASMAR = "34513679000134"


def _order(cnpj: str | None = "34.513.679/0001-34", numero: str | None = "AF066") -> Order:
    return Order(
        header=OrderHeader(order_number=numero, customer_name="Nasmar", customer_cnpj=cnpj),
        items=[OrderItem(description="MEIA", quantity=1)],
    )


def _env(**over):
    base = {
        "id": "e1",
        "slug": "mm",
        "intercompany_cnpj": _NASMAR,
        "intercompany_env_slug": "nasmar",
    }
    base.update(over)
    return base


def test_nao_se_aplica_quando_config_vazia(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env(intercompany_cnpj=None))
    chamou = MagicMock()
    monkeypatch.setattr(ic, "resolver_cliente_real", chamou)
    assert ic.resolucao_para(_order(), slug="mm") is None
    chamou.assert_not_called()


def test_nao_se_aplica_quando_cliente_nao_e_a_revenda(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    chamou = MagicMock()
    monkeypatch.setattr(ic, "resolver_cliente_real", chamou)
    assert ic.resolucao_para(_order(cnpj="06.347.409/0296-51"), slug="mm") is None
    chamou.assert_not_called()


def test_cnpj_casa_mesmo_formatado_diferente(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    esperado = ResolucaoCliente(True, cnpj="10772208000182", nome="AF", motivo="ok")
    monkeypatch.setattr(ic, "resolver_cliente_real", lambda chave, *, revenda_slug: esperado)
    assert ic.resolucao_para(_order(cnpj="34.513.679/0001-34"), slug="mm") is esperado


def test_usa_order_number_como_chave(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())
    visto = {}

    def _fake(chave, *, revenda_slug):
        visto["chave"] = chave
        visto["slug"] = revenda_slug
        return ResolucaoCliente(False, motivo="nao_encontrado")

    monkeypatch.setattr(ic, "resolver_cliente_real", _fake)
    ic.resolucao_para(_order(numero="AF066"), slug="mm")
    assert visto == {"chave": "AF066", "slug": "nasmar"}


def test_ambiente_inexistente_nao_levanta(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: None)
    assert ic.resolucao_para(_order(), slug="fantasma") is None


def test_erro_no_resolver_nao_levanta(monkeypatch):
    monkeypatch.setattr(ic.environments_repo, "get_by_slug", lambda s: _env())

    def _boom(chave, *, revenda_slug):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ic, "resolver_cliente_real", _boom)
    r = ic.resolucao_para(_order(), slug="mm")
    assert r is not None and r.resolvido is False and r.motivo == "erro_conexao"


def test_erro_no_get_by_slug_nao_levanta(monkeypatch):
    # Regressão: a leitura do ambiente também precisa estar sob o guard, não
    # só o resolver — uma falha de SQLite (lock/IO) não pode escapar.
    def _boom(s):
        raise RuntimeError("sqlite lock")

    monkeypatch.setattr(ic.environments_repo, "get_by_slug", _boom)
    chamou = MagicMock()
    monkeypatch.setattr(ic, "resolver_cliente_real", chamou)
    r = ic.resolucao_para(_order(), slug="mm")
    assert r is not None and r.resolvido is False and r.motivo == "erro_conexao"
    chamou.assert_not_called()


import app.integrations.flowpcp.hook as hook  # noqa: E402
from app.integrations.flowpcp.config import FlowPCPConfig  # noqa: E402

_CFG = FlowPCPConfig(
    enabled=True, base_url="https://flow.test", service_token="t", tenant_id="uuid"
)


def test_hook_repassa_resolucao_e_audita(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    res = ResolucaoCliente(
        True,
        cnpj="10772208000182",
        nome="AF",
        motivo="ok",
        pedidos_no_4=[{"codigo": 1, "status": "FATURADO", "codnf": 9}],
        revenda_slug="nasmar",
    )
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: res)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append((i, e, d)))

    fake_exporter = MagicMock()
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: fake_exporter)
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")

    envio = fake_exporter.export if fake_exporter.export.called else fake_exporter.enqueue
    assert envio.call_args.kwargs["resolucao"] is res
    assert auditado[0][1] == "depara_cliente"
    assert auditado[0][2]["motivo"] == "ok"
    assert auditado[0][2]["cnpj_real"] == "10772208000182"
    assert auditado[0][2]["pedidos_no_4"] == [{"codigo": 1, "status": "FATURADO", "codnf": 9}]


def test_hook_audita_revenda_slug_e_cnpj_gatilho(monkeypatch):
    """Achado 3 da revisão: se `intercompany_env_slug`/`intercompany_cnpj`
    estiver mal configurado, a única forma de auditar depois quais pedidos
    passaram por ele é o próprio audit registrar QUAL slug respondeu e QUAL
    CNPJ disparou o de-para."""
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    res = ResolucaoCliente(True, cnpj="10772208000182", nome="AF", motivo="ok", revenda_slug="nasmar")
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: res)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append((i, e, d)))
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(cnpj="34.513.679/0001-34"), import_id="imp-1", slug="mm")

    detail = auditado[0][2]
    assert detail["revenda_slug"] == "nasmar"
    assert detail["cnpj_gatilho"] == "34513679000134"


def test_hook_audita_revenda_slug_mesmo_em_config_invalida(monkeypatch):
    """config_invalida hoje não diz QUAL slug era inválido — revenda_slug no
    resultado fecha isso mesmo em falha."""
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    res = ResolucaoCliente(False, motivo="config_invalida", revenda_slug="fantasma")
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: res)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append((i, e, d)))
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")

    assert auditado[0][2]["revenda_slug"] == "fantasma"


def test_hook_cap_pedidos_no_4_em_50_mas_grava_total(monkeypatch):
    """Achado 5a: uma chave genérica (PULMÃO, AF, GFNASMAR) pode casar
    centenas de linhas na revenda — a lista auditada fica limitada, mas a
    contagem real sempre é gravada. Não mexe na query SQL (isso esconderia um
    segundo CNPJ e quebraria a detecção de ambiguidade)."""
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    muitos = [{"codigo": i, "status": "PEDIDO", "codnf": None} for i in range(120)]
    res = ResolucaoCliente(False, motivo="ambiguo", pedidos_no_4=muitos, revenda_slug="nasmar")
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: res)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append((i, e, d)))
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")

    detail = auditado[0][2]
    assert len(detail["pedidos_no_4"]) == 50
    assert detail["pedidos_no_4_total"] == 120

def test_hook_nao_audita_quando_nao_se_aplica(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    monkeypatch.setattr(hook, "resolucao_para", lambda order, *, slug: None)
    auditado = []
    monkeypatch.setattr(hook.repo, "append_audit", lambda i, e, d=None: auditado.append(e))
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")
    assert auditado == []


def test_hook_nao_derruba_o_push_se_o_audit_falhar(monkeypatch):
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)
    monkeypatch.setattr(
        hook, "resolucao_para", lambda order, *, slug: ResolucaoCliente(False, motivo="ambiguo")
    )

    def _boom(*a, **k):
        raise RuntimeError("audit fora")

    monkeypatch.setattr(hook.repo, "append_audit", _boom)
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")  # não pode levantar


def test_hook_nao_levanta_se_resolucao_para_falhar(monkeypatch):
    # Defesa em profundidade: mesmo que `resolucao_para` reintroduza uma falha
    # não protegida no futuro, o `push_new_order` — contrato usado direto por
    # app/web/server.py logo após o Fire/XLS já ter tido sucesso — não pode
    # propagar.
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)

    def _boom(order, *, slug):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(hook, "resolucao_para", _boom)
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")  # não pode levantar


def test_hook_nao_levanta_quando_get_by_slug_falha_no_encadeamento_real(monkeypatch):
    # Reproduz o repro do review: sem mockar `resolucao_para`, o encadeamento
    # real hook -> intercompany -> environments_repo não pode deixar uma
    # falha de `get_by_slug` (SQLite lock, IO, permissão) escapar de
    # `push_new_order` — ele é chamado logo após o Fire/XLS já ter sucedido.
    monkeypatch.setattr(hook, "flowpcp_config_for_slug", lambda slug: _CFG)

    def _boom(s):
        raise RuntimeError("sqlite lock")

    monkeypatch.setattr(ic.environments_repo, "get_by_slug", _boom)
    monkeypatch.setattr(hook, "FlowPCPExporter", lambda *a, **k: MagicMock())
    monkeypatch.setattr(hook, "FlowPCPClient", lambda **_kw: MagicMock(), raising=False)

    hook.push_new_order(_order(), import_id="imp-1", slug="mm")  # não pode levantar
