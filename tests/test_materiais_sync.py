"""Carga de material Fire -> Flow (dryRun). Fire e Flow são falsos; o SQLite do ambiente é real."""

from datetime import date, datetime

import pytest

from app.erp.catalog_extract import MaterialFireDTO
from app.integrations.flowpcp import materiais_sync as ms
from app.integrations.flowpcp.client import FlowPCPClientError
from app.integrations.flowpcp.materiais_schema import MateriaisRequest, MateriaisResposta
from app.persistence import materiais_fire_repo, router


class _Cfg:
    enabled = True


class _Client:
    def __init__(self, caps=("materiais.dryrun.v1", "materiais.v1"), erro=None):
        self.caps, self.erro, self.chamadas = list(caps), erro, []

    def send_materiais(self, request):
        self.chamadas.append(request)
        if self.erro:
            raise self.erro
        return MateriaisResposta(resumo={"criar": 1}, capacidades=self.caps)

    def close(self):
        pass


@pytest.fixture
def env_conn(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    with router.env_connect("mm") as conn:
        yield conn


@pytest.fixture
def caplog_loguru():
    from app.utils.logger import logger

    msgs: list[str] = []
    hid = logger.add(lambda m: msgs.append(f"{m.record['level'].name}|{m}"), level="WARNING")
    yield msgs
    logger.remove(hid)


def _dto(seq="250", **kw):
    base = dict(
        erp_produto_id=seq,
        codigo=seq,
        nome="FIO 70/2",
        unidade="KG",
        ean=None,
        ativo=True,
        ncm_bruto="5402.33.10",
        sped_bruto=1,
        grupo_codigo=1,
        grupo_nome="FIO",
    )
    base.update(kw)
    return MaterialFireDTO(**base)


def _rodar(monkeypatch, env_conn, dtos, *, entradas=None, ligado=True, client=None, **kw):
    monkeypatch.setattr(ms, "extract_materiais", lambda conn: dtos)
    monkeypatch.setattr(ms, "extract_ultima_entrada", lambda conn: entradas or {})
    monkeypatch.setattr(ms, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.delenv("PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_MM", raising=False)
    if ligado:
        monkeypatch.setenv("PORTAL_FLOWPCP_MATERIAIS", "mm")
    else:
        monkeypatch.delenv("PORTAL_FLOWPCP_MATERIAIS", raising=False)
    client = client or _Client()
    rep = ms.run_materiais_sync(
        "mm",
        now_iso="2026-10-16T00:00:00Z",
        _client=client,
        _fire_conn=object(),
        _env_conn=env_conn,
        **kw,
    )
    return rep, client


def test_exige_aprovacao():
    g = frozenset({1, 4})
    assert ms.exige_aprovacao(1, None, g) is True
    assert ms.exige_aprovacao(12, 1, g) is True
    assert ms.exige_aprovacao(12, 7, g) is False
    assert ms.exige_aprovacao(20, 10, g) is True
    assert ms.exige_aprovacao(None, None, g) is False


def test_grupos_producao_padrao_env_e_invalido(monkeypatch, caplog_loguru):
    monkeypatch.delenv("PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_MM", raising=False)
    padrao = frozenset({1, 4, 5, 6, 11, 13, 19})
    assert ms.grupos_producao("mm") == padrao
    monkeypatch.setenv("PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_MM", "4, 19")
    assert ms.grupos_producao("mm") == {4, 19}
    monkeypatch.setenv("PORTAL_FLOWPCP_MATERIAIS_GRUPOS_PRODUCAO_MM", "4,x")
    assert ms.grupos_producao("mm") == padrao
    assert any(m.startswith("ERROR|") and "inválido" in m for m in caplog_loguru)


def test_desligado_grava_local_e_nao_chama_client(monkeypatch, env_conn):
    rep, client = _rodar(monkeypatch, env_conn, [_dto("250"), _dto("251")], ligado=False)
    assert isinstance(rep, ms.MateriaisLocalResult) and rep.itens == 2
    assert client.chamadas == []
    assert materiais_fire_repo.seqs(env_conn) == {"250", "251"}
    row = env_conn.execute(
        "SELECT ncm, tipo_item_sped, exige_aprovacao FROM materiais_fire WHERE erp_produto_id='250'"
    ).fetchone()
    assert tuple(row) == ("54023310", 1, 1)


def test_apply_com_envio_desligado_grava_local_e_levanta(monkeypatch, env_conn):
    with pytest.raises(ms.MateriaisDesligadoError):
        _rodar(monkeypatch, env_conn, [_dto("250")], ligado=False, dry_run=False)
    assert materiais_fire_repo.seqs(env_conn) == {"250"}


def test_ligado_dryrun_monta_itens(monkeypatch, env_conn):
    dtos = [
        _dto("250"),
        _dto("251", grupo_codigo=12, grupo_nome=None),
        _dto("252", grupo_codigo=None, grupo_nome=None),
    ]
    rep, client = _rodar(
        monkeypatch, env_conn, dtos, entradas={"250": ("5402.33.90", date(2026, 7, 1))}
    )
    assert len(client.chamadas) == 1
    req = client.chamadas[0]
    assert isinstance(req, MateriaisRequest) and req.dryRun is True
    i250 = req.itens[0]
    assert i250.ncm == "54023310" and i250.ncmUltimaEntrada == "54023390"
    assert i250.ultimaEntradaEm == "2026-07-01"
    i251 = req.itens[1]
    assert i251.grupoErp.model_dump() == {"codigo": "12", "nome": "GRUPO 12"}
    assert [i.erpProdutoId for i in req.itens] == ["250", "251"]
    mp = rep.model_extra["materiais_portal"]
    assert mp["enviados"] == 2 and mp["capacidade_ok"] is True
    assert mp["fora"] == {"252": "sem_grupo"}
    assert mp["pendencias"] == {"251": ["grupo_sem_nome"]}
    assert "ultimaEntradaEm" in req.model_dump(by_alias=True)["itens"][0]
    assert "ultimaEntradaEm" not in req.model_dump(by_alias=True)["itens"][1]


def test_ultima_entrada_so_com_data_real(monkeypatch, env_conn):
    entradas = {
        "250": ("54023310", "2026-07-01"),  # texto: não é date
        "251": ("54023310", datetime(2026, 7, 1, 13, 30)),
    }
    _, client = _rodar(monkeypatch, env_conn, [_dto("250"), _dto("251")], entradas=entradas)
    a, b = client.chamadas[0].itens
    assert a.ncmUltimaEntrada == "54023310" and a.ultimaEntradaEm is None
    assert b.ultimaEntradaEm == "2026-07-01"


def test_duplicado_fica_fora_mantendo_o_primeiro(monkeypatch, env_conn):
    dtos = [_dto("250", codigo="AB1"), _dto("251", codigo="ab1"), _dto("252", codigo="AB2")]
    rep, client = _rodar(monkeypatch, env_conn, dtos)
    assert [i.erpProdutoId for i in client.chamadas[0].itens] == ["250", "252"]
    assert rep.model_extra["materiais_portal"]["fora"] == {"251": "duplicado"}


def test_erp_produto_id_repetido_fica_fora(monkeypatch, env_conn):
    # SEQ é PK da cópia local: o repo recebe o lote já sem a repetição exata.
    monkeypatch.setattr(ms.materiais_fire_repo, "replace_all", lambda *a, **k: 0)
    dtos = [_dto("250", codigo="A1"), _dto("250", codigo="A2"), _dto("251", codigo="A3")]
    rep, client = _rodar(monkeypatch, env_conn, dtos)
    assert [i.erpProdutoId for i in client.chamadas[0].itens] == ["250", "251"]
    assert rep.model_extra["materiais_portal"]["fora"] == {"250": "duplicado"}


def test_campo_excede_fica_fora_sem_falhar_o_lote(monkeypatch, env_conn):
    dtos = [_dto("250"), _dto("251", nome="X" * 256), _dto("252", ean="9" * 33)]
    rep, client = _rodar(monkeypatch, env_conn, dtos)
    assert [i.erpProdutoId for i in client.chamadas[0].itens] == ["250"]
    assert rep.model_extra["materiais_portal"]["fora"] == {
        "251": "campo_excede",
        "252": "campo_excede",
    }


def test_apply_sem_capacidade_nao_envia_o_apply(monkeypatch, env_conn):
    client = _Client(caps=["materiais.dryrun.v1"])
    with pytest.raises(ms.ApplySemCapacidadeError):
        _rodar(monkeypatch, env_conn, [_dto()], client=client, dry_run=False)
    assert len(client.chamadas) == 1 and client.chamadas[0].dryRun is True


def test_apply_com_capacidade_sonda_e_depois_grava(monkeypatch, env_conn):
    rep, client = _rodar(monkeypatch, env_conn, [_dto()], dry_run=False)
    assert [c.dryRun for c in client.chamadas] == [True, False]
    assert rep.model_extra["materiais_portal"]["capacidade_ok"] is True


def test_flow_antigo_sem_dryrun_marca_e_loga(monkeypatch, env_conn, caplog_loguru):
    rep, _ = _rodar(monkeypatch, env_conn, [_dto()], client=_Client(caps=[]))
    assert rep.model_extra["materiais_portal"]["capacidade_ok"] is False
    assert any(
        m.startswith("ERROR|") and "não declarou 'materiais.dryrun.v1'" in m for m in caplog_loguru
    )


def test_403_vira_erro_claro_da_flag_do_tenant(monkeypatch, env_conn):
    client = _Client(erro=FlowPCPClientError("materiais status 403", status_code=403))
    with pytest.raises(FlowPCPClientError, match="materiaisErpAtivo"):
        _rodar(monkeypatch, env_conn, [_dto()], client=client)


def test_abre_o_fire_somente_leitura(monkeypatch, env_conn):
    from contextlib import contextmanager

    from app.persistence import environments_repo

    abertos = []

    @contextmanager
    def leitura(self, cfg):
        abertos.append(cfg)
        yield object()

    monkeypatch.setattr(environments_repo, "get_by_slug", lambda s: {"slug": s})
    monkeypatch.setattr(environments_repo, "to_fb_config", lambda e: {"fb": 1})
    monkeypatch.setattr(ms.FirebirdConnection, "connect_somente_leitura", leitura)
    monkeypatch.setattr(
        ms.FirebirdConnection,
        "connect_with_config",
        lambda *a, **k: pytest.fail("material só lê: conexão de escrita proibida"),
    )
    monkeypatch.setattr(ms, "extract_materiais", lambda conn: [_dto()])
    monkeypatch.setattr(ms, "extract_ultima_entrada", lambda conn: {})
    monkeypatch.setattr(ms, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.delenv("PORTAL_FLOWPCP_MATERIAIS", raising=False)
    ms.run_materiais_sync("mm", now_iso="2026-10-16T00:00:00Z", _env_conn=env_conn)
    assert abertos == [{"fb": 1}]
