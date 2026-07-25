# tests/test_depara_cliente.py
from __future__ import annotations

import pytest

import app.erp.depara_cliente as dc


@pytest.fixture(autouse=True)
def _limpa_cache():
    dc.limpar_cache()
    yield
    dc.limpar_cache()


# (V.CODIGO, V.STATUS, V.CODNF, C.CODIGO, C.NOME, C.RAZAO_SOCIAL, C.CPF_CNPJ)
_LINHA_AF066 = (
    301,
    "FATURADO",
    9001,
    55,
    "AUTHENTIC FEET",
    "AUTHENTIC FEET LTDA",
    "10.772.208/0001-82",
)
_LINHA_AF066_B = (
    302,
    "FATURADO",
    9002,
    55,
    "AUTHENTIC FEET",
    "AUTHENTIC FEET LTDA",
    "10.772.208/0001-82",
)
_LINHA_OUTRO = (
    303,
    "PEDIDO",
    None,
    77,
    "DAKOTA NORDESTE",
    "DAKOTA NORDESTE S/A",
    "00.465.813/0004-08",
)


class _FakeCursor:
    """Modela `fdb.Cursor` — é ELA que tem `.execute()`/`.fetchall()`, não a
    conexão (`fdb.Connection` só tem `.cursor()`/`.execute_immediate()`)."""

    def __init__(self, rows, capturado):
        self._rows = rows
        self._capturado = capturado
        self.closed = False

    def execute(self, sql, params):
        self._capturado.append((sql, params))
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        self.closed = True


class _FakeConn:
    """Modela `fdb.Connection`: só abre cursor, não executa direto."""

    def __init__(self, rows, capturado):
        self._rows = rows
        self._capturado = capturado

    def cursor(self):
        return _FakeCursor(self._rows, self._capturado)


@pytest.fixture
def fake_fire(monkeypatch):
    """Aponta o resolver pra um Firebird falso. Devolve a lista de (sql, params)."""
    capturado: list = []

    def _instalar(rows, *, boom: Exception | None = None):
        import contextlib

        class _FakeFirebird:
            @contextlib.contextmanager
            def connect_with_config(self, cfg):
                if boom is not None:
                    raise boom
                yield _FakeConn(rows, capturado)

        monkeypatch.setattr(dc, "FirebirdConnection", _FakeFirebird)
        monkeypatch.setattr(
            dc.environments_repo, "get_by_slug", lambda slug: {"id": "env-4", "slug": slug}
        )
        monkeypatch.setattr(
            dc.environments_repo, "to_fb_config", lambda env: {"path": "x.fdb", "host": "h"}
        )
        return capturado

    return _instalar


def test_match_unico_resolve(fake_fire):
    fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.motivo == "ok"
    assert r.cnpj == "10772208000182"  # só dígitos
    assert r.nome == "AUTHENTIC FEET LTDA"  # RAZAO_SOCIAL tem precedência
    assert r.pedidos_no_4 == [{"codigo": 301, "status": "FATURADO", "codnf": 9001}]


def test_varias_linhas_mesmo_cnpj_resolve(fake_fire):
    # Caso real: AF086 tem 3 linhas no .4, todas do mesmo cliente.
    fake_fire([_LINHA_AF066, _LINHA_AF066_B])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.cnpj == "10772208000182"
    assert len(r.pedidos_no_4) == 2


def test_cnpjs_diferentes_e_ambiguo(fake_fire):
    fake_fire([_LINHA_AF066, _LINHA_OUTRO])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "ambiguo"
    assert r.cnpj is None


@pytest.mark.parametrize("cpf_cnpj_bruto", ["ISENTO", "0", "", "   ", "12.345.678/0001"])
def test_cnpj_malformado_vira_sem_cnpj_nao_derruba_push(fake_fire, cpf_cnpj_bruto):
    """CADASTRO.CPF_CNPJ legado às vezes tem lixo ('ISENTO', '0', meio digitado).
    Antes disso, QUALQUER string não-vazia resolvia — e um CNPJ curto demais
    (ex: '0') é rejeitado pelo contrato do Flow (400), o que faz o push
    inteiro cair pro outbox e morrer na dead letter. Uma falha estrita aqui é
    pior que o fallback desenhado (subir como revenda)."""
    linha = (301, "FATURADO", 9001, 55, "CLIENTE X", "CLIENTE X LTDA", cpf_cnpj_bruto)
    fake_fire([linha])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "sem_cnpj"
    assert r.cnpj is None


def test_cpf_valido_11_digitos_resolve(fake_fire):
    linha = (301, "FATURADO", 9001, 55, "PESSOA FISICA", "", "123.456.789-01")
    fake_fire([linha])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.cnpj == "12345678901"


