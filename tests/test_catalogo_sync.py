import json
from dataclasses import replace

import pytest

from app.erp.catalog_extract import ProdutoFireDTO
from app.integrations.flowpcp import catalogo_sync
from app.integrations.flowpcp.catalogo_schema import (
    CatalogoContagens,
    CatalogoReconciliacaoResponse,
)
from app.integrations.flowpcp.client import FlowPCPClientError


@pytest.fixture(autouse=True)
def _sem_interruptores_de_ambiente(monkeypatch):
    """Testes antigos não dependem do ambiente de quem roda."""
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", raising=False)
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", raising=False)


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


def test_fora_do_envio_tira_do_flow_mas_mantem_na_copia_local(monkeypatch):
    """Exclusão pontual do envio (15/10/2026: 10 pares do listrado Sam's têm 2
    cadastros no Fire e iam virar par duplicado no Flow). A cópia local segue
    sendo o Fire inteiro: só o payload ao Flow perde os itens."""
    dtos = [
        ProdutoFireDTO("3174", "3174", "BRANCO COM PRETO", "PAR", None, True, "simples"),
        ProdutoFireDTO("3381", "3381", "KIT C/5", "KIT", None, True, "kit"),
        ProdutoFireDTO("2149", "2149", "KIT 3 CANO ALTO KINGS", "KIT", None, True, "kit"),
    ]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    fake_client = _FakeClient()
    env_conn = _FakeEnvConn()
    catalogo_sync.run_catalogo_sync(
        "mm",
        dry_run=True,
        full_sync=True,
        now_iso="t",
        fora_do_envio=frozenset({"3174", "3381"}),
        _client=fake_client,
        _fire_conn=object(),
        _env_conn=env_conn,
    )
    assert [i.fireProdutoId for i in fake_client.sent.itens] == ["2149"]
    assert len(env_conn.rows) == 3  # cópia local completa


def test_sem_fora_do_envio_manda_tudo(monkeypatch):
    dtos = [ProdutoFireDTO("1", "1", "X", "PC", None, True, "simples")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    fake_client = _FakeClient()
    catalogo_sync.run_catalogo_sync(
        "mm", now_iso="t", _client=fake_client, _fire_conn=object(), _env_conn=_FakeEnvConn()
    )
    assert len(fake_client.sent.itens) == 1


def test_fora_do_envio_com_seq_inexistente_nao_envia_nada(monkeypatch):
    """Falha fechada: a exclusão que a resposta confirma tem que ter acontecido."""
    import pytest

    dtos = [ProdutoFireDTO("3174", "3174", "X", "PAR", None, True, "simples")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    fake_client = _FakeClient()
    with pytest.raises(catalogo_sync.ForaDoEnvioInexistenteError) as e:
        catalogo_sync.run_catalogo_sync(
            "mm",
            dry_run=False,
            now_iso="t",
            fora_do_envio=frozenset({"3174", "9999"}),
            _client=fake_client,
            _fire_conn=object(),
            _env_conn=_FakeEnvConn(),
        )
    assert e.value.seqs == ["9999"]
    assert fake_client.sent is None


def test_exclusao_guardada_com_seq_inexistente_nao_envia_nada(monkeypatch):
    import pytest

    dtos = [ProdutoFireDTO("3174", "3174", "X", "PAR", None, True, "simples")]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: dtos)
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "3174,9999")

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    fake_client = _FakeClient()
    with pytest.raises(catalogo_sync.ForaDoEnvioInexistenteError) as e:
        catalogo_sync.run_catalogo_sync(
            "mm",
            dry_run=False,
            now_iso="t",
            _client=fake_client,
            _fire_conn=object(),
            _env_conn=_FakeEnvConn(),
        )
    assert e.value.guardados == ["9999"]
    assert e.value.variavel == "PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM"
    assert fake_client.sent is None


class _FireComKit:
    """Conexão fake: PRODUTOS_KIT devolve linhas ou falha."""

    def __init__(self, linhas=None, falha=False):
        self.linhas, self.falha = linhas or [], falha
        self.sqls: list[str] = []

    def cursor(self):
        dono = self

        class _C:
            def execute(self, sql):
                dono.sqls.append(sql)
                if dono.falha:
                    raise RuntimeError("Fire caiu")

            def fetchall(self):
                return dono.linhas

            def close(self):
                pass

        return _C()


_DTOS_KIT = [
    ProdutoFireDTO("2544", "2544", "KIT", "KIT", None, True, "kit", tem_composicao=True),
    ProdutoFireDTO(
        "3306", "3306", "PAR", "PARES", None, True, "simples", codigo_alternativo="8366"
    ),
    ProdutoFireDTO("3174", "3174", "PAR DUP", "PARES", None, True, "simples"),
]


