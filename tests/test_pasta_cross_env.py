"""A aba de arquivos esperando, somada entre empresas.

Aqui a empresa e DECLARADA, nao derivada: `import_id` e unico globalmente,
nome de arquivo nao e.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.persistence import environments_repo, roteamento_repo, router
from app.web.server import app


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TEST_AUTH_BYPASS", "1")
    router.reset_init_cache()
    with router.shared_connect():
        pass
    for slug, nome in (("mm", "MM Americanense"), ("nasmar", "Nasmar")):
        (tmp_path / slug / "in").mkdir(parents=True)
        (tmp_path / slug / "out").mkdir(parents=True)
        environments_repo.create(
            slug=slug,
            name=nome,
            watch_dir=str(tmp_path / slug / "in"),
            output_dir=str(tmp_path / slug / "out"),
            fb_path=str(tmp_path / f"{slug}.fdb"),
        )
    (tmp_path / "mm" / "in" / "PEDIDO.pdf").write_bytes(b"%PDF-1.4 mm")
    (tmp_path / "nasmar" / "in" / "PEDIDO.pdf").write_bytes(b"%PDF-1.4 nasmar")
    yield TestClient(app)


def test_ligado_pending_soma_as_pastas_com_selo(portal):
    """Mesmo NOME de arquivo nas duas pastas: o selo e o que os distingue."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.get("/api/pending")
    assert r.status_code == 200
    files = r.json()["files"]
    assert len(files) == 2
    assert {f["env_slug"] for f in files} == {"mm", "nasmar"}
    assert {f["env_name"] for f in files} == {"MM Americanense", "Nasmar"}
    assert {f["name"] for f in files} == {"PEDIDO.pdf"}


@pytest.mark.parametrize("modo", [roteamento_repo.DESLIGADO, roteamento_repo.OBSERVANDO])
def test_fora_de_ligado_pending_e_o_de_hoje(portal, modo):
    roteamento_repo.set_modo(modo, por="teste")
    portal.cookies.set("portal_env", environments_repo.get_by_slug("mm")["id"])
    files = portal.get("/api/pending").json()["files"]
    assert len(files) == 1
    assert "env_slug" not in files[0]


def test_ligado_sem_env_slug_recusa_com_400(portal):
    """Nome de arquivo nao e unico entre empresas: sem o slug nao da pra saber."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post("/api/preview-pending", json={"filename": "PEDIDO.pdf"})
    assert r.status_code == 400
    assert "env_slug" in str(r.json()["detail"])


def test_ligado_env_slug_invalido_da_404(portal):
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post(
        "/api/preview-pending",
        json={"filename": "PEDIDO.pdf", "env_slug": "fantasma"},
    )
    assert r.status_code == 404


def test_ligado_import_sem_env_slug_recusa_com_400(portal):
    """A mesma trava do preview-pending vale pro import direto."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post("/api/import", json={"files": ["PEDIDO.pdf"]})
    assert r.status_code == 400
    assert "env_slug" in str(r.json()["detail"])


def test_ligado_reimport_sem_env_slug_recusa_com_400(portal):
    """E pro reimport — a resolução acontece antes de qualquer I/O de disco."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post("/api/reimport", json={"filename": "PEDIDO.pdf"})
    assert r.status_code == 400
    assert "env_slug" in str(r.json()["detail"])


def test_ligado_import_declarado_le_a_pasta_certa(portal, tmp_path):
    """A prova de que 'importar o segundo da lista' importa o SEGUNDO: o
    conteúdo guardado em `recebidos/` (gravado ANTES do parse, ver
    `_guardar_original`) é o byte da pasta da Nasmar, nunca o da MM — mesmo
    as duas pastas tendo um arquivo `PEDIDO.pdf` idêntico no nome."""
    roteamento_repo.set_modo(roteamento_repo.LIGADO, por="teste")
    r = portal.post("/api/import", json={"files": ["PEDIDO.pdf"], "env_slug": "nasmar"})
    assert r.status_code == 200, r.text

    copias = list((tmp_path / "recebidos" / "nasmar").rglob("*.pdf"))
    assert len(copias) == 1
    assert copias[0].read_bytes() == b"%PDF-1.4 nasmar"
    # a pasta da OUTRA empresa nunca foi tocada
    assert not (tmp_path / "recebidos" / "mm").exists()
    assert (tmp_path / "mm" / "in" / "PEDIDO.pdf").read_bytes() == b"%PDF-1.4 mm"
