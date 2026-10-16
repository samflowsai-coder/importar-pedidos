import json

import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import capacidades, catalogo_sync
from app.integrations.flowpcp import catalogo_fiscal as cf
from app.integrations.flowpcp.catalogo_mapper import build_catalogo_request
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoContagens,
    CatalogoReconciliacaoResponse,
)


@pytest.mark.parametrize("bruto,esperado", [
    ("61159500", ("61159500", None)),
    ("61.159.500", ("61159500", None)),
    (" 6115.95.00 ", ("61159500", None)),
    ("6115-9500", ("61159500", None)),
    ("6115950061159500", (None, "ncm_invalido")),
    ("611595", (None, "ncm_invalido")),
    ("6115950A", (None, "ncm_invalido")),
    ("", (None, "ncm_vazio")),
    (None, (None, "ncm_vazio")),
])
def test_normalizar_ncm(bruto, esperado):
    assert cf.normalizar_ncm(bruto) == esperado


@pytest.mark.parametrize("bruto,esperado", [
    (3, (3, None)), (4, (4, None)), (0, (0, None)), (10, (10, None)), (99, (99, None)),
    ("04", (4, None)),
    (12, (None, "sped_invalido")), (-1, (None, "sped_invalido")), ("x", (None, "sped_invalido")),
    (None, (None, "sped_vazio")),
])
def test_normalizar_sped(bruto, esperado):
    assert cf.normalizar_sped(bruto) == esperado


def test_texto_apara_e_limita():
    assert cf.texto("  BRANCO ", 120) == ("BRANCO", False)
    assert cf.texto("", 120) == (None, False)
    assert cf.texto("x" * 121, 120) == (None, True)


def _dto(**kw):
    base = dict(fire_produto_id="2645", codigo="2645", nome="KIT3 MEIA CURTO BRANCO",
                unidade="KIT", ean=None, ativo=True, tipo="kit")
    base.update(kw)
    return ProdutoFireDTO(**base)


def test_campos_fiscais_completo_com_acento():
    campos, motivos = cf.campos_fiscais(_dto(
        ncm_bruto="61.159.500", sped_bruto=4, grupo_codigo=8, grupo_nome="CONFECCIONADO",
        codigo_nfe="KIT3-MEIA-CURTO/BCOM", cor="AÇÚCAR"))
    assert campos == {"ncm": "61159500", "tipoItemSped": 4,
                      "grupoErp": {"codigo": "8", "nome": "CONFECCIONADO"},
                      "codigoNfe": "KIT3-MEIA-CURTO/BCOM", "cor": "AÇÚCAR"}
    assert motivos == []


def test_campos_fiscais_invalido_fica_ausente_com_motivo():
    campos, motivos = cf.campos_fiscais(_dto(ncm_bruto="611595", sped_bruto=12, cor="x" * 121))
    assert "ncm" not in campos and "tipoItemSped" not in campos and "cor" not in campos
    assert sorted(motivos) == ["cor_longa", "ncm_invalido", "sped_invalido"]


def test_campos_fiscais_grupo_sem_nome_fica_ausente():
    campos, _ = cf.campos_fiscais(_dto(grupo_codigo=8, grupo_nome=None))
    assert "grupoErp" not in campos


