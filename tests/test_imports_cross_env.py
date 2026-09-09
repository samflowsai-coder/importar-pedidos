# tests/test_imports_cross_env.py
from __future__ import annotations

import pytest

from app.persistence import context as env_context
from app.persistence import environments_repo, repo, router


@pytest.fixture
def dois_ambientes_com_pedidos(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("nasmar", "Nasmar"), ("mm", "MM Americanense")):
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug),
            output_dir=str(tmp_path / slug),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )

    def _inserir(slug, ident, quando, cliente):
        env = environments_repo.get_by_slug(slug)
        with env_context.active_env(env["id"], env["slug"]):
            repo.insert_import(
                {
                    "id": ident,
                    "source_filename": f"{ident}.pdf",
                    "imported_at": quando,
                    "order_number": ident,
                    "customer_name": cliente,
                    "status": "success",
                    "portal_status": "parsed",
                }
            )

    _inserir("nasmar", "N1", "2026-09-01T10:00:00", "DAJU")
    _inserir("mm", "M1", "2026-09-02T10:00:00", "CENTAURO")
    _inserir("nasmar", "N2", "2026-09-03T10:00:00", "STUDIO Z")
    yield


def test_lista_junta_os_ambientes_em_ordem_de_data(dois_ambientes_com_pedidos):
    linhas = repo.list_imports_all_envs(limit=10)
    assert [r["id"] for r in linhas] == ["N2", "M1", "N1"]


def test_cada_linha_carrega_o_selo_da_empresa(dois_ambientes_com_pedidos):
    por_id = {r["id"]: r for r in repo.list_imports_all_envs(limit=10)}
    assert por_id["N1"]["env_slug"] == "nasmar"
    assert por_id["N1"]["env_name"] == "Nasmar"
    assert por_id["M1"]["env_name"] == "MM Americanense"


def test_paginacao_atravessa_a_fronteira_dos_ambientes(dois_ambientes_com_pedidos):
    assert [r["id"] for r in repo.list_imports_all_envs(limit=2)] == ["N2", "M1"]
    assert [r["id"] for r in repo.list_imports_all_envs(limit=2, offset=2)] == ["N1"]


def test_filtro_vale_para_todos_os_ambientes(dois_ambientes_com_pedidos):
    linhas = repo.list_imports_all_envs(limit=10, customer_search="DAJU")
    assert [r["id"] for r in linhas] == ["N1"]


def test_contagem_soma_os_ambientes(dois_ambientes_com_pedidos):
    assert repo.count_imports_all_envs() == 3
    assert repo.count_imports_all_envs(customer_search="DAJU") == 1


def test_chips_somam_os_ambientes_junto_com_a_lista(dois_ambientes_com_pedidos):
    """Chip que discorda da lista e pior que chip nenhum."""
    assert repo.count_by_portal_status_all_envs() == {"parsed": 3}


def test_sem_ambiente_nenhum_devolve_lista_vazia(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    router.reset_init_cache()
    with router.shared_connect():
        pass
    assert repo.list_imports_all_envs() == []
    assert repo.count_imports_all_envs() == 0
