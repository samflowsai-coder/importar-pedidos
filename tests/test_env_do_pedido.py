"""De qual empresa é este pedido? — o resolver e o gate.

Espelha a fixture de `tests/test_imports_cross_env.py`: duas empresas reais
em `tmp_path`, SQLite puro, zero Firebird.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, roteamento_repo, router


@pytest.fixture
def duas_empresas(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    yield tmp_path


def _grava(slug: str, ident: str) -> None:
    env = environments_repo.get_by_slug(slug)
    with env_context.active_env(env["id"], env["slug"]):
        repo.insert_import(
            {
                "id": ident,
                "source_filename": f"{ident}.pdf",
                "imported_at": "2026-09-11T10:00:00",
                "order_number": ident,
                "customer_name": "DAJU",
                "status": "success",
                "portal_status": "parsed",
            }
        )


def test_resolver_acha_na_empresa_certa(duas_empresas):
    from app.web.dependencies.pedido import env_do_import_id

    _grava("nasmar", "N1")
    achado = env_do_import_id("N1")
    assert achado is not None
    assert achado["slug"] == "nasmar"


def test_resolver_devolve_none_para_id_inexistente(duas_empresas):
    from app.web.dependencies.pedido import env_do_import_id

    assert env_do_import_id("nao-existe") is None


def test_resolver_ignora_empresa_desativada(duas_empresas):
    """Desativar uma empresa para a atividade nela, nao so a esconde da UI."""
    from app.web.dependencies.pedido import env_do_import_id

    _grava("nasmar", "N1")
    environments_repo.soft_delete(environments_repo.get_by_slug("nasmar")["id"])
    assert env_do_import_id("N1") is None


def _app_de_teste():
    """App mínimo que expõe o que a dependency ativou no contextvar."""
    from app.web.dependencies.pedido import env_do_pedido

    app = FastAPI()

    @app.get("/x/{import_id}")
    def rota(import_id: str, env=Depends(env_do_pedido)):
        ativo = env_context.current()
        return {
            "env_da_dependency": env["slug"] if env else None,
            # `ActiveEnv` e' TypedDict (app/persistence/context.py:26) -- em
            # runtime e' um dict puro, entao acesso e' por chave, nao atributo.
            "contextvar": ativo["slug"] if ativo else None,
        }

    return TestClient(app)


def test_ligado_resolve_e_ativa_o_ambiente_do_pedido(duas_empresas):
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = _app_de_teste().get("/x/N1")
    assert r.status_code == 200
    assert r.json() == {"env_da_dependency": "nasmar", "contextvar": "nasmar"}


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_fora_de_ligado_a_dependency_e_inerte(duas_empresas, modo, monkeypatch):
    """Nao resolve, nao ativa, e NEM CONSULTA BANCO."""
    from app.web.dependencies import pedido as mod

    _grava("nasmar", "N1")
    roteamento_repo.set_modo(modo, por="teste")

    chamou: list[str] = []
    monkeypatch.setattr(mod, "env_do_import_id", lambda i: chamou.append(i) or None)
    r = _app_de_teste().get("/x/N1")
    assert r.json() == {"env_da_dependency": None, "contextvar": None}
    assert chamou == []


def test_ligado_com_id_inexistente_da_404(duas_empresas):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    assert _app_de_teste().get("/x/nao-existe").status_code == 404


def _app_que_reporta_state():
    """App mínimo que expõe se a dependency encostou em `request.state`.

    App separado de `_app_de_teste` de propósito: o teste de inércia lá asserta
    o corpo inteiro por igualdade, e mexer na forma da resposta dele apagaria a
    garantia de que ele não mudou.
    """
    from app.web.dependencies.pedido import env_do_pedido

    app = FastAPI()

    @app.get("/y/{import_id}")
    def rota(import_id: str, request: Request, env=Depends(env_do_pedido)):  # noqa: ARG001
        return {
            "tem_state": hasattr(request.state, "environment"),
            "state": (getattr(request.state, "environment", None) or {}).get("slug"),
        }

    return TestClient(app)


def test_ligado_ativa_as_duas_metades_contextvar_e_request_state(duas_empresas):
    """O contextvar resolve o SQLite; `request.state.environment` resolve o
    Firebird, a pasta de saida e o slug do Flow. Ativar so' uma amarrava o
    pedido a duas empresas ao mesmo tempo."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = _app_que_reporta_state().get("/y/N1")
    assert r.json() == {"tem_state": True, "state": "nasmar"}


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_fora_de_ligado_nao_encosta_em_request_state(duas_empresas, modo):
    """Nem para escrever, nem para criar o atributo com `None`."""
    _grava("nasmar", "N1")
    roteamento_repo.set_modo(modo, por="teste")
    r = _app_que_reporta_state().get("/y/N1")
    assert r.json() == {"tem_state": False, "state": None}


def test_a_dependency_e_async(duas_empresas):
    """Dependency sincrona com yield + contextvar estoura no FastAPI com
    'Token was created in a different Context'. Este teste falha se alguem
    trocar o `async def` por `def`."""
    import inspect

    from app.web.dependencies.pedido import env_do_pedido

    assert inspect.isasyncgenfunction(env_do_pedido), (
        "env_do_pedido tem que ser `async def` com yield — a versao sincrona "
        "roda em threadpool e o reset do contextvar estoura"
    )
