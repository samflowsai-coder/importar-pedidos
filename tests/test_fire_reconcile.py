"""Reconciliação: achar no Fire o pedido que a operação cadastrou à mão.

Regra que atravessa o arquivo: a chave é SEMPRE dupla — número do pedido E
identidade do cliente. Casar por número sozinho tira pedido da fila de trabalho
sem ele estar no ERP, que é o pior desfecho possível desta feature.
"""

from __future__ import annotations

import pytest

from app.erp import fire_reconcile
from app.erp.fire_reconcile import Candidato, _buscar_no_fire_detalhado, buscar_no_fire


class _FakeCursor:
    def __init__(self, linhas):
        self._linhas = linhas
        self.executados = []

    def execute(self, sql, params=None):
        self.executados.append((sql, list(params or [])))

    def fetchall(self):
        return self._linhas

    def close(self):
        pass


class _FakeConn:
    def __init__(self, linhas):
        self._cursor = _FakeCursor(linhas)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _limpa():
    fire_reconcile.limpar_cache()
    yield
    fire_reconcile.limpar_cache()


def _plugar(monkeypatch, linhas, *, erro=None):
    """Substitui a conexão e o lookup de ambiente por fakes.

    Devolve o `_FakeConn` criado — o teste de lote inspeciona
    `.cursor().executados` nele diretamente, sem depender de nenhum gancho de
    teste exposto pelo módulo de produção.
    """
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    fake_conn = _FakeConn(linhas)

    def _connect(self, cfg):
        if erro:
            raise erro
        return fake_conn

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )
    return fake_conn


# (PEDIDO_CLIENTE, V.CODIGO, STATUS, DATA_PEDIDO, C.CODIGO, CPF_CNPJ)
def _linha(numero, codigo, cnpj, *, status="PEDIDO", data="2026-08-01", cliente=77):
    return (numero, codigo, status, data, cliente, cnpj)


def test_caminho_2_casa_por_cnpj_do_header(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "12.345.678/0001-99")])
    cand = Candidato(
        import_id="i1",
        numero="6702645869",
        cliente_codigo=None,
        cnpj_header="12.345.678/0001-99",
        cnpjs_entrega=(),
        data_pedido="2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 900
    assert achados["i1"].caminho == 2


def test_cnpj_divergente_nao_casa(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "99.999.999/0001-11")])
    cand = Candidato("i1", "6702645869", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_1_override_ganha_e_dispensa_cnpj(monkeypatch):
    _plugar(monkeypatch, [_linha("K01", 901, "", cliente=4242)])
    cand = Candidato("i1", "K01", 4242, None, (), "2026-08-01")
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 1


def test_caminho_3_marca_so_quando_todas_as_lojas_casam(monkeypatch):
    """Riachuelo: 3 lojas no pedido, 2 no Fire => NÃO marca."""
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22", "33.333.333/0003-33"),
        "2026-08-01",
    )
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_3_marca_quando_todas_as_lojas_casam(monkeypatch):
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22"),
        "2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].caminho == 3
    assert achados["i1"].lojas_casadas == 2
    assert achados["i1"].fire_codigo == 900  # menor CODIGO


def test_variante_sem_sufixo_casa_caso_sams(monkeypatch):
    _plugar(monkeypatch, [_linha("06654993", 902, "12.345.678/0001-99")])
    cand = Candidato("i1", "06654993-0000", None, "12.345.678/0001-99", (), "2026-08-01")
    assert "i1" in buscar_no_fire([cand], env_slug="mm")


def test_guarda_temporal_barra_numero_reusado(monkeypatch):
    """K01 do ano passado, mesmo cliente. Chave dupla não fecha; a data fecha."""
    _plugar(monkeypatch, [_linha("K01", 903, "12.345.678/0001-99", data="2024-01-10")])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_firebird_fora_devolve_vazio_sem_levantar(monkeypatch):
    _plugar(monkeypatch, [], erro=RuntimeError("host inalcançável"))
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_cool_down_evita_segunda_tentativa(monkeypatch):
    tentativas = []

    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    def _connect(self, cfg):
        tentativas.append(1)
        raise RuntimeError("fora")

    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection, "connect_with_config", _connect
    )

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    buscar_no_fire([cand], env_slug="mm")
    buscar_no_fire([cand], env_slug="mm")
    assert len(tentativas) == 1


