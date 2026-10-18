"""Sync da tabela, envio de revisões e decisões com o Flow (contrato precos v1, Task 7.4).

Flow fake no lugar do cliente HTTP (`precos._cliente`), respostas do
`exemplos.json`; o cliente HTTP em si é testado com `httpx.MockTransport`.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.http.client import OutboundClient
from app.integrations.flowpcp import envio_portal, precos
from app.integrations.flowpcp import precos_schema as ps
from app.integrations.flowpcp.client import FlowPCPClient, FlowPCPClientError
from app.persistence import environments_repo, precos_repo, router
from app.persistence import preco_revisao_repo as revs

EXEMPLOS = json.loads(
    (Path(__file__).parent / "fixtures" / "flow_precos_v1" / "exemplos.json").read_text("utf-8")
)
MM = "11111111000191"
NASMAR = "22222222000182"
IMP = "portal-mm-000917"
AGORA = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)
TENANT = "5b0c1f0e-0000-4000-8000-000000000001"


def _ex(nome):
    return copy.deepcopy(EXEMPLOS[nome])


def _pagina(**over):
    p = _ex("tabelaPagina")
    p.update(over)
    return ps.TabelaPaginaV1.model_validate(p)


def _resposta(revisao=1, *, ja=False, aprovacao_id=None, situacao="pendente", external=IMP):
    r = _ex("aprovacaoResposta")
    r.update(revisao=revisao, jaRecebido=ja, externalId=external, situacao=situacao)
    if aprovacao_id is not None:
        r["aprovacaoId"] = aprovacao_id
        r["itens"] = []
    return ps.AprovacaoRespostaV1.model_validate(r)


def _erro(status, error=None, details=None, body=None):
    return FlowPCPClientError(
        f"status {status}", status_code=status, error=error, details=details or {}, body=body
    )


class FakeFlow:
    """Respostas em fila por rota: item = modelo, exceção, ou callable(args) → um deles.
    Fila vazia: tabela → página final vazia; aprovação → 201; decisões → vazia."""

    def __init__(self):
        self.tabela: list = []
        self.aprovacao: list = []
        self.decisoes: list = []
        self.confirmar: list = []
        self.chamadas: list[tuple[str, dict]] = []

    def _proximo(self, fila, args, padrao):
        r = fila.pop(0) if fila else padrao(args)
        if callable(r) and not isinstance(r, Exception):
            r = r(args)
        if isinstance(r, Exception):
            raise r
        return r

    def get_precos_tabela(self, empresa, cursor=None, limite=500):
        args = {"empresa": empresa, "cursor": cursor, "limite": limite}
        self.chamadas.append(("tabela", args))
        return self._proximo(
            self.tabela,
            args,
            lambda a: _pagina(mudancas=[], cursor="fim", empresaFaturadoraCnpj=a["empresa"]),
        )

    def post_precos_aprovacao(self, corpo):
        self.chamadas.append(("aprovacao", copy.deepcopy(corpo)))
        return self._proximo(
            self.aprovacao,
            corpo,
            lambda c: _resposta(c["revisao"], external=c["externalId"]),
        )

    def get_precos_decisoes(self, empresa, cursor=None, limite=100):
        args = {"empresa": empresa, "cursor": cursor, "limite": limite}
        self.chamadas.append(("decisoes", args))
        return self._proximo(
            self.decisoes,
            args,
            lambda a: ps.DecisoesPaginaV1.model_validate(
                {
                    "schema": "precos.decisoes.v1",
                    "empresaFaturadoraCnpj": a["empresa"],
                    "decisoes": [],
                    "cursor": None,
                    "temMais": False,
                }
            ),
        )

    def post_precos_confirmar(self, corpo):
        self.chamadas.append(("confirmar", copy.deepcopy(corpo)))
        return self._proximo(
            self.confirmar,
            corpo,
            lambda c: ps.ConfirmacaoRespostaV1.model_validate(
                {
                    "resultados": [
                        {"decisaoId": x["decisaoId"], "situacao": "confirmada"}
                        for x in c["confirmacoes"]
                    ]
                }
            ),
        )

    def rotas(self):
        return [r for r, _ in self.chamadas]

    def de(self, rota):
        return [a for r, a in self.chamadas if r == rota]


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("PORTAL_FLOWPCP_PRECOS", raising=False)
    router.reset_init_cache()
    with router.shared_connect():
        pass
    precos._reset_estado()
    monkeypatch.setattr(environments_repo, "get_flowpcp_token", lambda env_id: "tok")
    yield tmp_path
    precos._reset_estado()


def _env(slug, cnpj, *, modo="ligado", flow=True, fonte=None):
    e = environments_repo.create(
        slug=slug,
        name=slug.upper(),
        watch_dir="/tmp/in",
        output_dir="/tmp/out",
        fb_path="/tmp/x.fdb",
        cnpj=cnpj,
    )
    if flow:
        environments_repo.set_flowpcp_config(
            e["id"], enabled=True, base_url=f"https://flow-{slug}.test", tenant_id=TENANT
        )
    if modo != "desligado":
        environments_repo.set_preco_trava_modo(e["id"], modo, por="teste")
    if fonte is not None:
        with router.shared_connect() as conn:
            conn.execute(
                "UPDATE environments SET preco_fonte_slug = ? WHERE id = ?", (fonte, e["id"])
            )
    return e


@pytest.fixture
def flow(monkeypatch):
    fake = FakeFlow()
    configs = []

    def _cliente(cfg):
        configs.append(cfg)
        return fake

    monkeypatch.setattr(precos, "_cliente", _cliente)
    fake.configs = configs
    return fake


@pytest.fixture
def mm(shared):
    return _env("mm", MM)


def _corpo_pedido(external=IMP, empresa=MM):
    c = _ex("aprovacaoPedido")
    c.pop("revisao")
    c["externalId"] = external
    c["empresaFaturadoraCnpj"] = empresa
    for it in c["itens"]:
        it["itemChave"] = f"{external}:{it['linha']}"
    return c


def _revisao(slug="mm", external=IMP, h="sha256:aaaaaaaaaaaaaaaa", empresa=MM):
    with router.env_connect(slug) as conn:
        return revs.garantir_revisao(
            conn, external, _corpo_pedido(external, empresa), h, agora=AGORA
        )


def _situacoes(slug="mm", external=IMP):
    with router.env_connect(slug) as conn:
        return [
            (r["revisao"], r["situacao"])
            for r in conn.execute(
                "SELECT revisao, situacao FROM preco_revisao WHERE import_id = ? ORDER BY revisao",
                (external,),
            )
        ]


def _linha(revisao, slug="mm", external=IMP):
    with router.env_connect(slug) as conn:
        return dict(
            conn.execute(
                "SELECT * FROM preco_revisao WHERE import_id = ? AND revisao = ?",
                (external, revisao),
            ).fetchone()
        )


def _estado(slug="mm"):
    with router.env_connect(slug) as conn:
        return precos_repo.estado_sync(conn)


def _redes(slug="mm"):
    with router.env_connect(slug) as conn:
        return sorted(r[0] for r in conn.execute("SELECT rede_id FROM preco_rede_v1"))


# ─── (a) tabela ─────────────────────────────────────────────────────────────


def test_sync_de_duas_paginas_avanca_o_cursor_e_so_completa_na_segunda(mm, flow):
    p1 = _pagina(temMais=True, cursor="c1", geradoEm="2026-10-20T11:00:00.000Z")
    flow.tabela = [p1, _erro(503, "erro_db")]
    precos.rodar(AGORA)
    e = _estado()
    assert e.cursor == "c1" and e.completo_em is None
    assert [a["cursor"] for a in flow.de("tabela")] == [None, "c1"]
    assert all(a["empresa"] == MM for a in flow.de("tabela"))

    p2 = _pagina(mudancas=[], cursor="c2", temMais=False, geradoEm="2026-10-20T13:59:00.000Z")
    flow.tabela = [p2]
    precos.rodar(AGORA + timedelta(seconds=61))
    e = _estado()
    assert e.cursor == "c2"
    assert e.completo_em == datetime(2026, 10, 20, 13, 59, tzinfo=UTC)
    assert flow.de("tabela")[-1]["cursor"] == "c1"
    assert len(_redes()) == 2


def test_tabela_respeita_o_intervalo_de_60s(mm, flow):
    precos.rodar(AGORA)
    precos.rodar(AGORA + timedelta(seconds=30))
    assert flow.rotas().count("tabela") == 1
    precos.rodar(AGORA + timedelta(seconds=61))
    assert flow.rotas().count("tabela") == 2


def test_reiniciar_zera_a_copia_e_recomeca_sem_cursor(mm, flow):
    flow.tabela = [_pagina(cursor="c1")]
    precos.rodar(AGORA)
    assert len(_redes()) == 2

    nova_rede = {
        "tipo": "rede",
        "redeId": "a1a1a1a1-0000-4000-8000-0000000000ff",
        "nome": "Nova",
        "ativa": True,
        "travada": False,
        "raizes": ["99887766"],
        "cnpjs": [],
    }
    flow.tabela = [
        _pagina(reiniciar=True, mudancas=[], cursor="lixo"),
        _pagina(mudancas=[nova_rede], cursor="c9"),
    ]
    precos.rodar(AGORA + timedelta(seconds=61))
    assert [a["cursor"] for a in flow.de("tabela")][-2:] == ["c1", None]
    assert _redes() == ["a1a1a1a1-0000-4000-8000-0000000000ff"]
    assert _estado().cursor == "c9"


def test_copia_de_outra_empresa_reinicia_antes_de_pedir(mm, flow):
    with router.env_connect("mm") as conn:
        precos_repo.aplicar_pagina(
            conn,
            {**_ex("tabelaPagina"), "empresaFaturadoraCnpj": None, "cursor": "velho"},
            empresa_cnpj="99999999000191",
        )
    flow.tabela = [_pagina(mudancas=[], cursor="c1")]
    precos.rodar(AGORA)
    assert flow.de("tabela")[0]["cursor"] is None
    assert _redes() == []
    assert _estado().empresa_cnpj == MM


def test_403_tabela_desligada_nao_apaga_a_copia_e_pausa(mm, flow):
    flow.tabela = [_pagina(cursor="c1")]
    precos.rodar(AGORA)
    flow.tabela = [_erro(403, "tabela_preco_desligada")]
    precos.rodar(AGORA + timedelta(seconds=61))
    assert len(_redes()) == 2
    assert _estado().cursor == "c1"
    n = len(flow.chamadas)
    precos.rodar(AGORA + timedelta(minutes=5))
    assert len(flow.chamadas) == n  # pausa longa: nem tabela nem b/c
    precos.rodar(AGORA + timedelta(minutes=17))
    assert len(flow.chamadas) > n


def test_cursor_invalido_reinicia_a_copia_e_recomeca_sem_cursor(mm, flow):
    flow.tabela = [_pagina(cursor="c1")]
    precos.rodar(AGORA)
    nova_rede = {
        "tipo": "rede",
        "redeId": "a1a1a1a1-0000-4000-8000-0000000000ee",
        "nome": "Outra",
        "ativa": True,
        "travada": False,
        "raizes": ["55443322"],
        "cnpjs": [],
    }
    flow.tabela = [_erro(400, "cursor_invalido"), _pagina(mudancas=[nova_rede], cursor="c2")]
    precos.rodar(AGORA + timedelta(seconds=61))
    assert [a["cursor"] for a in flow.de("tabela")][-2:] == ["c1", None]
    assert _redes() == ["a1a1a1a1-0000-4000-8000-0000000000ee"]
    assert _estado().cursor == "c2"


def test_cursor_invalido_em_laco_para_em_max_reinicios_e_pausa(mm, flow):
    def responde(a):
        if a["cursor"] is not None:
            return _erro(400, "cursor_invalido")
        return _pagina(mudancas=[], temMais=True, cursor="cx")

    flow.tabela = [responde] * 50
    precos.rodar(AGORA)
    chamadas = len(flow.de("tabela"))
    assert chamadas == 2 * (precos.MAX_REINICIOS + 1)
    precos.rodar(AGORA + timedelta(seconds=61))
    assert len(flow.de("tabela")) == chamadas  # pausado


def test_pagina_de_outra_empresa_nao_e_aplicada(mm, flow):
    flow.tabela = [_pagina(empresaFaturadoraCnpj=NASMAR)]
    precos.rodar(AGORA)
    assert _redes() == []
    assert _estado().cursor is None


def test_ambiente_sem_cnpj_nao_sincroniza(shared, flow):
    _env("mm", None)
    _revisao()
    precos.rodar(AGORA)
    assert flow.chamadas == []


def test_ambiente_desligado_nao_fala_com_o_flow(shared, flow):
    _env("mm", MM, modo="desligado")
    precos.rodar(AGORA)
    assert flow.chamadas == []


def test_observando_sincroniza_mas_nunca_chama_as_rotas_b_e_c(shared, flow):
    _env("mm", MM, modo="observando")
    _revisao()
    precos.rodar(AGORA)
    assert flow.rotas() == ["tabela"]
    assert _situacoes() == [(1, "pendente_envio")]


def test_sem_capacidade_de_aprovacao_nao_chama_b_e_c(mm, flow):
    _revisao()
    flow.tabela = [_pagina(capacidades=["precos.tabela.v1"])]
    precos.rodar(AGORA)
    assert flow.rotas() == ["tabela"]
    assert _situacoes() == [(1, "pendente_envio")]


def test_nasmar_usa_a_config_da_mm_e_manda_o_cnpj_da_nasmar(shared, flow):
    _env("mm", MM, modo="desligado")
    _env("nasmar", NASMAR, flow=False, fonte="mm")
    _revisao("nasmar", "portal-nas-1", empresa=NASMAR)
    flow.tabela = [_pagina(empresaFaturadoraCnpj=NASMAR, mudancas=[], cursor="n1")]
    precos.rodar(AGORA)
    assert [c.base_url for c in flow.configs] == ["https://flow-mm.test"]
    assert flow.de("tabela")[0]["empresa"] == NASMAR
    assert flow.de("aprovacao")[0]["empresaFaturadoraCnpj"] == NASMAR
    assert flow.de("decisoes")[0]["empresa"] == NASMAR
    assert _estado("nasmar").cursor == "n1"
    assert _estado("mm").cursor is None


# ─── (b) envio das revisões ─────────────────────────────────────────────────


def test_201_marca_enviada(mm, flow):
    _revisao()
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "enviada")]
    enviado = flow.de("aprovacao")[0]
    assert enviado["revisao"] == 1 and enviado["externalId"] == IMP
    ps.validar_envio(enviado)


def test_200_ja_recebido_marca_enviada(mm, flow):
    _revisao()
    flow.aprovacao = [_resposta(1, ja=True)]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "enviada")]


def test_envio_respeita_o_intervalo_de_30s(mm, flow):
    precos.rodar(AGORA)
    _revisao()
    precos.rodar(AGORA + timedelta(seconds=10))
    assert flow.de("aprovacao") == []
    precos.rodar(AGORA + timedelta(seconds=31))
    assert len(flow.de("aprovacao")) == 1


def test_marcar_tentada_false_nao_envia(mm, flow, monkeypatch):
    _revisao()
    monkeypatch.setattr(precos.revs, "marcar_tentada", lambda *a, **k: False)
    precos.rodar(AGORA)
    assert flow.de("aprovacao") == []
    assert _situacoes() == [(1, "pendente_envio")]


def test_409_revisao_obsoleta_sobe_o_piso_e_reenvia_acima_da_vigente_do_flow(mm, flow):
    _revisao()
    flow.aprovacao = [_erro(409, "revisao_obsoleta", {"revisaoVigente": 3})]
    precos.rodar(AGORA)
    assert [c["revisao"] for c in flow.de("aprovacao")] == [1, 4]
    assert _situacoes() == [(1, "substituida"), (4, "enviada")]


def test_409_revisao_obsoleta_de_revisao_ja_substituida_nao_reenvia(mm, flow):
    _revisao()

    def nasce_outra(corpo):
        _revisao(h="sha256:bbbbbbbbbbbbbbbb")  # o pedido mudou enquanto o POST voava
        return _erro(409, "revisao_obsoleta", {"revisaoVigente": 5})

    flow.aprovacao = [nasce_outra]
    precos.rodar(AGORA)
    assert [c["revisao"] for c in flow.de("aprovacao")] == [1]
    assert _situacoes() == [(1, "substituida"), (2, "pendente_envio")]


def test_409_conteudo_divergente_incrementa_e_reenvia(mm, flow):
    _revisao()
    flow.aprovacao = [_erro(409, "conteudo_divergente", {"conteudoHashRecebido": "x"})]
    precos.rodar(AGORA)
    assert [c["revisao"] for c in flow.de("aprovacao")] == [1, 2]
    assert _situacoes() == [(1, "substituida"), (2, "enviada")]


def test_422_produto_desconhecido_recusa_a_revisao_e_nao_reenvia(mm, flow):
    _revisao()
    flow.aprovacao = [_erro(422, "produto_desconhecido", {"erpProdutoIds": ["48214"]})]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "recusada_flow")]
    erro = _linha(1)["erro_definitivo"]
    assert "48214" in erro and "catálogo do Flow" in erro
    precos.rodar(AGORA + timedelta(seconds=31))
    assert len(flow.de("aprovacao")) == 1


@pytest.mark.parametrize(
    ("status", "error", "texto"),
    [
        (413, "payload_grande", "pedido grande demais para o Flow"),
        (400, "payload_invalido", "corpo fora do contrato"),
    ],
)
def test_corpo_definitivamente_recusado_bloqueia_local_e_nao_reenvia(
    mm, flow, status, error, texto
):
    _revisao()
    flow.aprovacao = [_erro(status, error)]
    precos.rodar(AGORA)
    linha = _linha(1)
    assert linha["situacao"] == "pendente_envio"
    assert texto in linha["bloqueio_local"]
    assert linha["erro_definitivo"] is None
    assert linha["tentada_em"] is None
    precos.rodar(AGORA + timedelta(seconds=31))
    assert len(flow.de("aprovacao")) == 1
    assert flow.rotas().count("decisoes") == 2  # não pausou


def test_corpo_invalido_local_bloqueia_sem_ir_ao_flow(mm, flow):
    _revisao()
    with router.env_connect("mm") as conn:
        conn.execute("UPDATE preco_revisao SET corpo_json = ?", (json.dumps({"tipo": "pedido"}),))
    precos.rodar(AGORA)
    assert flow.de("aprovacao") == []
    linha = _linha(1)
    assert "corpo fora do contrato" in linha["bloqueio_local"]
    assert linha["erro_definitivo"] is None


def test_pedido_envenenado_nao_bloqueia_os_outros_nem_a_tabela(mm, flow):
    _revisao(external="imp-a")
    _revisao(external="imp-b")
    flow.aprovacao = [_erro(400, "payload_invalido")]
    precos.rodar(AGORA)
    assert [c["externalId"] for c in flow.de("aprovacao")] == ["imp-a", "imp-b"]
    assert _linha(1, external="imp-a")["bloqueio_local"] is not None
    assert _situacoes(external="imp-b") == [(1, "enviada")]
    precos.rodar(AGORA + timedelta(seconds=61))
    assert flow.rotas().count("tabela") == 2


def test_resposta_2xx_fora_do_contrato_na_aprovacao_conta_como_enviada(mm, flow):
    _revisao()
    flow.aprovacao = [_erro(201, "resposta_fora_do_contrato", body='{"novo": 1}')]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "enviada")]
    assert "novo" in _linha(1)["resposta_json"]
    assert flow.rotas().count("decisoes") >= 1  # sem pausa
    precos.rodar(AGORA + timedelta(seconds=61))
    assert flow.rotas().count("tabela") == 2


def test_transitorio_num_pedido_nao_para_retiradas_nem_decisoes(mm, flow):
    _revisao(external="imp-velho")
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, "imp-velho", "pedido cancelado no Portal", em=AGORA)
    _revisao(external="imp-a")
    _revisao(external="imp-b")
    flow.aprovacao = [_erro(503, "erro_db")]
    n_dec = flow.rotas().count("decisoes")
    precos.rodar(AGORA + timedelta(seconds=31))
    pedidos = [c for c in flow.de("aprovacao") if c["tipo"] == "pedido"]
    assert [c["externalId"] for c in pedidos][-1] == "imp-a"  # parou o laço de envio
    assert [c["tipo"] for c in flow.de("aprovacao")][-1] == "retirada"
    assert flow.rotas().count("decisoes") == n_dec + 1


def test_503_continua_pendente_e_tenta_de_novo_no_proximo_ciclo(mm, flow):
    _revisao()
    flow.aprovacao = [_erro(503, "erro_db")]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "pendente_envio")]
    assert _linha(1)["tentada_em"] is not None
    precos.rodar(AGORA + timedelta(seconds=31))
    assert [c["revisao"] for c in flow.de("aprovacao")] == [1, 1]
    assert _situacoes() == [(1, "enviada")]


def test_erro_de_rede_e_transitorio(mm, flow):
    _revisao()
    flow.aprovacao = [FlowPCPClientError("conexão recusada", status_code=None)]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "pendente_envio")]


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (403, "aprovacao_preco_desligada"),
        (401, "unauthorized"),
        (400, "json_invalido"),
        (404, "tenant_nao_encontrado"),
        (422, "tenant_inativo"),
    ],
)
def test_config_ou_ordem_de_ligar_pausa_e_nunca_toca_a_revisao(mm, flow, status, error):
    _revisao()
    flow.aprovacao = [_erro(status, error)]
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "pendente_envio")]
    assert _linha(1)["erro_definitivo"] is None
    precos.rodar(AGORA + timedelta(minutes=5))
    assert len(flow.de("aprovacao")) == 1
    assert flow.rotas().count("tabela") == 2  # a pausa é só da aprovação
    precos.rodar(AGORA + timedelta(minutes=16))
    assert len(flow.de("aprovacao")) == 2


def test_retirada_409_reenvia_com_a_revisao_vigente_do_flow(mm, flow):
    _revisao()
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, IMP, "pedido cancelado no Portal", em=AGORA)
    flow.aprovacao = [
        _erro(409, "revisao_obsoleta", {"revisaoVigente": 2}),
        _resposta(2, situacao="retirada"),
    ]
    precos.rodar(AGORA + timedelta(seconds=31))
    retiradas = [c for c in flow.de("aprovacao") if c["tipo"] == "retirada"]
    assert [c["revisao"] for c in retiradas] == [1, 2]
    assert retiradas[0]["motivo"] == "pedido cancelado no Portal"
    assert retiradas[0]["empresaFaturadoraCnpj"] == MM
    ps.validar_envio(retiradas[0])
    assert _linha(1)["retirada_enviada_em"] is not None
    with router.env_connect("mm") as conn:
        assert revs.retiradas_pendentes(conn, 10) == []


@pytest.fixture
def logs():
    from app.utils.logger import logger

    msgs: list[str] = []
    sink = logger.add(lambda m: msgs.append(str(m)), level="WARNING")
    yield msgs
    logger.remove(sink)


def test_retirada_409_sem_revisao_nova_loga_uma_vez(mm, flow, logs):
    _revisao()
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, IMP, "pedido cancelado no Portal", em=AGORA)
    obsoleta = _erro(409, "revisao_obsoleta", {"revisaoVigente": 1})
    flow.aprovacao = [obsoleta, obsoleta, obsoleta]
    precos.rodar(AGORA + timedelta(seconds=31))
    precos.rodar(AGORA + timedelta(seconds=62))
    assert len([m for m in logs if "retirada" in m and "409" in m]) == 1
    assert _linha(1)["retirada_enviada_em"] is None


def test_retirada_invalida_loga_uma_vez(mm, flow, logs):
    _revisao()
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, IMP, "pedido cancelado no Portal", em=AGORA)
        # externalId acima de 128 caracteres: a retirada nunca passa no schema
        conn.execute("UPDATE preco_revisao SET import_id = ?", ("x" * 200,))
    precos.rodar(AGORA + timedelta(seconds=31))
    precos.rodar(AGORA + timedelta(seconds=62))
    assert [c for c in flow.de("aprovacao") if c["tipo"] == "retirada"] == []
    assert len([m for m in logs if "retirada" in m and "inválida" in m]) == 1


def test_retirada_200_com_aprovacao_nula_esta_feita(mm, flow):
    _revisao()
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, IMP, "pedido cancelado no Portal", em=AGORA)
    flow.aprovacao = [
        _resposta(1, situacao="retirada", aprovacao_id="00000000-0000-0000-0000-000000000000")
    ]
    precos.rodar(AGORA + timedelta(seconds=31))
    assert _linha(1)["retirada_enviada_em"] is not None


# ─── (c) decisões ───────────────────────────────────────────────────────────


def _decisoes(*ds):
    p = _ex("decisoesPagina")
    p["decisoes"] = list(ds)
    return ps.DecisoesPaginaV1.model_validate(p)


def test_decisao_da_vigente_e_gravada_e_confirmada_aplicada(mm, flow):
    _revisao()
    ds = _ex("decisoesPagina")["decisoes"]
    flow.decisoes = [_decisoes(*ds)]
    precos.rodar(AGORA)
    conf = flow.de("confirmar")
    assert len(conf) == 1
    assert conf[0]["schema"] == "precos.decisoes.confirmacao.v1"
    assert conf[0]["empresaFaturadoraCnpj"] == MM
    assert {(c["decisaoId"], c["resultado"]) for c in conf[0]["confirmacoes"]} == {
        (d["decisaoId"], "aplicada") for d in ds
    }
    ps.ConfirmacaoDecisoesV1.model_validate(conf[0])
    assert all(a["cursor"] is None and a["empresa"] == MM for a in flow.de("decisoes"))
    assert flow.rotas()[-1] == "decisoes"  # releu até vir vazia
    with router.env_connect("mm") as conn:
        assert set(revs.decisoes_da_vigente(conn, IMP)) == {f"{IMP}:0", f"{IMP}:1", f"{IMP}:2"}
        assert revs.confirmacoes_pendentes(conn, 10) == []


def test_decisao_de_revisao_velha_e_confirmada_descartada(mm, flow):
    _revisao()
    _revisao(h="sha256:bbbbbbbbbbbbbbbb")  # vigente agora é a 2
    d = _ex("decisoesPagina")["decisoes"][0]
    flow.decisoes = [_decisoes(d)]
    precos.rodar(AGORA)
    assert flow.de("confirmar")[0]["confirmacoes"] == [
        {"decisaoId": d["decisaoId"], "resultado": "descartada"}
    ]


def test_decisao_de_outra_empresa_e_confirmada_descartada_sem_gravar(mm, flow):
    _revisao()
    d = _ex("decisoesPagina")["decisoes"][0]
    d["empresaFaturadoraCnpj"] = NASMAR
    flow.decisoes = [_decisoes(d)]
    precos.rodar(AGORA)
    assert flow.de("confirmar")[0]["confirmacoes"] == [
        {"decisaoId": d["decisaoId"], "resultado": "descartada"}
    ]
    with router.env_connect("mm") as conn:
        assert revs.decisoes_da_vigente(conn, IMP) == {}


def test_confirmar_2xx_fora_do_contrato_nao_pausa_e_nao_reenvia(mm, flow):
    _revisao()
    d = _ex("decisoesPagina")["decisoes"][0]
    flow.decisoes = [_decisoes(d)]
    flow.confirmar = [_erro(200, "resposta_fora_do_contrato", body="{}")]
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        assert revs.confirmacoes_pendentes(conn, 10) == []
    precos.rodar(AGORA + timedelta(seconds=61))
    assert flow.rotas().count("tabela") == 2
    assert flow.rotas().count("decisoes") >= 2


def test_confirmacao_conflito_ou_nao_encontrada_so_loga_e_nao_reenvia(mm, flow):
    _revisao()
    ds = _ex("decisoesPagina")["decisoes"][:2]
    flow.decisoes = [_decisoes(*ds)]
    flow.confirmar = [
        ps.ConfirmacaoRespostaV1.model_validate(
            {
                "resultados": [
                    {"decisaoId": ds[0]["decisaoId"], "situacao": "conflito"},
                    {"decisaoId": ds[1]["decisaoId"].upper(), "situacao": "nao_encontrada"},
                ]
            }
        )
    ]
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        assert revs.confirmacoes_pendentes(conn, 10) == []
    precos.rodar(AGORA + timedelta(seconds=31))
    assert len(flow.de("confirmar")) == 1


def test_decisao_que_volta_sem_progresso_nao_prende_o_ciclo(mm, flow):
    _revisao()
    d = _ex("decisoesPagina")["decisoes"][0]
    flow.decisoes = [_decisoes(d)] * 50
    precos.rodar(AGORA)
    assert len(flow.de("decisoes")) == 2


def test_falha_no_confirmar_deixa_para_o_proximo_ciclo(mm, flow):
    _revisao()
    d = _ex("decisoesPagina")["decisoes"][0]
    flow.decisoes = [_decisoes(d)]
    flow.confirmar = [_erro(503, "erro_db")]
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        assert [p["decisao_id"] for p in revs.confirmacoes_pendentes(conn, 10)] == [d["decisaoId"]]
    flow.decisoes = [_decisoes(d)]
    precos.rodar(AGORA + timedelta(seconds=31))
    assert flow.de("confirmar")[-1]["confirmacoes"] == [
        {"decisaoId": d["decisaoId"], "resultado": "aplicada"}
    ]


# ─── ciclo da thread ────────────────────────────────────────────────────────


def test_ciclo_chama_precos_e_respeita_o_interruptor(monkeypatch):
    monkeypatch.setattr(envio_portal, "drenar", lambda: None)
    monkeypatch.setattr(envio_portal.catalogo_diario, "rodar", lambda: None)
    monkeypatch.setattr(envio_portal.cadastro_volta, "rodar_se_devido", lambda agora: None)
    chamadas = []
    monkeypatch.setattr(envio_portal.precos, "rodar", lambda agora: chamadas.append(agora))
    monkeypatch.delenv("PORTAL_FLOWPCP_PRECOS", raising=False)
    envio_portal.ciclo(datetime.min, AGORA)
    assert chamadas == [AGORA]
    monkeypatch.setenv("PORTAL_FLOWPCP_PRECOS", "0")
    envio_portal.ciclo(datetime.min, AGORA)
    assert chamadas == [AGORA]


def test_falha_nos_precos_nao_mata_a_thread(monkeypatch):
    monkeypatch.setattr(envio_portal, "drenar", lambda: None)
    monkeypatch.setattr(envio_portal.catalogo_diario, "rodar", lambda: None)
    monkeypatch.setattr(envio_portal.cadastro_volta, "rodar_se_devido", lambda agora: None)
    monkeypatch.delenv("PORTAL_FLOWPCP_PRECOS", raising=False)

    def explode(agora):
        raise RuntimeError("x")

    monkeypatch.setattr(envio_portal.precos, "rodar", explode)
    envio_portal.ciclo(datetime.min, AGORA)


# ─── cliente HTTP ───────────────────────────────────────────────────────────


def _cliente_http(handler, policies=None):
    out = OutboundClient(
        base_url="https://flow.test",
        default_headers={"X-Service-Token": "tok", "X-Tenant-Id": TENANT},
        transport=httpx.MockTransport(handler),
    )
    if policies is not None:
        original = out._send_with_retry

        def espia(method, url, **kw):
            policies.append((method, kw.get("retry_policy")))
            return original(method, url, **kw)

        out._send_with_retry = espia
    return FlowPCPClient(base_url="", service_token="tok", tenant_id=TENANT, outbound=out)


def test_cliente_get_tabela_com_filtro_cursor_limite_e_politica_de_leitura():
    vistos, policies = [], []

    def handler(req):
        vistos.append(req)
        return httpx.Response(200, json=EXEMPLOS["tabelaPagina"])

    c = _cliente_http(handler, policies)
    pagina = c.get_precos_tabela(MM, "c1", 500)
    assert pagina.cursor == EXEMPLOS["tabelaPagina"]["cursor"]
    req = vistos[0]
    assert req.url.path == "/api/portal-pedidos/precos/tabela"
    assert dict(req.url.params) == {"empresaFaturadora": MM, "cursor": "c1", "limite": "500"}
    assert req.headers["X-Tenant-Id"] == TENANT
    pol = policies[0][1]
    assert pol is not None and pol.should_retry_status(429) and pol.should_retry_status(503)


def test_cliente_get_sem_cursor_nao_manda_o_parametro():
    vistos = []

    def handler(req):
        vistos.append(req)
        return httpx.Response(200, json=EXEMPLOS["decisoesPagina"])

    _cliente_http(handler).get_precos_decisoes(MM, None, 100)
    assert vistos[0].url.path == "/api/portal-pedidos/precos/decisoes"
    assert dict(vistos[0].url.params) == {"empresaFaturadora": MM, "limite": "100"}


def test_cliente_posts_nas_rotas_do_contrato_com_politica_de_post_idempotente():
    vistos, policies = [], []

    def handler(req):
        vistos.append((req.url.path, json.loads(req.content), req.headers.get("Idempotency-Key")))
        if req.url.path.endswith("/confirmar"):
            return httpx.Response(200, json=EXEMPLOS["confirmacaoResposta"])
        return httpx.Response(201, json=EXEMPLOS["aprovacaoResposta"])

    c = _cliente_http(handler, policies)
    r = c.post_precos_aprovacao(EXEMPLOS["aprovacaoPedido"])
    assert r.situacao == "pendente"
    c.post_precos_confirmar(EXEMPLOS["confirmacao"])
    assert [v[0] for v in vistos] == [
        "/api/portal-pedidos/precos/aprovacoes",
        "/api/portal-pedidos/precos/decisoes/confirmar",
    ]
    assert vistos[0][1] == EXEMPLOS["aprovacaoPedido"]
    assert all(v[2] for v in vistos)
    assert all(p is not None and not p.should_retry_status(429) for _, p in policies)


def test_cliente_erro_carrega_status_error_e_details():
    def handler(req):
        return httpx.Response(
            409, json={"error": "revisao_obsoleta", "details": {"revisaoVigente": 3}}
        )

    with pytest.raises(FlowPCPClientError) as exc:
        _cliente_http(handler).post_precos_aprovacao(EXEMPLOS["aprovacaoPedido"])
    assert exc.value.status_code == 409
    assert exc.value.error == "revisao_obsoleta"
    assert exc.value.details == {"revisaoVigente": 3}


def test_cliente_resposta_fora_do_contrato_vira_erro():
    def handler(req):
        return httpx.Response(200, json={"schema": "outra"})

    with pytest.raises(FlowPCPClientError) as exc:
        _cliente_http(handler).get_precos_tabela(MM, None, 500)
    assert exc.value.error == "resposta_fora_do_contrato"


# ─── 2xx sem JSON (proxy, manutenção, captive portal): o Flow não recebeu nada ──


def _flow_http(monkeypatch, quebrada: str):
    """Flow de verdade pelo cliente HTTP (MockTransport); a rota `quebrada`
    responde 200 com HTML. As outras respondem como o contrato."""
    vistos: list[str] = []

    def handler(req):
        rota = req.url.path.rsplit("/precos/", 1)[1]
        vistos.append(rota)
        if rota == quebrada:
            return httpx.Response(200, text="<html>manutenção</html>")
        if rota == "tabela":
            return httpx.Response(200, json=EXEMPLOS["tabelaPagina"])
        if rota == "aprovacoes":
            corpo = json.loads(req.content)
            r = _resposta(corpo["revisao"], external=corpo["externalId"])
            return httpx.Response(201, json=r.model_dump(mode="json"))
        if rota == "decisoes":
            return httpx.Response(200, json=EXEMPLOS["decisoesPagina"])
        return httpx.Response(200, json=EXEMPLOS["confirmacaoResposta"])

    monkeypatch.setattr(precos, "_cliente", lambda cfg: _cliente_http(handler))
    return vistos


def test_aprovacao_200_html_fica_pendente_sem_pausar(mm, monkeypatch):
    _revisao()
    vistos = _flow_http(monkeypatch, "aprovacoes")
    precos.rodar(AGORA)
    assert _situacoes() == [(1, "pendente_envio")]
    assert _linha(1)["resposta_json"] is None
    precos.rodar(AGORA + timedelta(seconds=61))
    assert vistos.count("aprovacoes") == 2  # tentou de novo: sem pausa
    assert vistos.count("tabela") == 2


def test_retirada_200_html_fica_pendente_sem_pausar(mm, monkeypatch):
    _revisao()
    _flow_http(monkeypatch, "nenhuma")
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        revs.retirar(conn, IMP, "pedido cancelado no Portal", em=AGORA)
    vistos = _flow_http(monkeypatch, "aprovacoes")
    precos.rodar(AGORA + timedelta(seconds=61))
    assert _linha(1)["retirada_enviada_em"] is None
    precos.rodar(AGORA + timedelta(seconds=122))
    assert vistos.count("aprovacoes") == 2
    assert vistos.count("tabela") == 2


def test_confirmar_200_html_fica_pendente_sem_pausar(mm, monkeypatch):
    _revisao()
    vistos = _flow_http(monkeypatch, "decisoes/confirmar")
    precos.rodar(AGORA)
    with router.env_connect("mm") as conn:
        assert len(revs.confirmacoes_pendentes(conn, 10)) == 3
    precos.rodar(AGORA + timedelta(seconds=61))
    assert vistos.count("decisoes/confirmar") == 2
    assert vistos.count("tabela") == 2


@pytest.mark.parametrize("corpo", ["<html>manutenção</html>", "[1, 2]", '"ok"', ""])
def test_cliente_2xx_sem_objeto_json_e_resposta_sem_json(corpo):
    def handler(req):
        return httpx.Response(200, text=corpo)

    with pytest.raises(FlowPCPClientError) as exc:
        _cliente_http(handler).post_precos_aprovacao(EXEMPLOS["aprovacaoPedido"])
    assert exc.value.error == "resposta_sem_json"
    assert exc.value.status_code == 200


def test_cliente_2xx_fora_do_contrato_guarda_o_bruto_com_teto():
    grande = {"x": "a" * 200_000}

    def handler(req):
        return httpx.Response(200, json=grande)

    with pytest.raises(FlowPCPClientError) as exc:
        _cliente_http(handler).post_precos_aprovacao(EXEMPLOS["aprovacaoPedido"])
    assert exc.value.error == "resposta_fora_do_contrato"
    assert len(exc.value.body) == 64 * 1024
