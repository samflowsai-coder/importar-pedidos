# app/updates/package.py
from __future__ import annotations

import hashlib
import json
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = "portal-pedidos"
ALLOWED_TOP = {
    "app",
    "scripts",
    "tools",
    "ui.py",
    "main.py",
    "pyproject.toml",
    ".env.example",
    "README.md",
    "INSTALACAO-SERVIDOR.md",
    "manifest.json",
}
DENY_SUFFIX = (".db", ".sqlite", ".sqlite3", ".fdb", ".fbk", ".gbk")
DENY_NAME = {".env", ".secret.key", "config.json", "firebird.json"}
MAX_MEMBERS = 10_000
MAX_UNCOMPRESSED = 500 * 1024 * 1024


class PackageError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def compute_deps_sha256(pyproject_path: Path) -> str:
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
    proj = data.get("project", {})
    deps = list(proj.get("dependencies", []))
    for v in proj.get("optional-dependencies", {}).values():
        deps += list(v)
    norm = "\n".join(sorted(d.strip() for d in deps if d.strip()))
    return hashlib.sha256(norm.encode()).hexdigest()


@dataclass(frozen=True)
class StagedPackage:
    update_id: str
    version: str
    git_commit: str
    built_at: str
    files_count: int
    deps_changed: bool


def _member_ok(name: str) -> None:
    if not name.startswith(ROOT + "/"):
        raise PackageError(f"membro fora da raiz do pacote: {name}")
    rel = name[len(ROOT) + 1 :]
    if rel == "" or rel.endswith("/"):
        return  # diretório
    if ".." in Path(rel).parts or Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        raise PackageError(f"caminho inseguro: {name}")
    base = Path(rel).name
    if base in DENY_NAME or base.endswith(DENY_SUFFIX) or rel.split("/")[0] == "data":
        raise PackageError(f"membro proibido (segredo/dado): {name}")
    top = rel.split("/")[0]
    if not (top in ALLOWED_TOP or top.endswith(".bat")):
        raise PackageError(f"membro fora da allowlist: {name}")


def validate_and_stage(
    zip_path: Path, staging_root: Path, local_pyproject: Path, *, update_id: str
) -> StagedPackage:
    if not zipfile.is_zipfile(zip_path):
        raise PackageError("arquivo não é um zip válido")
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            raise PackageError("zip corrompido")
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise PackageError("pacote com membros demais")
        total = sum(i.file_size for i in infos)
        if total > MAX_UNCOMPRESSED:
            raise PackageError("pacote descomprimido excede o limite")
        for i in infos:
            if (i.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
                raise PackageError(f"symlink não permitido: {i.filename}")
            _member_ok(i.filename)
        try:
            manifest = json.loads(z.read(f"{ROOT}/manifest.json"))
        except KeyError:
            raise PackageError("manifest.json ausente") from None
        except Exception:
            raise PackageError("manifest.json inválido") from None
        if manifest.get("name") != "portal-pedidos":
            raise PackageError("manifest.json: name divergente")
        for k in ("version", "built_at", "git_commit", "deps_sha256"):
            if not manifest.get(k):
                raise PackageError(f"manifest.json: campo {k} ausente")
        dest = staging_root / update_id
        if dest.exists():
            import shutil

            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        for i in infos:
            target = (dest / i.filename).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise PackageError(f"extração fora do staging: {i.filename}")
            z.extract(i, dest)
        files = sum(1 for i in infos if not i.filename.endswith("/"))
    local_sha = compute_deps_sha256(local_pyproject)
    return StagedPackage(
        update_id=update_id,
        version=manifest["version"],
        git_commit=manifest["git_commit"],
        built_at=manifest["built_at"],
        files_count=files,
        deps_changed=(manifest["deps_sha256"] != local_sha),
    )