def test_lote_acima_de_200_quebra_em_blocos(monkeypatch):
    fake_conn = _plugar(monkeypatch, [])
    cands = [
        Candidato(f"i{i}", f"P{i}", None, "12.345.678/0001-99", (), "2026-08-01")
        for i in range(250)
    ]
    buscar_no_fire(cands, env_slug="mm")
    # 250 números viram 2 execuções, não 250
    assert len(fake_conn.cursor().executados) == 2


# ── Fix round 1: caminho 3 furava a chave dupla com CNPJ de entrega sem dígito ──


def test_caminho_3_cnpj_de_entrega_sem_digito_nao_casa(monkeypatch):
    """'A COMBINAR' vira "" via cnpj_digits; CADASTRO.CPF_CNPJ NULL no Fire
    também vira "". Os dois vazios não podem se encontrar — falta de âncora
    tem que bloquear o match inteiro, nunca descartar só a entrada ruim."""
    _plugar(monkeypatch, [_linha("K01", 900, None, cliente=1)])
    cand = Candidato("i1", "K01", None, None, ("A COMBINAR",), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_caminho_3_uma_loja_real_mais_sem_cnpj_nao_casa(monkeypatch):
    """1 loja real bate, mas a 2ª entrega sem CNPJ não pode ser ignorada —
    senão 'lojas_casadas' infla contando uma loja não-verificável como se
    tivesse batido."""
    _plugar(monkeypatch, [_linha("K01", 900, "11.111.111/0001-11", cliente=1)])
    cand = Candidato("i1", "K01", None, None, ("11.111.111/0001-11", "S/CNPJ"), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


# ── Fix round 1: "nunca levanta" precisa valer nas 4 fases, não só em 2 ──


def test_to_fb_config_falha_nao_levanta_nem_arma_cooldown(monkeypatch):
    """Não é falha de rede (lê SQLite local + decripta senha) — não pode
    armar o cool-down do slug."""
    tentativas = []

    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )

    def _to_fb_config(env):
        tentativas.append(1)
        raise RuntimeError("senha corrompida")

    monkeypatch.setattr(fire_reconcile.environments_repo, "to_fb_config", _to_fb_config)

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}
    assert buscar_no_fire([cand], env_slug="mm") == {}
    assert len(tentativas) == 2  # sem cool-down: a segunda chamada tenta de novo


def test_pedido_cliente_nao_string_nao_levanta(monkeypatch):
    """PEDIDO_CLIENTE não-string (ex.: numérico no Fire) quebraria o TRIM em
    Python (`.strip()` num int) — precisa virar {} e log, não estourar."""
    linha_ruim = (12345, 900, "PEDIDO", "2026-08-01", 77, "12.345.678/0001-99")
    _plugar(monkeypatch, [linha_ruim])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_v_codigo_nulo_nao_levanta(monkeypatch):
    """V.CODIGO nulo numa linha casada quebra o min() do desempate
    (None não compara com int) — precisa virar {} e log, não estourar."""
    linhas = [
        _linha("K01", None, "12.345.678/0001-99"),
        _linha("K01", 900, "12.345.678/0001-99"),
    ]
    _plugar(monkeypatch, linhas)
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    assert buscar_no_fire([cand], env_slug="mm") == {}


# ── Fix round 1: data ilegível do candidato não pode desarmar a guarda ──


def test_data_candidato_ilegivel_nao_desliga_a_guarda(monkeypatch):
    """Data do candidato preenchida mas ilegível não é a mesma coisa que
    'sem data' — a incerteza tem que descartar a linha, não deixar passar."""
    _plugar(monkeypatch, [_linha("K01", 900, "12.345.678/0001-99", data="2026-08-01")])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "não é uma data")
    assert buscar_no_fire([cand], env_slug="mm") == {}