def test_envio_ligado(monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", " MM , nasmar")
    assert cf.envio_ligado("mm") and cf.envio_ligado("NASMAR")
    assert not cf.envio_ligado("outra")
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_FISCAL")
    assert not cf.envio_ligado("mm")


def test_capacidades_tolera_ausente_e_lixo():
    assert capacidades.de({}) == frozenset()
    assert capacidades.de({"capacidades": None}) == frozenset()
    assert capacidades.de({"capacidades": "fiscal.v1"}) == frozenset()
    assert capacidades.de({"capacidades": ["fiscal.v1", 3]}) == frozenset({"fiscal.v1"})

    class _M:
        model_extra = {"capacidades": ["materiais.dryrun.v1"]}
    assert capacidades.de(_M()) == frozenset({"materiais.dryrun.v1"})


def test_resumo():
    r = cf.resumo({"10": ["ncm_vazio"], "11": ["ncm_invalido", "sped_invalido"]}, enviados=5)
    assert r == {"enviados": 5, "com_pendencia": 2,
                 "por_motivo": {"ncm_vazio": 1, "ncm_invalido": 1, "sped_invalido": 1},
                 "pendencias": {"10": ["ncm_vazio"], "11": ["ncm_invalido", "sped_invalido"]},
                 "capacidade_ok": None}


def _req(dtos, fiscal=None):
    return build_catalogo_request(dtos, dry_run=True, full_sync=True, importador_versao="1",
                                  extraido_em="2026-10-16T00:00:00Z", fiscal=fiscal)


def test_sem_fiscal_payload_igual_ao_de_hoje():
    d = _dto(ncm_bruto="61159500", sped_bruto=4)
    corpo = _req([d]).model_dump(by_alias=True)["itens"][0]
    assert set(corpo) == {"fireProdutoId", "codigo", "nome", "unidade", "ean", "ativo", "tipo"}


def test_com_fiscal_so_chaves_validas_e_sem_null():
    d = _dto(ncm_bruto="611595", sped_bruto=4, grupo_codigo=8, grupo_nome="RESÍDUOS")
    campos, _ = cf.campos_fiscais(d)
    corpo = _req([d], fiscal={d.fire_produto_id: campos}).model_dump(by_alias=True)["itens"][0]
    assert corpo["tipoItemSped"] == 4 and corpo["grupoErp"] == {"codigo": "8", "nome": "RESÍDUOS"}
    assert "ncm" not in corpo and "codigoNfe" not in corpo and "cor" not in corpo
    assert "RESÍDUOS" in json.dumps(corpo, ensure_ascii=False)


class _Cfg:
    enabled = True
    catalogo_push = True
    catalogo_apenas_meias = False


class _Client:
    def __init__(self, caps):
        self.caps, self.sent = caps, None

    def send_catalogo(self, request):
        self.sent = request
        extra = {} if self.caps is None else {"capacidades": self.caps}
        return CatalogoReconciliacaoResponse(contagens=CatalogoContagens(), **extra)

    def close(self):
        pass


class _Env:
    def execute(self, *a, **k):
        class C:
            fetchone = staticmethod(lambda: (0,))
            fetchall = staticmethod(lambda: [])
        return C()

    def executemany(self, *a):
        pass


@pytest.fixture
def caplog_loguru():
    from app.utils.logger import logger
    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    logger.remove(hid)


def _rodar(monkeypatch, caps, ligado=True):
    dtos = [_dto(ncm_bruto="61159500", sped_bruto=4), _dto(fire_produto_id="9", codigo="9", ncm_bruto="")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)
    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    if ligado:
        monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", "mm")
    else:
        monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_FISCAL", raising=False)
    client = _Client(caps)
    rep = catalogo_sync.run_catalogo_sync("mm", now_iso="2026-10-16T00:00:00Z",
                                          _client=client, _fire_conn=object(), _env_conn=_Env())
    return rep, client


def test_sync_fiscal_ligado_com_capacidade(monkeypatch):
    rep, client = _rodar(monkeypatch, ["fiscal.v1"])
    assert client.sent.itens[0].ncm == "61159500"
    fp = rep.model_extra["fiscal_portal"]
    assert fp["capacidade_ok"] is True and fp["pendencias"] == {"9": ["ncm_vazio"]}


def test_sync_fiscal_ligado_sem_capacidade_marca_e_loga(monkeypatch, caplog_loguru):
    rep, _ = _rodar(monkeypatch, None)
    assert rep.model_extra["fiscal_portal"]["capacidade_ok"] is False
    assert any("ignorou os campos fiscais" in m for m in caplog_loguru)


def test_sync_fiscal_desligado_nao_manda_nem_relata(monkeypatch):
    rep, client = _rodar(monkeypatch, ["fiscal.v1"], ligado=False)
    assert client.sent.itens[0].ncm is None
    assert "fiscal_portal" not in (rep.model_extra or {})
