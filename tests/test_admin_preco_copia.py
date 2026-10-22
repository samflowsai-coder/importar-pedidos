"""GET /api/admin/environments/{id}/preco-trava/copia: o que a cópia da tabela de preço tem.

Só leitura. Serve para provar, sem o token do Flow, que a cópia sincronizou e por qual vigência
um pedido resolve.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.persistence import db, environments_repo, router
from app.security import secret_store

MM = "35394871000111"
NASMAR = "34513679000134"


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(secret_store, "_KEY_FILE", tmp_path / ".secret.key")
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    e = environments_repo.create(
        slug="mm",
        name="MM",
        watch_dir=str(tmp_path / "w"),
        output_dir=str(tmp_path / "o"),
        fb_path="",
    )
    yield e
    db.set_db_path(None)
    db.reset_init_cache()


def _preco(
    conn,
    pid,
    produto,
    vig,
    centavos,
    *,
    empresa=MM,
    rede=None,
    cancelado=0,
    reg="2026-10-21T14:00:00Z",
):
    conn.execute(
        "INSERT INTO preco_tabela_v1 (preco_id, rede_id, empresa_cnpj, erp_produto_id, unidade_erp,"
        " preco_centavos, vigencia_inicio, registrado_em, cancelado) VALUES (?,?,?,?,?,?,?,?,?)",
        (pid, rede, empresa, produto, "KIT", centavos, vig, reg, cancelado),
    )


def _client():
    from app.web.server import app

    return TestClient(app)


def _popular():
    with router.env_connect("mm") as conn:
        conn.execute(
            "INSERT INTO preco_sync_v1 (id, empresa_cnpj, cursor, completo_em) VALUES (1,?,?,?)",
            (MM, "c", "2026-10-22T16:33:29.615000+00:00"),
        )
        conn.execute("INSERT INTO preco_rede_v1 VALUES ('r1','Artwalk',1,0)")
        _preco(conn, "a", "3914", "2026-08-01", 1095)
        _preco(conn, "b", "3914", "2026-10-19", 1095, reg="2026-10-19T10:00:00Z")
        _preco(conn, "c", "3915", "2026-08-01", 1200)
        _preco(conn, "d", "3915", "2026-10-19", 1300, cancelado=1)
        _preco(conn, "e", "3914", "2026-08-01", 900, empresa=NASMAR)


def test_conta_precos_por_vigencia_e_empresa(env):
    _popular()
    corpo = _client().get(f"/api/admin/environments/{env['id']}/preco-trava/copia").json()
    assert corpo["sincronizada_em"].startswith("2026-10-22T16:33")
    mm = next(e for e in corpo["empresas"] if e["empresa_cnpj"] == MM)
    assert mm["precos"] == 4 and mm["ativos"] == 3 and mm["cancelados"] == 1
    assert mm["por_vigencia"] == {"2026-08-01": 2, "2026-10-19": 1}
    assert mm["produtos"] == 2
    assert corpo["redes"] == 1
    nas = next(e for e in corpo["empresas"] if e["empresa_cnpj"] == NASMAR)
    assert nas["ativos"] == 1


def test_resolve_pela_vigencia_da_data_de_emissao(env):
    _popular()
    base = f"/api/admin/environments/{env['id']}/preco-trava/copia"
    antes = _client().get(base, params={"produto": "3915", "data": "2026-09-15"}).json()
    mm = next(r for r in antes["resolucao"] if r["empresa_cnpj"] == MM)
    # 15/09 já resolve pela vigência retroativa de 01/08; a de 19/10 está cancelada.
    assert (mm["preco_centavos"], mm["vigencia_inicio"]) == (1200, "2026-08-01")
    antes_vig = _client().get(base, params={"produto": "3914", "data": "2026-07-31"}).json()
    assert (
        next(r for r in antes_vig["resolucao"] if r["empresa_cnpj"] == MM)["preco_centavos"] is None
    )


def test_sem_cache_vazio_nao_quebra(env):
    corpo = _client().get(f"/api/admin/environments/{env['id']}/preco-trava/copia").json()
    assert corpo["sincronizada_em"] is None and corpo["empresas"] == []


def test_ambiente_inexistente_404(env):
    assert _client().get("/api/admin/environments/nao-existe/preco-trava/copia").status_code == 404


def test_data_invalida_422(env):
    r = _client().get(
        f"/api/admin/environments/{env['id']}/preco-trava/copia",
        params={"produto": "1", "data": "ontem"},
    )
    assert r.status_code == 422
