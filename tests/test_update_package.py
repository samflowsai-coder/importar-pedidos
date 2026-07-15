# tests/test_update_package.py
import json
import zipfile as zf
from pathlib import Path

import pytest

from app.updates import package as pkg


def _pyproject(tmp_path, deps: list[str]) -> Path:
    body = 'requires-python = ">=3.11"\n'
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "pyproject.toml"
    p.write_text(
        f'[project]\nname="x"\nversion="0.1.0"\n{body}'
        f"dependencies = [{', '.join(repr(d) for d in deps)}]\n",
        encoding="utf-8",
    )
    return p


def test_deps_sha_ignora_ordem_e_espacos(tmp_path):
    a = pkg.compute_deps_sha256(_pyproject(tmp_path / "a", ["fastapi", "  pydantic "]))
    (tmp_path / "a").rename(tmp_path / "a2")  # noqa
    b = pkg.compute_deps_sha256(_pyproject(tmp_path / "b", ["pydantic", "fastapi"]))
    assert a == b


def test_deps_sha_muda_com_nova_dep(tmp_path):
    a = pkg.compute_deps_sha256(_pyproject(tmp_path / "a", ["fastapi"]))
    b = pkg.compute_deps_sha256(_pyproject(tmp_path / "b", ["fastapi", "loguru"]))
    assert a != b


def _make_zip(tmp_path, members: dict[str, bytes], name="pkg.zip") -> Path:
    p = tmp_path / name
    with zf.ZipFile(p, "w") as z:
        for arc, data in members.items():
            z.writestr(arc, data)
    return p


def _valid_manifest(deps_sha="abc") -> bytes:
    return json.dumps(
        {
            "name": "portal-pedidos",
            "version": "20260714-1030",
            "built_at": "2026-07-14T10:30:00Z",
            "git_commit": "deadbee",
            "deps_sha256": deps_sha,
        }
    ).encode()


def _base_members(deps_sha="abc") -> dict[str, bytes]:
    return {
        "portal-pedidos/manifest.json": _valid_manifest(deps_sha),
        "portal-pedidos/pyproject.toml": b'[project]\nname="x"\nversion="0.1.0"\ndependencies=["fastapi"]\n',
        "portal-pedidos/app/__init__.py": b"",
        "portal-pedidos/ui.py": b"# ui\n",
    }


def test_manifesto_ausente_rejeita(tmp_path):
    m = _base_members()
    del m["portal-pedidos/manifest.json"]
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_name_divergente_rejeita(tmp_path):
    m = _base_members()
    m["portal-pedidos/manifest.json"] = json.dumps({"name": "outro"}).encode()
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


@pytest.mark.parametrize(
    "bad",
    [
        "portal-pedidos/../evil.py",
        "/etc/passwd",
        "portal-pedidos/../../x",
        "C:\\\\windows\\\\x",
        "portal-pedidos/data/app_shared.db",
        "portal-pedidos/app/.secret.key",
        "portal-pedidos/.env",
        "portal-pedidos/x.db",
        "portal-pedidos/naoexiste_na_allowlist.txt",
        # Achado 1 — deny-list precisa ser case-insensitive (NTFS é case-insensitive)
        "portal-pedidos/app/firebird.JSON",
        "portal-pedidos/app/CONFIG.JSON",
        "portal-pedidos/app/backup.DB",
        "portal-pedidos/app/.SECRET.KEY",
        "portal-pedidos/Data/x.txt",
        # Achado 2 — traversal não pode depender do OS do host (backslash é separador
        # em NTFS mesmo rodando o parser em POSIX)
        r"portal-pedidos/app/evil\..\..\pwned.txt",
        r"portal-pedidos/app/C:\evil.exe",
    ],
)
def test_membros_proibidos_rejeitam(tmp_path, bad):
    m = _base_members()
    m[bad] = b"x"
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_arquivo_nao_zip_rejeita(tmp_path):
    naozip = tmp_path / "pacote.zip"
    naozip.write_bytes(b"isto nao e um zip valido")
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(
            naozip,
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_manifesto_sem_built_at_rejeita(tmp_path):
    m = _base_members()
    manifest = json.loads(m["portal-pedidos/manifest.json"])
    del manifest["built_at"]
    m["portal-pedidos/manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_pacote_valido_extrai_e_reporta(tmp_path):
    sha = pkg.compute_deps_sha256(_pyproject(tmp_path / "pp", ["fastapi"]))
    m = _base_members(deps_sha=sha)
    st = tmp_path / "st"
    res = pkg.validate_and_stage(
        _make_zip(tmp_path, m), st, _pyproject(tmp_path / "pp2", ["fastapi"]), update_id="u1"
    )
    assert res.version == "20260714-1030"
    assert res.deps_changed is False  # mesmo sha
    assert (st / "u1" / "portal-pedidos" / "ui.py").read_bytes() == b"# ui\n"


def test_deps_changed_true_quando_hash_difere(tmp_path):
    m = _base_members(deps_sha="hash-antigo-diferente")
    res = pkg.validate_and_stage(
        _make_zip(tmp_path, m),
        tmp_path / "st",
        _pyproject(tmp_path / "pp", ["fastapi"]),
        update_id="u1",
    )
    assert res.deps_changed is True


# ── D4: cap anti-zip-bomb tem que ser checado ANTES de testzip() ───────────
#
# `testzip()` descomprime TODOS os membros do zip para verificar o CRC. Um
# zip-bomb (tamanho declarado enorme) travaria um core no request síncrono se
# essa descompressão total rodasse antes de qualquer cap. `MAX_MEMBERS` e
# `MAX_UNCOMPRESSED` usam só o diretório central (`infolist()`, sem
# descomprimir) e por isso têm que ser checados primeiro.


def test_max_members_excedido_rejeita(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg, "MAX_MEMBERS", 2)
    m = {"a.txt": b"1", "b.txt": b"2", "c.txt": b"3"}
    with pytest.raises(pkg.PackageError, match="membros demais"):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_max_uncompressed_excedido_rejeita(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg, "MAX_UNCOMPRESSED", 10)
    m = {"portal-pedidos/x.txt": b"x" * 20}
    with pytest.raises(pkg.PackageError, match="excede o limite"):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )


def test_cap_de_membros_dispara_sem_chamar_testzip(tmp_path, monkeypatch):
    """Prova a ORDEM: se testzip() rodasse antes do cap, ele executaria e
    (aqui) estouraria o AssertionError abaixo em vez do PackageError
    esperado — o teste travaria a regressão da reordenação do D4."""
    monkeypatch.setattr(pkg, "MAX_MEMBERS", 1)

    def _boom(self):
        raise AssertionError("testzip() não deveria rodar antes do cap de membros")

    monkeypatch.setattr(zf.ZipFile, "testzip", _boom)
    m = {"a.txt": b"1", "b.txt": b"2"}
    with pytest.raises(pkg.PackageError, match="membros demais"):
        pkg.validate_and_stage(
            _make_zip(tmp_path, m),
            tmp_path / "st",
            _pyproject(tmp_path / "pp", ["fastapi"]),
            update_id="u1",
        )
