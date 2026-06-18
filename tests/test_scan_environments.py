"""Watcher multi-pasta: scan_environments."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from app.persistence import db, environments_repo, router
from app.worker.jobs import scan_environments


@pytest.fixture
def two_envs(tmp_path: Path):
    import os
    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    mm = environments_repo.create(
        slug="mm", name="MM",
        watch_dir=str(tmp_path / "mm-in"),
        output_dir=str(tmp_path / "mm-out"),
        fb_path=str(tmp_path / "mm.fdb"),
    )
    nm = environments_repo.create(
        slug="nasmar", name="Nasmar",
        watch_dir=str(tmp_path / "nm-in"),
        output_dir=str(tmp_path / "nm-out"),
        fb_path=str(tmp_path / "nm.fdb"),
    )
    Path(mm["watch_dir"]).mkdir()
    Path(nm["watch_dir"]).mkdir()
    yield mm, nm
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _fake_order(num="PED-001"):
    """Cria um Order minimal pra mockar o pipeline."""
    from app.models.order import Order, OrderHeader
    return Order(
        header=OrderHeader(order_number=num, customer_name="ACME", customer_cnpj="00000000000100"),
        items=[],
    )


def test_skip_when_watch_dir_missing(two_envs):
    mm, _ = two_envs
    Path(mm["watch_dir"]).rmdir()
    # Não levanta — sem arquivos para processar
    scan_environments.run_scan()


def test_picks_up_new_pdf_and_inserts_import(two_envs):
    mm, _ = two_envs
    pdf = Path(mm["watch_dir"]) / "pedido-001.pdf"
    pdf.write_bytes(b"%PDF-1.4\nfake")

    with patch.object(scan_environments, "pipeline_process", return_value=_fake_order()):
        scan_environments.run_scan()

    # arquivo movido pra Pedidos importados
    moved = Path(mm["watch_dir"]) / "Pedidos importados" / "pedido-001.pdf"
    assert moved.exists()
    assert not pdf.exists()

    # row inserida na DB do MM
    with router.env_connect("mm") as conn:
        rows = conn.execute("SELECT id, environment_id, status FROM imports").fetchall()
    assert len(rows) == 1
    assert rows[0]["environment_id"] == mm["id"]
    assert rows[0]["status"] == "success"


def test_idempotent_by_sha(two_envs):
    mm, _ = two_envs
    content = b"%PDF-1.4\nidempotent"
    (Path(mm["watch_dir"]) / "a.pdf").write_bytes(content)

    with patch.object(scan_environments, "pipeline_process", return_value=_fake_order("X")):
        scan_environments.run_scan()

    # mesmo conteúdo recolocado com nome diferente
    (Path(mm["watch_dir"]) / "b.pdf").write_bytes(content)
    with patch.object(scan_environments, "pipeline_process", return_value=_fake_order("X")) as m:
        scan_environments.run_scan()
        # pipeline NÃO foi chamado pra "b.pdf" (sha já existe)
        assert m.call_count == 0

    # apenas 1 row no DB
    with router.env_connect("mm") as conn:
        count = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    assert count == 1


def test_iterates_multiple_envs(two_envs):
    mm, nm = two_envs
    (Path(mm["watch_dir"]) / "mm.pdf").write_bytes(b"AAA")
    (Path(nm["watch_dir"]) / "nm.pdf").write_bytes(b"BBB")

    with patch.object(scan_environments, "pipeline_process",
                      side_effect=[_fake_order("MM-1"), _fake_order("NM-1")]):
        scan_environments.run_scan()

    with router.env_connect("mm") as conn:
        mm_rows = conn.execute(
            "SELECT environment_id, source_filename FROM imports"
        ).fetchall()
    with router.env_connect("nasmar") as conn:
        nm_rows = conn.execute(
            "SELECT environment_id, source_filename FROM imports"
        ).fetchall()

    assert len(mm_rows) == 1 and mm_rows[0]["source_filename"] == "mm.pdf"
    assert len(nm_rows) == 1 and nm_rows[0]["source_filename"] == "nm.pdf"
    # cada um na sua DB com seu environment_id
    assert mm_rows[0]["environment_id"] == mm["id"]
    assert nm_rows[0]["environment_id"] == nm["id"]


def test_pipeline_failure_records_error(two_envs):
    mm, _ = two_envs
    (Path(mm["watch_dir"]) / "broken.pdf").write_bytes(b"junk")

    with patch.object(scan_environments, "pipeline_process", return_value=None):
        scan_environments.run_scan()

    # arquivo movido para com_erro
    err_dir = Path(mm["watch_dir"]) / "Pedidos importados" / "com_erro"
    assert err_dir.is_dir()
    assert (err_dir / "broken.pdf").exists()

    # import com status=error registrado
    with router.env_connect("mm") as conn:
        row = conn.execute(
            "SELECT status, error FROM imports WHERE source_filename = ?",
            ("broken.pdf",),
        ).fetchone()
    assert row["status"] == "error"
    assert row["error"]


def test_continues_when_one_env_fails(two_envs, monkeypatch):
    """Erro processando arquivo de um env não interrompe outros envs."""
    mm, nm = two_envs
    (Path(mm["watch_dir"]) / "mm.pdf").write_bytes(b"AAA")
    (Path(nm["watch_dir"]) / "nm.pdf").write_bytes(b"BBB")

    call_count = {"n": 0}

    def fake_process(loaded):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("boom")
        return _fake_order("OK")

    with patch.object(scan_environments, "pipeline_process", side_effect=fake_process):
        scan_environments.run_scan()

    # Pelo menos um foi processado mesmo com erro no outro
    rows_total = 0
    for slug in ("mm", "nasmar"):
        with router.env_connect(slug) as conn:
            rows_total += conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    assert rows_total == 2  # 1 erro + 1 sucesso