def test_candidato_sem_data_guarda_nao_se_aplica(monkeypatch):
    """Regressão: candidato SEM data_pedido continua sem guarda (regra da spec)."""
    _plugar(monkeypatch, [_linha("K01", 900, "12.345.678/0001-99", data="2020-01-01")])
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), None)
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 900


# ── Fix round 1: fire_status deve vir da mesma linha que decide fire_codigo ──


def test_caminho_3_fire_status_vem_da_linha_de_menor_codigo(monkeypatch):
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "11.111.111/0001-11", cliente=1, status="APROVADO"),
            _linha("6702645869", 901, "22.222.222/0002-22", cliente=2, status="FATURADO"),
        ],
    )
    cand = Candidato(
        "i1",
        "6702645869",
        None,
        None,
        ("11.111.111/0001-11", "22.222.222/0002-22"),
        "2026-08-01",
    )
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_status == "APROVADO"


def test_desempate_ignora_cancelado_mesmo_com_codigo_menor(monkeypatch):
    """Cadastrou, cancelou, recadastrou: duas linhas no Fire com o mesmo
    PEDIDO_CLIENTE. `min(V.CODIGO)` sozinho pega a linha CANCELADA (mais
    antiga, código menor) — o selo mostraria "Cadastrado no Fire (CANCELADO)"
    para um pedido que na verdade está FATURADO. O desempate tem que preferir
    a linha não-CANCELADO; só entre as não-canceladas o menor CODIGO decide.
    """
    _plugar(
        monkeypatch,
        [
            _linha("6702645869", 900, "12.345.678/0001-99", status="CANCELADO"),
            _linha("6702645869", 901, "12.345.678/0001-99", status="FATURADO"),
        ],
    )
    cand = Candidato("i1", "6702645869", None, "12.345.678/0001-99", (), "2026-08-01")
    achados = buscar_no_fire([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 901
    assert achados["i1"].fire_status == "FATURADO"


# ── Fix round 1 (reviewer): erro_conexao explícito no retorno, não em getter
# separado sobre estado global — imune à corrida entre duas chamadas
# concorrentes pro mesmo env_slug (casos B/C/D provados pelo reviewer) ──


def test_detalhado_sucesso_tem_erro_conexao_false(monkeypatch):
    _plugar(monkeypatch, [_linha("6702645869", 900, "12.345.678/0001-99")])
    cand = Candidato("i1", "6702645869", None, "12.345.678/0001-99", (), "2026-08-01")
    achados, erro_conexao = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert achados["i1"].fire_codigo == 900
    assert erro_conexao is False


def test_detalhado_falha_de_conexao_tem_erro_conexao_true(monkeypatch):
    _plugar(monkeypatch, [], erro=RuntimeError("host inalcançável"))
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    achados, erro_conexao = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert achados == {}
    assert erro_conexao is True


def test_detalhado_cooldown_ativo_tem_erro_conexao_true(monkeypatch):
    """A 2ª chamada nem tenta a rede (cool-down) — mesmo assim é
    erro_conexao=True: sabemos, agora, que o Fire está indisponível."""
    tentativas = []
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "to_fb_config", lambda env: object()
    )

    def _connect(self, cfg):
        tentativas.append(1)
        raise RuntimeError("fora")

    monkeypatch.setattr(fire_reconcile.FirebirdConnection, "connect_with_config", _connect)

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    _, erro1 = _buscar_no_fire_detalhado([cand], env_slug="mm")
    _, erro2 = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert erro1 is True
    assert erro2 is True
    assert len(tentativas) == 1  # só a 1ª tentou de verdade; a 2ª leu o cool-down


def test_detalhado_to_fb_config_invalido_tem_erro_conexao_true(monkeypatch):
    """Caso C do reviewer: senha do ambiente não decripta. Não é falha de
    rede (não arma cool-down), mas é "não consegui consultar" pra quem lê o
    resultado — tem que chegar como erro_conexao=True, não False."""
    tentativas = []
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )

    def _to_fb_config(env):
        tentativas.append(1)
        raise RuntimeError("senha corrompida")

    monkeypatch.setattr(fire_reconcile.environments_repo, "to_fb_config", _to_fb_config)

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    achados, erro_conexao = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert achados == {}
    assert erro_conexao is True
    assert len(tentativas) == 1  # não arma cool-down: sem 2ª chamada aqui pra provar


