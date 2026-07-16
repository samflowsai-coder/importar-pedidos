import json
from concurrent.futures import ThreadPoolExecutor

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


def test_write_status_concorrente_nao_levanta(tmp_path):
    """N threads escrevendo status ao mesmo tempo não podem levantar
    (regressão: tmp fixo + .replace() colidindo entre threads -> FileNotFoundError)."""
    n_threads = 8
    n_calls = 50

    def _worker(thread_id: int) -> None:
        for i in range(n_calls):
            state.write_status(
                tmp_path,
                status="running",
                thread_id=thread_id,
                call=i,
            )

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [pool.submit(_worker, t) for t in range(n_threads)]
        for f in futures:
            f.result()  # propaga qualquer exceção da thread

    final = state.read_status(tmp_path)
    assert isinstance(final, dict)
    assert final["status"] == "running"


def test_read_status_json_corrompido_retorna_idle(tmp_path):
    (tmp_path / "status.json").write_text("{isso nao e json valido", encoding="utf-8")
    assert state.read_status(tmp_path) == {"status": "idle"}


def test_read_status_json_valido_nao_dict_retorna_idle(tmp_path):
    (tmp_path / "status.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert state.read_status(tmp_path) == {"status": "idle"}