@pytest.fixture
def _sync(monkeypatch):
    monkeypatch.delenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", raising=False)

    class _Cfg:
        enabled = True
        catalogo_push = True

    monkeypatch.setattr(catalogo_sync, "flowpcp_config_for_slug", lambda slug: _Cfg())
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: list(_DTOS_KIT))

    def rodar(*, ligado_para="mm", fire=None, cliente=None, **kw):
        monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_COMPOSICAO", ligado_para)
        cliente = cliente or _FakeClient()
        rep = catalogo_sync.run_catalogo_sync(
            "mm",
            now_iso="t",
            _client=cliente,
            _env_conn=_FakeEnvConn(),
            _fire_conn=fire or _FireComKit([(2544, 3306, 6)]),
            **kw,
        )
        return rep, cliente.sent.model_dump(by_alias=True)

    return rodar


_LEGADO = [
    {"fireProdutoId": "2544", "codigo": "2544", "nome": "KIT", "unidade": "KIT",
     "ean": None, "ativo": True, "tipo": "kit"},
    # 3306 tem CODPROD_ALTERN: o codigoMes não pode vazar com o interruptor desligado
    {"fireProdutoId": "3306", "codigo": "3306", "nome": "PAR", "unidade": "PARES",
     "ean": None, "ativo": True, "tipo": "simples"},
    {"fireProdutoId": "3174", "codigo": "3174", "nome": "PAR DUP", "unidade": "PARES",
     "ean": None, "ativo": True, "tipo": "simples"},
]


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("ligado_para", ["", "nasmar"])
def test_sync_desligado_payload_identico_ao_de_hoje(_sync, ligado_para, dry_run):
    fire = _FireComKit([(2544, 3306, 6)])
    rep, corpo = _sync(ligado_para=ligado_para, fire=fire, dry_run=dry_run)
    assert json.dumps(corpo["itens"], sort_keys=True) == json.dumps(_LEGADO, sort_keys=True)
    assert set(corpo) == {"schema", "dryRun", "fullSync", "itens", "origem"}
    assert corpo["dryRun"] is dry_run
    assert not any("PRODUTOS_KIT" in q for q in fire.sqls)
    assert "composicao_portal" not in (rep.model_extra or {})


def test_sync_ligado_manda_composicao_e_resumo(_sync):
    rep, corpo = _sync()
    kit, par, _ = corpo["itens"]
    assert kit["componentes"] == [{"erpProdutoId": "3306", "quantidade": 6.0}]
    assert par["componentes"] == [] and par["codigoMes"] == "8366"
    assert rep.model_extra["composicao_portal"]["com_lista"] == 1


@pytest.mark.parametrize("fire", [_FireComKit(falha=True), _FireComKit([])])
def test_sync_leitura_com_falha_nenhum_item_com_a_chave(_sync, fire):
    rep, corpo = _sync(fire=fire)
    assert all("componentes" not in i for i in corpo["itens"])
    assert corpo["itens"][1]["codigoMes"] == "8366"
    assert rep.model_extra["composicao_portal"]["leitura_ok"] is False


def test_sync_kit_com_quantidade_divergente_do_nome_vai_sem_a_chave(_sync, monkeypatch):
    dtos = [replace(_DTOS_KIT[0], nome="KIT C/5"), *_DTOS_KIT[1:]]
    monkeypatch.setattr(catalogo_sync, "extract_produtos", lambda conn, **kw: list(dtos))
    rep, corpo = _sync(fire=_FireComKit([(2544, 3306, 3)]))
    kit, par, _ = corpo["itens"]
    assert "componentes" not in kit  # congelado: o Flow não mexe
    assert par["componentes"] == []
    cp = rep.model_extra["composicao_portal"]
    assert cp["com_lista"] == 0
    assert cp["divergencia_nome"] == {"2544": "nome diz 5, composição soma 3"}


def test_sync_exclusao_guardada_vale_mesmo_desligado(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "3174")
    rep, corpo = _sync(ligado_para="")
    assert [i["fireProdutoId"] for i in corpo["itens"]] == ["2544", "3306"]
    assert rep.model_extra["fora_do_envio_guardado"] == ["3174"]


def test_sync_exclusao_guardada_tira_a_chave_do_kit_que_usa_o_par(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "3306")
    rep, corpo = _sync()
    assert "componentes" not in corpo["itens"][0]
    assert rep.model_extra["composicao_portal"]["omitidos"] == {"2544": "filho fora do envio: 3306"}


def test_sync_exclusao_guardada_invalida_nao_envia_nada(_sync, monkeypatch):
    monkeypatch.setenv("PORTAL_FLOWPCP_CATALOGO_EXCLUIR_MM", "abc")
    cliente = _FakeClient()
    with pytest.raises(ValueError):
        _sync(cliente=cliente)
    assert cliente.sent is None


def test_sync_confirmacao_so_com_apply(_sync):
    with pytest.raises(ValueError, match="dryRun"):
        _sync(dry_run=True, confirmar_remocao=True)
    _, corpo = _sync(dry_run=False, confirmar_substituicao=True)
    assert corpo["confirmarSubstituicaoComposicao"] is True


def test_sync_400_com_composicao_ligada_diz_para_desligar(_sync):
    class _Recusa(_FakeClient):
        def send_catalogo(self, request):
            self.sent = request
            raise FlowPCPClientError("catalogo status 400", status_code=400, body="zod")

    with pytest.raises(FlowPCPClientError, match="PORTAL_FLOWPCP_CATALOGO_COMPOSICAO"):
        _sync(cliente=_Recusa())
