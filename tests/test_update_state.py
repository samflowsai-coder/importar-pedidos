import pytest

from app.updates import state


def test_status_default_idle(tmp_path):
    assert state.read_status(tmp_path)["status"] == "idle"


def test_write_merge_e_le(tmp_path):
    state.write_status(tmp_path, status="staged", update_id="u1")
    state.write_status(tmp_path, phase="backup")
    s = state.read_status(tmp_path)
    assert s["status"] == "staged" and s["update_id"] == "u1" and s["phase"] == "backup"


def test_lock_e_idade(tmp_path):
    assert state.is_locked(tmp_path) is False
    state.lock_path(tmp_path).write_text("x")
    assert state.is_locked(tmp_path) is True
    # idade calculada contra now_ts injetado
    import os

    mtime = os.path.getmtime(state.lock_path(tmp_path))
    assert state.lock_age_seconds(tmp_path, mtime + 120) == pytest.approx(120, abs=2)


def test_history_append(tmp_path):
    state.append_history(tmp_path, {"update_id": "u1", "result": "succeeded"})
    state.append_history(tmp_path, {"update_id": "u2", "result": "rolled_back"})
    lines = (tmp_path / "history.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2 and '"u2"' in lines[1]
