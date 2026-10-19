import pytest

import main as main_mod


@pytest.mark.parametrize("modo", ["db", "both"])
def test_cli_recusa_modo_db(monkeypatch, modo):
    from app import config as app_config

    monkeypatch.setattr(
        app_config, "load", lambda: {"export_mode": modo, "watch_dir": ".", "output_dir": "."}
    )

    def _explode(*a, **kw):
        raise AssertionError("nenhum arquivo pode ser processado")

    monkeypatch.setattr(main_mod.FileLoader, "load_files", _explode)
    monkeypatch.setattr(main_mod, "process", _explode)

    with pytest.raises(SystemExit) as exc:
        main_mod.main()

    assert exc.value.code not in (0, None)
    assert "Gravação direta no Fire só pelo portal (preview → enviar)." in str(exc.value.code)
