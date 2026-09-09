"""O watcher roteia, mas sem humano na frente: quem não sabe responder retém.

`test_desligado_importa_no_ambiente_da_pasta` é o teste que protege a adoção
— em 'desligado' (default) nada muda em relação ao comportamento pré-feature.

A cerca de `tests/conftest.py::_no_real_firebird` bloqueia qualquer teste que
deixe `deps_padrao()` real tentar `historico.consultar` (Firebird real). Os
cenários aqui resolvem no degrau 1 (documento) ou caem direto em 'perguntar'
sem depender do degrau 2 — mas `cnpjs_do_pedido` inclui `customer_cnpj`
mesmo quando o fornecedor é `None`, então o degrau 2 SERIA consultado. Por
isso, como em `tests/test_routing_wiring.py`, substituímos `deps_padrao`
por uma versão sem Firebird: `env_por_cnpj` e `memoria` continuam reais
(SQLite puro, sem risco), só `historico` vira uma tupla vazia.
"""

from __future__ import annotations

import pytest

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router
from app.routing import ambiente as routing
from app.worker.jobs import scan_environments

NASMAR = "34513679000134"
MM = "35394871000111"


@pytest.fixture
def dois_ambientes(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome, cnpj in (("nasmar", "Nasmar", NASMAR), ("mm", "MM", MM)):
        d = tmp_path / slug
        (d / "in").mkdir(parents=True)
        (d / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug, name=nome, cnpj=cnpj,
            watch_dir=str(d / "in"), output_dir=str(d / "out"),
            fb_path=str(d / f"{slug}.fdb"),
        )

    # Substitui só o degrau que faria I/O real (Firebird) — ver
    # tests/test_routing_wiring.py. `env_por_cnpj` e `memoria` continuam
    # reais (SQLite puro); `historico` vira vazio.
    real = routing.deps_padrao()

    def _deps_sem_firebird():
        return routing.Deps(
            env_por_cnpj=real.env_por_cnpj,
            historico=lambda cnpjs: (),  # noqa: ARG005 — nenhum cenário depende do degrau 2
            memoria=real.memoria,
        )

    monkeypatch.setattr(routing, "deps_padrao", _deps_sem_firebird)

    yield tmp_path


def _imports(slug):
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        return repo.list_imports(limit=50)


def _order(supplier):
    from app.models.order import Order, OrderHeader, OrderItem

    return Order(
        header=OrderHeader(order_number="4711", customer_cnpj="11222333000181",
                           customer_name="DAJU", supplier_cnpj=supplier),
        items=[OrderItem(description="Meia", quantity=5, unit_price=10.0)],
    )


def _arquivo(tmp_path, slug="mm", nome="PEDIDO.pdf"):
    p = tmp_path / slug / "in" / nome
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_desligado_importa_no_ambiente_da_pasta(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("desligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("mm")) == 1     # a pasta manda, como hoje
    assert _imports("nasmar") == []


def test_ligado_importa_no_ambiente_do_documento(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("nasmar")) == 1
    assert _imports("mm") == []


def test_ligado_sem_resposta_retem_o_arquivo_e_nao_importa(dois_ambientes, monkeypatch):
    """Nunca um ambiente default. O arquivo fica na pasta e vira pendencia."""
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(None))
    p = _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert _imports("mm") == []
    assert _imports("nasmar") == []
    assert p.exists(), "o arquivo tem que continuar na pasta de entrada"
    assert roteamento_repo.contar_pendencias() == 1


def test_pendencia_nao_infla_a_cada_varredura(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(None))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    scan_environments.run_scan()
    scan_environments.run_scan()
    assert roteamento_repo.contar_pendencias() == 1
    assert roteamento_repo.listar_pendencias()[0]["visto_vezes"] == 3


def test_observando_importa_na_pasta_e_grava_sombra(dois_ambientes, monkeypatch):
    roteamento_repo.set_modo("observando", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("mm")) == 1
    t = roteamento_repo.taxa()
    assert t["total"] == 1 and t["divergiu"] == 1


def _explode():
    raise RuntimeError("Firebird fora do ar")


def test_observando_com_falha_do_roteador_importa_na_pasta_sem_sombra(dois_ambientes, monkeypatch):
    """Contrato igual à Task 10: exceção do roteador em 'observando' vira log
    e o arquivo é importado normalmente na pasta varrida, sem sombra."""
    roteamento_repo.set_modo("observando", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    monkeypatch.setattr(routing, "deps_padrao", _explode)
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("mm")) == 1
    assert roteamento_repo.taxa()["total"] == 0  # falha não virou sombra


def test_ligado_com_falha_do_roteador_retem_o_arquivo(dois_ambientes, monkeypatch):
    """Sem humano pra perguntar, falha do roteador em 'ligado' NÃO cai pro
    ambiente varrido em silêncio — vira retenção, igual a 'não soube responder'."""
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    monkeypatch.setattr(routing, "deps_padrao", _explode)
    p = _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert _imports("mm") == []
    assert _imports("nasmar") == []
    assert p.exists()
    assert roteamento_repo.contar_pendencias() == 1


def test_duplicidade_e_global_arquivo_roteado_nao_reimporta_na_pasta_original(
    dois_ambientes, monkeypatch
):
    """Pedido roteado pra Nasmar; se o mesmo arquivo aparecer de novo na pasta
    da MM (mesmo sha256), não pode reimportar — o sha é único no Portal."""
    roteamento_repo.set_modo("ligado", por="t")
    monkeypatch.setattr(scan_environments, "pipeline_process", lambda f: _order(NASMAR))
    _arquivo(dois_ambientes)
    scan_environments.run_scan()
    assert len(_imports("nasmar")) == 1

    # Mesmo conteúdo (mesmo sha256) reaparece na pasta da MM.
    _arquivo(dois_ambientes, slug="mm", nome="PEDIDO_DE_NOVO.pdf")
    scan_environments.run_scan()
    assert len(_imports("nasmar")) == 1
    assert _imports("mm") == []