def test_detalhado_ambiente_inexistente_tem_erro_conexao_true(monkeypatch):
    monkeypatch.setattr(fire_reconcile.environments_repo, "get_by_slug", lambda slug: None)
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    achados, erro_conexao = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert achados == {}
    assert erro_conexao is True


class _FakeCursorRuim:
    def execute(self, sql, params=None):
        raise RuntimeError("SQL malformado")

    def close(self):
        pass


class _FakeConnRuim:
    def cursor(self):
        return _FakeCursorRuim()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# Fix de 2026-08-24 (achado em teste real de navegador): falha de LEITURA
# devolvia erro_conexao=False, e o log mostrava a leitura falhando enquanto a
# UI dizia "consultei e nenhum dos pedidos está cadastrado lá ainda" — mentira
# pra operadora, que não tem como saber que a consulta nem completou. As duas
# asserções abaixo estavam coladas num teste só (que fixava o bug como
# contrato); agora são dois testes distintos, cada um com a garantia certa.


def test_detalhado_leitura_ruim_tem_erro_conexao_true(monkeypatch):
    """Conectou (Firebird de pé); a query/leitura falhou. Ainda é
    erro_conexao=True: quem lê o resultado (a operadora, via `Resultado.
    status` do runner) não consegue distinguir "não conectei" de "conectei
    mas não consegui ler" — as duas são "não consegui consultar o Fire" pra
    ela. A distinção fina entre os dois só importa pro cool-down (ver o
    teste seguinte)."""
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(fire_reconcile.environments_repo, "to_fb_config", lambda env: object())
    monkeypatch.setattr(
        fire_reconcile.FirebirdConnection,
        "connect_with_config",
        lambda self, cfg: _FakeConnRuim(),
    )

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    achados, erro_conexao = _buscar_no_fire_detalhado([cand], env_slug="mm")
    assert achados == {}
    assert erro_conexao is True


def test_detalhado_leitura_ruim_nao_arma_cooldown(monkeypatch):
    """Bug de dado/SQL, não de conexão — não pode armar o cool-down (senão
    qualquer linha suja no Fire suprimiria a reconciliação do ambiente
    inteiro por 45s, o mesmo desvio já catalogado pro `depara_cliente`).
    Prova: uma 2ª chamada, na sequência, ainda tenta a rede de verdade — não
    é interceptada pelo cool-down (comparar com
    `test_detalhado_cooldown_ativo_tem_erro_conexao_true`, onde a 2ª chamada
    NÃO tenta)."""
    tentativas = []
    monkeypatch.setattr(
        fire_reconcile.environments_repo, "get_by_slug", lambda slug: {"slug": slug}
    )
    monkeypatch.setattr(fire_reconcile.environments_repo, "to_fb_config", lambda env: object())

    def _connect(self, cfg):
        tentativas.append(1)
        return _FakeConnRuim()

    monkeypatch.setattr(fire_reconcile.FirebirdConnection, "connect_with_config", _connect)

    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    _buscar_no_fire_detalhado([cand], env_slug="mm")
    _buscar_no_fire_detalhado([cand], env_slug="mm")

    assert len(tentativas) == 2  # cool-down não armou: a 2ª tentou a rede de novo


def test_buscar_no_fire_publico_continua_so_o_dict(monkeypatch):
    """`buscar_no_fire()` é o wrapper de uma linha — devolve só o dict, nunca
    a tupla, preservando o contrato dos 18 testes acima (`== {}`)."""
    _plugar(monkeypatch, [], erro=RuntimeError("host inalcançável"))
    cand = Candidato("i1", "K01", None, "12.345.678/0001-99", (), "2026-08-01")
    resultado = buscar_no_fire([cand], env_slug="mm")
    assert resultado == {}
    assert isinstance(resultado, dict)
