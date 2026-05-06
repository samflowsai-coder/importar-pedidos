"""Tests for app.worker.jobs.poll_fire."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _seed_env(tmp_path: Path):
    """Cria um ambiente "test" no app_shared para que list_env_slugs() retorne 'test'."""
    import os
    from app.persistence import db, environments_repo, router
    os.environ["APP_DATA_DIR"] = str(tmp_path)
    db.set_db_path(tmp_path / "app_state.db")
    db.reset_init_cache()
    db.init()
    environments_repo.create(
        slug="test", name="Test",
        watch_dir=str(tmp_path / "in"),
        output_dir=str(tmp_path / "out"),
        fb_path=str(tmp_path / "x.fdb"),
    )
    yield
    db.set_db_path(None)
    db.reset_init_cache()
    os.environ.pop("APP_DATA_DIR", None)


def _make_entry(
    *,
    import_id: str = "imp-001",
    fire_codigo: int = 42,
    last_seen: str | None = None,
    snapshot_json: str | None = None,
    trace_id: str | None = "trace-abc",
) -> dict:
    if snapshot_json is None:
        snapshot_json = (
            '{"header": {"order_number": "PED-001", "customer_name": "LOJA X",'
            ' "customer_cnpj": "12.345.678/0001-99"}, "items": []}'
        )
    return {
        "id": import_id,
        "fire_codigo": fire_codigo,
        "trace_id": trace_id,
        "snapshot_json": snapshot_json,
        "fire_status_last_seen": last_seen,
        "fire_status_polled_at": None,
    }


def _make_fb_ctx(status: str) -> MagicMock:
    fb_row = MagicMock()
    fb_row.__getitem__ = lambda self, k: status if k == "STATUS" else 42
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.execute.return_value.fetchone.return_value = fb_row
    return ctx


def test_returns_early_when_no_envs_configured(tmp_path):
    """Sem ambientes ativos, run_poll_fire não chama nada."""
    import os
    from app.persistence import db, environments_repo, router
    # remove o env do fixture autouse
    for env in environments_repo.list_active():
        environments_repo.soft_delete(env["id"])

    from app.worker.jobs.poll_fire import run_poll_fire
    with patch("app.worker.jobs.poll_fire.repo") as mock_repo:
        run_poll_fire()
        mock_repo.list_pending_for_fire_poll.assert_not_called()


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
@patch("app.worker.jobs.poll_fire.append_event")
def test_status_change_emits_event(mock_append, mock_repo, mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    entry = _make_entry(last_seen="PEDIDO")
    mock_repo.list_pending_for_fire_poll.return_value = [entry]
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx("LIBERADO")

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": ""}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()

    mock_repo.update_fire_poll_result.assert_called_once()
    assert mock_repo.update_fire_poll_result.call_args[0][1] == "LIBERADO"
    mock_append.assert_called_once()
    from app.state.machine import LifecycleEvent
    assert mock_append.call_args[0][1] == LifecycleEvent.FIRE_STATUS_CHANGED


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
@patch("app.worker.jobs.poll_fire.append_event")
def test_unchanged_status_skips_event(mock_append, mock_repo, mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    entry = _make_entry(last_seen="PEDIDO")
    mock_repo.list_pending_for_fire_poll.return_value = [entry]
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx("PEDIDO")

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": ""}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()

    mock_append.assert_not_called()


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
@patch("app.worker.jobs.poll_fire.outbox_repo")
@patch("app.worker.jobs.poll_fire.transition")
@patch("app.worker.jobs.poll_fire.append_event")
def test_trigger_status_enqueues_to_outbox(
    mock_append, mock_transition, mock_outbox, mock_repo, mock_fb
):
    mock_fb.return_value.is_configured.return_value = True
    entry = _make_entry(last_seen="PEDIDO")
    mock_repo.list_pending_for_fire_poll.return_value = [entry]
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx("LIBERADO")

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": "LIBERADO"}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()

    mock_outbox.enqueue.assert_called_once()
    enqueue_kwargs = mock_outbox.enqueue.call_args[1]
    assert enqueue_kwargs["import_id"] == "imp-001"
    assert enqueue_kwargs["target"] == "gestor"

    mock_transition.assert_called_once()
    from app.state.machine import LifecycleEvent
    assert mock_transition.call_args[0][1] == LifecycleEvent.POST_TO_GESTOR_REQUESTED


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
@patch("app.worker.jobs.poll_fire.outbox_repo")
@patch("app.worker.jobs.poll_fire.append_event")
def test_empty_trigger_status_never_enqueues(mock_append, mock_outbox, mock_repo, mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    entry = _make_entry(last_seen="PEDIDO")
    mock_repo.list_pending_for_fire_poll.return_value = [entry]
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx("QUALQUER_STATUS")

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": ""}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()

    mock_outbox.enqueue.assert_not_called()


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
@patch("app.worker.jobs.poll_fire.outbox_repo")
@patch("app.worker.jobs.poll_fire.append_event")
def test_outbox_duplicate_is_swallowed(mock_append, mock_outbox, mock_repo, mock_fb):
    from app.persistence.outbox_repo import OutboxDuplicateError

    mock_fb.return_value.is_configured.return_value = True
    entry = _make_entry(last_seen="PEDIDO")
    mock_repo.list_pending_for_fire_poll.return_value = [entry]
    mock_fb.return_value.connect_with_config.return_value = _make_fb_ctx("LIBERADO")
    mock_outbox.enqueue.side_effect = OutboxDuplicateError("dup")

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": "LIBERADO"}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()  # must not raise


@patch("app.worker.jobs.poll_fire.FirebirdConnection")
@patch("app.worker.jobs.poll_fire.repo")
def test_no_pending_entries_skips_firebird(mock_repo, mock_fb):
    mock_fb.return_value.is_configured.return_value = True
    mock_repo.list_pending_for_fire_poll.return_value = []

    with patch("app.worker.jobs.poll_fire.app_config") as mock_cfg:
        mock_cfg.load.return_value = {"fire_trigger_status": ""}
        from app.worker.jobs.poll_fire import run_poll_fire
        run_poll_fire()

    mock_fb.return_value.connect.assert_not_called()
