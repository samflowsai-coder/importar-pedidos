from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_sync
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoContagens,
    CatalogoReconciliacaoResponse,
)


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_catalogo(self, request):
        self.sent = request
        return CatalogoReconciliacaoResponse(
            contagens=CatalogoContagens(fire_only=len(request.itens)),
            fire_pk_presente="todos",
        )

    def close(self):
        pass


class _FakeEnvConn:
    """Conexão SQLite fake — registra o que o repo local gravaria."""

    def __init__(self):
        self.executed = []

    def execute(self, sql, params=()):
        self.executed.append(sql)

        class _Cur:
            @staticmethod
            def fetchone():
                return (0,)

            @staticmethod
            def fetchall():
                return []

        return _Cur()

    def executemany(self, sql, rows):
        self.executed.append(sql)
        self.rows = list(rows)


def test_run_sync_extrai_empurra_e_devolve_relatorio(monkeypatch):
    dtos = [
        ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples"),
        ProdutoFireDTO("2", "2", "Y", "PC", "789", False, "kit"),
    ]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True  # gate ON — sem ele o sync é local-only

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    fake_client = _FakeClient()
    rep = catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="2026-06-29T00:00:00Z",
        _client=fake_client,
        _fire_conn=object(),
        _env_conn=_FakeEnvConn(),
    )
    assert rep.contagens.fire_only == 2 and rep.fire_pk_presente == "todos"
    assert fake_client.sent.dryRun is True
    assert fake_client.sent.fullSync is True
    assert len(fake_client.sent.itens) == 2


def test_run_sync_none_quando_flowpcp_desabilitado(monkeypatch):
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: None)
    assert catalogo_sync.run_catalogo_sync("mm", _client=object(), _fire_conn=object()) is None


def test_run_sync_local_only_quando_gate_off(monkeypatch):
    """catalogo_push OFF → extrai + grava local, NÃO envia ao Flow."""
    dtos = [ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = False

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    fake_client = _FakeClient()
    env_conn = _FakeEnvConn()
    rep = catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="2026-07-11T00:00:00Z",
        _client=fake_client,
        _fire_conn=object(),
        _env_conn=env_conn,
    )
    assert isinstance(rep, catalogo_sync.CatalogoLocalResult)
    assert rep.itens == 1
    assert rep.extraido_em == "2026-07-11T00:00:00Z"
    assert fake_client.sent is None  # nada foi ao Flow
    assert any("catalogo_fire" in sql for sql in env_conn.executed)  # local gravado


def test_run_sync_com_gate_on_grava_local_e_envia(monkeypatch):
    dtos = [ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    fake_client = _FakeClient()
    env_conn = _FakeEnvConn()
    rep = catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="t",
        _client=fake_client,
        _fire_conn=object(),
        _env_conn=env_conn,
    )
    assert rep.fire_pk_presente == "todos"  # relatório do Flow
    assert fake_client.sent is not None
    assert any("catalogo_fire" in sql for sql in env_conn.executed)


def _spy_extract(monkeypatch, dtos):
    """Monkeypatcha extract_produtos por um spy que CAPTURA o kwarg apenas_meias.
    Retorna o dict de captura — o teste garante que o flag chega no extract
    (um rename/typo do campo viraria no-op → vazaria o catálogo inteiro)."""
    captura: dict = {}

    def fake(conn, *, apenas_meias=False):
        captura["apenas_meias"] = apenas_meias
        return dtos

    monkeypatch.setattr(catalogo_sync, "extract_produtos", fake)
    return captura


def _run_sync_capturando_flag(monkeypatch, *, catalogo_apenas_meias):
    dtos = [ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples")]
    captura = _spy_extract(monkeypatch, dtos)

    class _Cfg:
        enabled = True
        catalogo_push = False  # local-only: isola o teste no wiring do extract

    _Cfg.catalogo_apenas_meias = catalogo_apenas_meias
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="t",
        _client=_FakeClient(),
        _fire_conn=object(),
        _env_conn=_FakeEnvConn(),
    )
    return captura


def test_flag_apenas_meias_true_chega_no_extract(monkeypatch):
    """catalogo_apenas_meias=True na config → extract_produtos(apenas_meias=True)."""
    captura = _run_sync_capturando_flag(monkeypatch, catalogo_apenas_meias=True)
    assert captura["apenas_meias"] is True


def test_flag_apenas_meias_false_chega_no_extract(monkeypatch):
    """catalogo_apenas_meias=False → extract_produtos(apenas_meias=False) (default: tudo)."""
    captura = _run_sync_capturando_flag(monkeypatch, catalogo_apenas_meias=False)
    assert captura["apenas_meias"] is False
