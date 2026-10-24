"""PORTAL_FLOWPCP_CATALOGO_INCLUIR_<SLUG>: SEQs que entram no catálogo mesmo fora do recorte.

Caso de uso (23/10/2026): 3 pares ativos da MM (3851, 3853, 3854) não eram do subgrupo MEIAS, não tinham
venda nos últimos 12 meses e ainda não eram componente de nenhum kit, então o recorte os deixava de fora e
4 composições do Flow esperavam por eles. Irmã de EXCLUIR: mesma sintaxe, mesma falha fechada.
"""

from __future__ import annotations

import pytest

from app.erp import queries
from app.erp.catalog_extract import ProdutoFireDTO, extract_produtos
from app.integrations.flowpcp import catalogo_composicao as cc
from app.integrations.flowpcp import catalogo_sync

VAR = "PORTAL_FLOWPCP_CATALOGO_INCLUIR_MM"


# ── a variável ────────────────────────────────────────────────────────────────


def test_incluir_guardado_normaliza_e_ignora_vazios(monkeypatch):
    monkeypatch.setenv(VAR, " 3851, 03853 ,3854,")
    assert cc.incluir_guardado("mm") == frozenset({"3851", "3853", "3854"})


def test_incluir_guardado_ausente_e_vazio(monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert cc.incluir_guardado("mm") == frozenset()


def test_incluir_guardado_slug_com_hifen(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_INCLUIR_MM_AMER", "1")
    assert cc.incluir_guardado("mm-amer") == frozenset({"1"})


@pytest.mark.parametrize("valor", ["3851;3853", "abc", "٣١", "12345678901"])
def test_incluir_guardado_invalido_falha_fechado_citando_a_variavel(monkeypatch, valor):
    monkeypatch.setenv(VAR, valor)
    with pytest.raises(cc.IncluirGuardadoInvalidoError, match=VAR):
        cc.incluir_guardado("mm")


def test_excluir_continua_igual(monkeypatch):
    """O helper compartilhado não pode mudar o comportamento de EXCLUIR."""
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", " 3174, 03381 ,")
    assert cc.excluir_guardado("mm") == frozenset({"3174", "3381"})
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "abc")
    with pytest.raises(cc.ExcluirGuardadoInvalidoError, match="EXCLUIR_MM"):
        cc.excluir_guardado("mm")


# ── a consulta ────────────────────────────────────────────────────────────────


def test_sql_sem_incluir_e_a_consulta_de_sempre():
    assert queries.list_produtos_catalogo_meias(0) == queries.LIST_PRODUTOS_CATALOGO_MEIAS


def test_sql_com_incluir_soma_os_seqs_as_sementes_e_nao_toca_no_resto():
    sql = queries.list_produtos_catalogo_meias(3)
    assert sql.count("?") == 3
    assert "P.SEQ IN (?, ?, ?)" in sql
    # só acrescentou: tudo o que a consulta original tinha continua lá
    for trecho in (
        "GRUPO_PRODUTOS_SUB",
        "NOT CONTAINING 'CUECA'",
        "NOT CONTAINING 'BOXER'",
        "NIVEL < 5",
        "FROM PRODUTOS_KIT K",
    ):
        assert trecho in sql
    assert sql.index("P.SEQ IN (?, ?, ?)") < sql.index("UNION ALL")


def test_sql_so_acrescenta_na_semente_nao_na_recursao():
    sql = queries.list_produtos_catalogo_meias(1)
    assert sql.count("P.SEQ IN (?)") == 1


# ── a extração ────────────────────────────────────────────────────────────────


class _Cur:
    def __init__(self):
        self.chamadas = []

    def execute(self, sql, params=None):
        self.chamadas.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        pass


class _Conn:
    def __init__(self):
        self.cur = _Cur()

    def cursor(self):
        return self.cur


def test_extract_com_incluir_e_apenas_meias_manda_os_seqs_como_inteiros_em_ordem():
    conn = _Conn()
    extract_produtos(conn, apenas_meias=True, incluir=frozenset({"3854", "3851", "3853"}))
    sql, params = conn.cur.chamadas[0]
    assert sql == queries.list_produtos_catalogo_meias(3)
    assert params == [3851, 3853, 3854]


def test_extract_sem_incluir_chama_como_antes_sem_parametros():
    conn = _Conn()
    extract_produtos(conn, apenas_meias=True)
    assert (
        conn.cur.chamadas == [(queries.LIST_PRODUTOS_CATALOGO_MEIAS, None)]
        or conn.cur.chamadas[0][0] == queries.LIST_PRODUTOS_CATALOGO_MEIAS
    )


def test_extract_sem_apenas_meias_ignora_incluir_porque_ja_vem_tudo():
    conn = _Conn()
    extract_produtos(conn, apenas_meias=False, incluir=frozenset({"3851"}))
    assert conn.cur.chamadas[0][0] == queries.LIST_PRODUTOS_CATALOGO


# ── o sync ────────────────────────────────────────────────────────────────────


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_catalogo(self, request):  # pragma: no cover - não deve ser chamado nos erros
        self.sent = request
        raise AssertionError("não devia enviar")

    def close(self):
        pass


class _FakeEnvConn:
    def execute(self, sql, params=()):
        class _C:
            @staticmethod
            def fetchone():
                return (0,)

            @staticmethod
            def fetchall():
                return []

        return _C()

    def executemany(self, sql, rows):
        pass


class _Cfg:
    enabled = True
    catalogo_push = True
    catalogo_apenas_meias = True


@pytest.fixture
def _sync(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", raising=False)
    monkeypatch.delenv(VAR, raising=False)
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    visto = {}

    def extrair(conn, **kw):
        visto.update(kw)
        return [
            ProdutoFireDTO("3174", "3174", "PAR", "PARES", None, True, "simples"),
            ProdutoFireDTO("3851", "3851", "ACT VITTA", "PARES", None, True, "simples"),
        ]

    monkeypatch.setattr(catalogo_sync, "extract_produtos", extrair)

    def rodar(**kw):
        cliente = _FakeClient()
        return (
            cliente,
            visto,
            lambda: catalogo_sync.run_catalogo_sync(
                "mm",
                now_iso="t",
                _client=cliente,
                _fire_conn=object(),
                _env_conn=_FakeEnvConn(),
                **kw,
            ),
        )

    return rodar


def test_sync_repassa_o_incluir_para_a_extracao(_sync, monkeypatch):
    monkeypatch.setenv(VAR, "3851")
    cliente, visto, executar = _sync(dry_run=True)
    try:
        executar()
    except AssertionError:
        pass
    assert visto["incluir"] == frozenset({"3851"})
    assert visto["apenas_meias"] is True


def test_sync_incluir_com_seq_que_nao_existe_no_fire_nao_envia_nada(_sync, monkeypatch):
    monkeypatch.setenv(VAR, "3851,9999")
    cliente, _, executar = _sync(dry_run=True)
    with pytest.raises(catalogo_sync.IncluirInexistenteError) as e:
        executar()
    assert e.value.seqs == ["9999"]
    assert e.value.variavel == VAR
    assert cliente.sent is None


def test_sync_incluir_invalido_nao_envia_nada(_sync, monkeypatch):
    monkeypatch.setenv(VAR, "abc")
    cliente, _, executar = _sync(dry_run=True)
    with pytest.raises(cc.IncluirGuardadoInvalidoError):
        executar()
    assert cliente.sent is None


def test_sync_sem_incluir_extrai_como_antes(_sync):
    _, visto, executar = _sync(dry_run=True)
    try:
        executar()
    except AssertionError:
        pass
    assert visto["incluir"] == frozenset()
