"""Catálogo do Fire ao Flow uma vez por dia (`catalogo_diario`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.integrations.flowpcp import catalogo_diario
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse
from app.utils.janelas_envio import FUSO


@pytest.fixture(autouse=True)
def _limpo(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_HORA", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", raising=False)
    monkeypatch.setattr(catalogo_diario, "_pausa", {})


def _envs(monkeypatch, **push):
    monkeypatch.setattr(
        catalogo_diario,
        "enabled_flowpcp_envs",
        lambda: {slug: SimpleNamespace(catalogo_push=ligado) for slug, ligado in push.items()},
    )


class _Sync:
    def __init__(self, erro=None):
        self.chamadas = []
        self.erro = erro

    def __call__(self, slug, **kw):
        self.chamadas.append((slug, kw))
        if self.erro:
            raise self.erro
        return SimpleNamespace(contagens=SimpleNamespace(model_dump=lambda **_: {"matchLimpo": 1}))


CINCO_E_DEZ = datetime(2026, 10, 16, 5, 10, tzinfo=FUSO)


def test_por_padrao_so_simula(monkeypatch):
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    enviados = catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None)
    assert enviados == ["mm"]
    assert sync.chamadas == [("mm", {"dry_run": True, "full_sync": True})]


def test_grava_so_quando_mandado(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", "gravar")
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None)
    assert sync.chamadas == [("mm", {"dry_run": False, "full_sync": True})]


@pytest.mark.parametrize("valor", ["desligado", "DESLIGADO"])
def test_desligado_nao_manda(monkeypatch, valor):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", valor)
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    assert catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None) == []
    assert sync.chamadas == []


def test_modo_invalido_nunca_vira_gravar(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", "sim")
    assert catalogo_diario.modo_do_catalogo() == "simular"


def test_fora_da_hora_nao_manda(monkeypatch):
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    assert catalogo_diario.rodar(CINCO_E_DEZ + timedelta(hours=1), sync=sync) == []
    assert sync.chamadas == []


def test_so_onde_o_envio_do_catalogo_esta_ligado(monkeypatch):
    _envs(monkeypatch, mm=True, nasmar=False)
    sync = _Sync()
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None)
    assert [s for s, _ in sync.chamadas] == ["mm"]


def test_uma_vez_por_dia_pela_copia_local(monkeypatch):
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    hoje_5h01 = datetime(2026, 10, 16, 8, 1, tzinfo=UTC)  # 5:01 em Brasília
    ontem = hoje_5h01 - timedelta(days=1)
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: hoje_5h01)
    assert sync.chamadas == []
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: ontem)
    assert len(sync.chamadas) == 1


def test_falha_pausa_e_nao_derruba_os_outros(monkeypatch):
    _envs(monkeypatch, mm=True)
    sync = _Sync(erro=RuntimeError("Fire fora"))
    assert catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None) == []
    catalogo_diario.rodar(CINCO_E_DEZ + timedelta(minutes=5), sync=sync, ultima_extracao=lambda s: None)
    assert len(sync.chamadas) == 1  # dentro da pausa não tenta de novo
    catalogo_diario.rodar(CINCO_E_DEZ + timedelta(minutes=11), sync=sync, ultima_extracao=lambda s: None)
    assert len(sync.chamadas) == 2


def test_hora_configuravel_e_invalida_cai_no_padrao(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_HORA", "3")
    assert catalogo_diario.hora_do_catalogo() == 3
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_HORA", "25")
    assert catalogo_diario.hora_do_catalogo() == 5
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_HORA", "x")
    assert catalogo_diario.hora_do_catalogo() == 5


def test_ultima_extracao_le_a_copia_local(monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE catalogo_fire (extraido_em TEXT)")

    @contextmanager
    def _env_connect(slug):
        yield conn

    monkeypatch.setattr(catalogo_diario.router, "env_connect", _env_connect)
    assert catalogo_diario._ultima_extracao("mm") is None
    conn.execute("INSERT INTO catalogo_fire VALUES ('2026-10-16T08:01:00Z')")  # 5:01 em Brasília
    ultima = catalogo_diario._ultima_extracao("mm")
    assert ultima == datetime(2026, 10, 16, 8, 1, tzinfo=UTC)
    assert catalogo_diario.devido(CINCO_E_DEZ, ultima, 5) is False


def _rep(**extra):
    return CatalogoReconciliacaoResponse.model_validate({"contagens": {"matchLimpo": 1}, **extra})


def test_resumo_traz_os_blocos_de_composicao():
    texto = catalogo_diario._resumo(
        _rep(composicao={"aCriar": 3}, vinculoMes={"novos": 2}, composicao_portal={"com_lista": 271})
    )
    assert "'aCriar': 3" in texto and "'novos': 2" in texto and "'com_lista': 271" in texto


@pytest.mark.parametrize(
    "extra",
    [
        {"composicao": {"travaRemocaoAtiva": True}},
        {"composicao": {"travaSubstituicaoAtiva": True, "travaSubstituicao": ["2544"]}},
        {"composicao": {"erpSemChave": 4}},
        {"composicao_portal": {"leitura_ok": False}},
        {"composicao_portal": {"leitura_ok": True, "divergencia_nome": {"3151": "nome diz 22, composição soma 1"}}},
        {"fiscal_portal": {"capacidade_ok": False}},
    ],
)
def test_atencao_quando_trava_falha_ou_kit_sem_chave(extra):
    assert catalogo_diario._precisa_atencao(_rep(**extra)) is True


def test_sem_atencao_no_normal():
    rep = _rep(
        composicao={"travaRemocaoAtiva": False, "erpSemChave": 0},
        composicao_portal={"leitura_ok": True},
    )
    assert catalogo_diario._precisa_atencao(rep) is False


def test_diario_nunca_manda_confirmacao(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_DIARIO", "gravar")
    _envs(monkeypatch, mm=True)
    sync = _Sync()
    catalogo_diario.rodar(CINCO_E_DEZ, sync=sync, ultima_extracao=lambda s: None)
    ((_, kw),) = sync.chamadas
    assert "confirmar_remocao" not in kw and "confirmar_substituicao" not in kw