def test_zero_hits_e_nao_encontrado(fake_fire):
    fake_fire([])
    r = dc.resolver_cliente_real("PULMÃO", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "nao_encontrado"


@pytest.mark.parametrize("chave", [None, "", "   "])
def test_chave_vazia_nao_consulta_o_banco(fake_fire, chave):
    capturado = fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real(chave, revenda_slug="nasmar")
    assert r.motivo == "sem_chave"
    assert capturado == []  # não abriu conexão à toa


def test_chave_vai_trimada_pro_bind(fake_fire):
    capturado = fake_fire([_LINHA_AF066])
    dc.resolver_cliente_real("  AF066  ", revenda_slug="nasmar")
    _sql, params = capturado[0]
    assert params == ("AF066",)


def test_nome_cai_pra_nome_quando_razao_social_vazia(fake_fire):
    fake_fire([(301, "PEDIDO", None, 55, "AUTHENTIC FEET", "   ", "10.772.208/0001-82")])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.nome == "AUTHENTIC FEET"


def test_erro_de_conexao_nao_levanta(fake_fire):
    fake_fire([], boom=RuntimeError("firebird fora do ar"))
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "erro_conexao"


def test_ambiente_inexistente_e_config_invalida(monkeypatch):
    monkeypatch.setattr(dc.environments_repo, "get_by_slug", lambda slug: None)
    r = dc.resolver_cliente_real("AF066", revenda_slug="fantasma")
    assert r.resolvido is False
    assert r.motivo == "config_invalida"


def test_lookup_do_ambiente_explode_nao_levanta(monkeypatch):
    # get_by_slug (SQLite) pode falhar por motivo próprio (lock, disco, etc.) —
    # isso não é "ambiente não existe", mas o contrato ainda é nunca levantar.
    def _boom(slug):
        raise RuntimeError("sqlite indisponível")

    monkeypatch.setattr(dc.environments_repo, "get_by_slug", _boom)
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is False
    assert r.motivo == "erro_conexao"


# ── revenda_slug no resultado (achado 3 da revisão): "qual banco respondeu" ──
#
# Se `intercompany_env_slug` apontar pro ambiente errado, a única forma de
# auditar depois quais pedidos foram resolvidos sob a config ruim é o próprio
# resultado carregar o slug usado — em TODO caminho, inclusive falha.


@pytest.mark.parametrize("chave", [None, "", "   "])
def test_revenda_slug_presente_mesmo_em_sem_chave(fake_fire, chave):
    fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real(chave, revenda_slug="nasmar")
    assert r.revenda_slug == "nasmar"


def test_revenda_slug_presente_em_config_invalida(monkeypatch):
    monkeypatch.setattr(dc.environments_repo, "get_by_slug", lambda slug: None)
    r = dc.resolver_cliente_real("AF066", revenda_slug="fantasma")
    assert r.revenda_slug == "fantasma"


def test_revenda_slug_presente_em_erro_conexao(fake_fire):
    fake_fire([], boom=RuntimeError("firebird fora do ar"))
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.revenda_slug == "nasmar"


def test_revenda_slug_presente_em_erro_no_lookup_do_ambiente(monkeypatch):
    monkeypatch.setattr(
        dc.environments_repo, "get_by_slug", lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.revenda_slug == "nasmar"


def test_revenda_slug_presente_quando_resolvido(fake_fire):
    fake_fire([_LINHA_AF066])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert r.revenda_slug == "nasmar"


def test_revenda_slug_presente_em_ambiguo_e_sem_cnpj(fake_fire):
    fake_fire([_LINHA_AF066, _LINHA_OUTRO])
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.motivo == "ambiguo"
    assert r.revenda_slug == "nasmar"


def test_resolucao_ok_e_cacheada(fake_fire):
    capturado = fake_fire([_LINHA_AF066])
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert len(capturado) == 1  # segunda chamada saiu do cache


def test_nao_encontrado_nao_e_cacheado(fake_fire):
    # O pedido pode ser criado no .4 depois — cachear negativo envenenaria
    # o processo (o servidor web fica de pé por dias).
    capturado = fake_fire([])
    dc.resolver_cliente_real("AF999", revenda_slug="nasmar")
    dc.resolver_cliente_real("AF999", revenda_slug="nasmar")
    assert len(capturado) == 2


# ── Cool-down por revenda_slug depois de erro_conexao (achado 4) ────────────
#
# `fdb` não expõe timeout — host inalcançável trava até ~180s. Sem cool-down,
# CADA preview aberto/refresh/export durante uma queda do Firebird da revenda
# paga o stall inteiro, por clique, pra sempre (handlers síncronos num
# threadpool limitado). O cache positivo (acima) já existe; isto é um cache
# NEGATIVO de curta duração — só para `erro_conexao`, nunca para
# `nao_encontrado` (o pedido pode ser criado depois).


def _fake_firebird_boom(monkeypatch, tentativas):
    import contextlib

    class _FakeFirebird:
        @contextlib.contextmanager
        def connect_with_config(self, cfg):
            tentativas.append(1)
            raise RuntimeError("firebird fora do ar")
            yield  # pragma: no cover — inalcançável; só mantém a função geradora

    monkeypatch.setattr(dc, "FirebirdConnection", _FakeFirebird)
    monkeypatch.setattr(dc.environments_repo, "get_by_slug", lambda slug: {"id": "e", "slug": slug})
    monkeypatch.setattr(dc.environments_repo, "to_fb_config", lambda env: {"path": "x"})


def test_apos_erro_conexao_curto_circuita_sem_tocar_rede(monkeypatch):
    tentativas: list = []
    _fake_firebird_boom(monkeypatch, tentativas)
    relogio = {"t": 1000.0}
    monkeypatch.setattr(dc, "_clock", lambda: relogio["t"])

    r1 = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r1.motivo == "erro_conexao"
    assert len(tentativas) == 1

    relogio["t"] += 5  # bem dentro da janela de cool-down
    r2 = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r2.motivo == "erro_conexao"
    assert len(tentativas) == 1  # curto-circuitou: não tocou a rede de novo


def test_cooldown_expira_e_tenta_de_novo(monkeypatch):
    tentativas: list = []
    _fake_firebird_boom(monkeypatch, tentativas)
    relogio = {"t": 1000.0}
    monkeypatch.setattr(dc, "_clock", lambda: relogio["t"])

    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    relogio["t"] += dc._ERRO_CONEXAO_COOLDOWN_S + 1  # janela expirou
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert len(tentativas) == 2


def test_cooldown_e_isolado_por_revenda_slug(monkeypatch):
    tentativas: list = []
    _fake_firebird_boom(monkeypatch, tentativas)
    monkeypatch.setattr(dc, "_clock", lambda: 1000.0)

    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    dc.resolver_cliente_real("AF066", revenda_slug="outra-revenda")
    assert len(tentativas) == 2  # cool-down de "nasmar" não vaza pra outro slug


def test_limpar_cache_reseta_cooldown(monkeypatch):
    tentativas: list = []
    _fake_firebird_boom(monkeypatch, tentativas)
    monkeypatch.setattr(dc, "_clock", lambda: 1000.0)

    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    dc.limpar_cache()
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")  # ainda na janela, mas...
    assert len(tentativas) == 2  # ...limpar_cache() também zera o cool-down


def test_erro_no_lookup_do_ambiente_tambem_ativa_cooldown(monkeypatch):
    """erro_conexao no lookup do ambiente (get_by_slug explode) conta tanto
    quanto falha de conexão Firebird — mesmo motivo, mesma proteção."""
    tentativas: list = []

    def _boom(slug):
        tentativas.append(1)
        raise RuntimeError("sqlite lock")

    monkeypatch.setattr(dc.environments_repo, "get_by_slug", _boom)
    relogio = {"t": 1000.0}
    monkeypatch.setattr(dc, "_clock", lambda: relogio["t"])

    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    relogio["t"] += 5
    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert len(tentativas) == 1  # segunda chamada nem tentou get_by_slug de novo


def test_sucesso_depois_de_erro_limpa_o_cooldown(monkeypatch):
    """Uma conexão que funciona depois de uma falha anterior prova que o
    Firebird voltou — não faz sentido manter o slug em cool-down."""
    import contextlib

    tentativas: list = []
    modo = {"boom": True}

    class _FakeFirebird:
        @contextlib.contextmanager
        def connect_with_config(self, cfg):
            tentativas.append(1)
            if modo["boom"]:
                raise RuntimeError("firebird fora do ar")
            yield _FakeConn([_LINHA_AF066], [])

    monkeypatch.setattr(dc, "FirebirdConnection", _FakeFirebird)
    monkeypatch.setattr(dc.environments_repo, "get_by_slug", lambda slug: {"id": "e", "slug": slug})
    monkeypatch.setattr(dc.environments_repo, "to_fb_config", lambda env: {"path": "x"})
    relogio = {"t": 1000.0}
    monkeypatch.setattr(dc, "_clock", lambda: relogio["t"])

    dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert len(tentativas) == 1

    relogio["t"] += dc._ERRO_CONEXAO_COOLDOWN_S + 1  # janela expira
    modo["boom"] = False
    r = dc.resolver_cliente_real("AF066", revenda_slug="nasmar")
    assert r.resolvido is True
    assert len(tentativas) == 2

    # falha de novo, imediatamente: não deve estar em cool-down residual
    modo["boom"] = True
    dc.resolver_cliente_real("BF999", revenda_slug="nasmar")
    assert len(tentativas) == 3
