"""Tests for app.worker.jobs.drain_outbox."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.persistence.outbox_repo import OutboxRow


@pytest.fixture(autouse=True)
def _seed_env(tmp_path: Path):
    """Cria um ambiente "test" para que list_env_slugs() retorne 'test'."""
    import os
    from app.persistence import db, environments_repo
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


def _make_row(*, attempts: int = 0, trace_id: str | None = None) -> OutboxRow:
    return OutboxRow(
        id=1,
        import_id="imp-001",
        target="gestor",
        endpoint="/v1/orders",
        payload={"external_id": "imp-001", "items": []},
        idempotency_key="idem-key-001",
        status="pending",
        attempts=attempts,
        next_attempt_at=None,
        last_error=None,
        response=None,
        trace_id=trace_id,
        created_at=datetime.now().isoformat(),
        sent_at=None,
    )


@patch("app.worker.jobs.drain_outbox.outbox_repo")
@patch("app.worker.jobs.drain_outbox.repo")
@patch("app.worker.jobs.drain_outbox.GestorClient")
@patch("app.worker.jobs.drain_outbox.transition")
def test_happy_path_marks_sent(mock_transition, mock_client, mock_repo, mock_outbox):
    row = _make_row()
    mock_outbox.claim_next.side_effect = [row, None]
    mock_resp = MagicMock()
    mock_resp.id = "gestor-abc"
    mock_resp.model_dump.return_value = {"id": "gestor-abc"}
    mock_client.return_value.create_order.return_value = mock_resp

    from app.worker.jobs.drain_outbox import run_drain_outbox
    run_drain_outbox()

    mock_outbox.mark_sent.assert_called_once_with(row.id, response={"id": "gestor-abc"})
    mock_repo.set_gestor_order_id.assert_called_once_with("imp-001", "gestor-abc")
    mock_transition.assert_called_once()
    args, _ = mock_transition.call_args
    assert args[0] == "imp-001"
    from app.state.machine import LifecycleEvent
    assert args[1] == LifecycleEvent.POST_TO_GESTOR_SENT


@patch("app.worker.jobs.drain_outbox.outbox_repo")
@patch("app.worker.jobs.drain_outbox.GestorClient")
def test_first_failure_schedules_retry_in_30s(mock_client, mock_outbox):
    row = _make_row(attempts=0)
    mock_outbox.claim_next.side_effect = [row, None]
    mock_client.return_value.create_order.side_effect = Exception("timeout")

    from app.worker.jobs.drain_outbox import run_drain_outbox
    run_drain_outbox()

    mock_outbox.mark_failed.assert_called_once()
    _, kwargs = mock_outbox.mark_failed.call_args
    assert kwargs.get("dead") is None or kwargs.get("dead") is False
    assert kwargs["next_attempt_at"] is not None
    next_dt = datetime.fromisoformat(kwargs["next_attempt_at"])
    delta = (next_dt - datetime.now(UTC)).total_seconds()
    assert 25 <= delta <= 35


@patch("app.worker.jobs.drain_outbox.outbox_repo")
@patch("app.worker.jobs.drain_outbox.append_event")
@patch("app.worker.jobs.drain_outbox.GestorClient")
def test_exhausted_retries_marks_dead(mock_client, mock_append, mock_outbox):
    row = _make_row(attempts=5)
    mock_outbox.claim_next.side_effect = [row, None]
    mock_client.return_value.create_order.side_effect = Exception("permanent failure")

    from app.worker.jobs.drain_outbox import run_drain_outbox
    run_drain_outbox()

    mock_outbox.mark_failed.assert_called_once()
    _, kwargs = mock_outbox.mark_failed.call_args
    assert kwargs["dead"] is True

    mock_append.assert_called_once()
    from app.state.machine import LifecycleEvent
    assert mock_append.call_args[0][1] == LifecycleEvent.POST_TO_GESTOR_FAILED


@patch("app.worker.jobs.drain_outbox.outbox_repo")
def test_no_rows_exits_immediately(mock_outbox):
    mock_outbox.claim_next.return_value = None

    from app.worker.jobs.drain_outbox import run_drain_outbox
    run_drain_outbox()

    mock_outbox.claim_next.assert_called_once()
    mock_outbox.mark_sent.assert_not_called()
    mock_outbox.mark_failed.assert_not_called()


@patch("app.worker.jobs.drain_outbox.outbox_repo")
@patch("app.worker.jobs.drain_outbox.repo")
@patch("app.worker.jobs.drain_outbox.GestorClient")
@patch("app.worker.jobs.drain_outbox.transition")
def test_batch_cap_stops_at_20(mock_transition, mock_client, mock_repo, mock_outbox):
    rows = [_make_row(attempts=0) for _ in range(25)]
    mock_outbox.claim_next.side_effect = rows
    mock_resp = MagicMock()
    mock_resp.id = "g-1"
    mock_resp.model_dump.return_value = {"id": "g-1"}
    mock_client.return_value.create_order.return_value = mock_resp

    from app.worker.jobs.drain_outbox import run_drain_outbox
    run_drain_outbox()

    assert mock_outbox.claim_next.call_count == 20
    assert mock_outbox.mark_sent.call_count == 20


@patch("app.worker.jobs.drain_outbox.outbox_repo")
@patch("app.worker.jobs.drain_outbox.GestorClient")
def test_backoff_progression(mock_client, mock_outbox):
    """Each attempt index maps to the correct delay."""
    from app.worker.jobs.drain_outbox import _BACKOFF_S

    for idx, expected_delta in enumerate(_BACKOFF_S):
        row = _make_row(attempts=idx)
        mock_outbox.claim_next.side_effect = [row, None]
        mock_client.return_value.create_order.side_effect = Exception("err")
        mock_outbox.reset_mock()

        from app.worker.jobs.drain_outbox import run_drain_outbox
        run_drain_outbox()

        _, kwargs = mock_outbox.mark_failed.call_args
        next_dt = datetime.fromisoformat(kwargs["next_attempt_at"])
        delta = (next_dt - datetime.now(UTC)).total_seconds()
        assert expected_delta - 5 <= delta <= expected_delta + 5, (
            f"attempts={idx}: expected ~{expected_delta}s, got {delta:.0f}s"
        )
