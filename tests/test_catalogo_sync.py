from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_sync
from app.integrations.flowpcp.catalogo_schema import CatalogoReconciliacaoResponse


class _FakeClient:
    def __init__(self):
        self.sent = None

    def send_catalogo(self, request):
        self.sent = request
        return CatalogoReconciliacaoResponse(
            match_limpo=0, fire_only=len(request.itens), fire_pk_presente=True
        )

    def close(self):
        pass


def test_run_sync_extrai_empurra_e_devolve_relatorio(monkeypatch):
    dtos = [
        ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples"),
        ProdutoFireDTO("2", "2", "Y", "PC", "789", False, "kit"),
    ]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn: dtos)

    class _Cfg:
        enabled = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())

    fake_client = _FakeClient()
    rep = catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="2026-06-29T00:00:00Z",
        _client=fake_client,
        _fire_conn=object(),
    )
    assert rep.fire_only == 2 and rep.fire_pk_presente is True
    assert fake_client.sent.dryRun is True
    assert fake_client.sent.fullSync is True
    assert len(fake_client.sent.itens) == 2


def test_run_sync_none_quando_flowpcp_desabilitado(monkeypatch):
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: None)
    assert catalogo_sync.run_catalogo_sync("mm", _client=object(), _fire_conn=object()) is None
