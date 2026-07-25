# Auto-update pelo portal (upload via VPN) + watchdog — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Permitir que o admin atualize o Portal no servidor do cliente pelo navegador (via VPN, sem RDP): sobe o zip do pacote, o portal valida/backup/aplica/reinicia com rollback automático; um watchdog externo religa o app se ele cair ou pendurar.

**Architecture:** O processo web NUNCA se atualiza — as rotas só validam + enfileiram um staging; quem aplica é um updater out-of-process (Tarefa Agendada one-shot `PortalPedidosUpdater`). Um watchdog (Tarefa Agendada, PowerShell, 1 min, `GET /health`) cobre crash/exit-limpo/processo-pendurado. Spec: `docs/superpowers/specs/2026-07-14-auto-update-endpoint-design.md`.

**Tech Stack:** Python 3.11, FastAPI, pytest + TestClient, `zipfile`/`hashlib`/`pathlib` (stdlib), PowerShell 5.1 (Scheduled Tasks), bash (build).

## Global Constraints

- Python 3.11+ (sintaxe `X | Y`, `match` liberados).
- Preservar SEMPRE no update: `.env`, `data/` (`app_shared.db`, `app_state_<slug>.db`), `app/.secret.key`, `config.json`, `logs/`, `backups/`, `.venv/`. Nunca vêm no pacote; o updater também os pula por deny-list.
- Manifesto obrigatório no pacote: `portal-pedidos/manifest.json` com `{name:"portal-pedidos", version, built_at, git_commit, deps_sha256}`. `name` divergente → rejeita.
- Rotas admin com `Depends(require_admin)`, router `prefix="/api/admin/update"`, padrão de `app/web/routes_environments.py`.
- `MAX_PACKAGE_BYTES = 100 * 1024 * 1024`.
- Novos diretórios sob `data/updates/` (preservado por contrato) e `backups/update/`.
- Ruff limpo + suíte verde antes de cada commit final. Testes direcionados por task.
- PowerShell NÃO roda em CI — tasks de script têm checklist manual no Windows do cliente-teste.

---

## File Structure

- **Novo** `app/updates/__init__.py` — pacote.
- **Novo** `app/updates/package.py` — validação do zip + `deps_sha256` + extração segura para staging. Puro-Python, sem FastAPI.
- **Novo** `app/updates/state.py` — leitura/escrita de `data/updates/status.json`, `update.lock`, `history.jsonl`; helpers de lock/idade.
- **Novo** `app/web/routes_update.py` — `APIRouter` com `upload`/`apply`/`status`.
- **Modificar** `app/web/server.py` — registrar o router + rota de página `GET /admin/atualizacao`.
- **Novo** `app/web/static/admin-atualizacao.html` — UI admin (upload → resumo → apply → progresso).
- **Modificar** `tools/build_package.sh` — gerar `manifest.json` na raiz do stage.
- **Novo** `scripts/apply-update.ps1` — updater out-of-process (backup/stop/apply/pip/start/health/rollback).
- **Novo** `scripts/watchdog.ps1` — health-check + restart.
- **Modificar** `scripts/setup-service.ps1` — registrar 3 tasks; **Modificar** `scripts/uninstall-service.ps1` — remover as 3.
- **Modificar** `scripts/update.ps1` — pip condicional (paridade com o updater).
- **Novo** `tests/test_update_package.py`, `tests/test_update_state.py`, `tests/test_update_routes.py`.

---

## Task 1: manifesto no build (`build_package.sh`)

**Files:**
- Modify: `tools/build_package.sh` (bloco antes do `zip`, após a cópia dos arquivos)
- Test: manual (rodar o build + inspecionar o zip)

**Interfaces:**
- Produces: `portal-pedidos/manifest.json` na raiz do pacote com `{name, version, built_at, git_commit, deps_sha256}`. `version` = stamp `AAAAMMDD-HHMM`. `deps_sha256` = sha256 do bloco `[project].dependencies` normalizado (linhas trimadas, ordenadas, sem vazias) — **mesma normalização** que `app/updates/package.py::compute_deps_sha256` (Task 2). Consumido por `package.py` (compara) e `apply-update.ps1` (lê `deps_changed`).

- [ ] **Step 1: Gerar o manifesto no stage**

Inserir no `tools/build_package.sh`, logo após as cópias (`cp -R app scripts tools ...`) e antes do bloco de limpeza CRLF:

```bash
# ── manifest.json (fonte de versão + hash de deps p/ o auto-update) ──────────
STAMP_HHMM="$(date +%Y%m%d-%H%M)"
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
GIT_COMMIT="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
# deps_sha256: bloco [project].dependencies normalizado (linhas trimadas, ordenadas)
DEPS_SHA="$(python3 - "$ROOT/pyproject.toml" <<'PY'
import sys, hashlib, tomllib
data = tomllib.load(open(sys.argv[1], "rb"))
deps = data.get("project", {}).get("dependencies", [])
opt = data.get("project", {}).get("optional-dependencies", {})
for v in opt.values():
    deps = deps + list(v)
norm = "\n".join(sorted(d.strip() for d in deps if d.strip()))
print(hashlib.sha256(norm.encode()).hexdigest())
PY
)"
cat > "$STAGE/manifest.json" <<JSON
{
  "name": "portal-pedidos",
  "version": "$STAMP_HHMM",
  "built_at": "$BUILT_AT",
  "git_commit": "$GIT_COMMIT",
  "deps_sha256": "$DEPS_SHA"
}
JSON
```

- [ ] **Step 2: Rodar o build e conferir**

Run: `bash tools/build_package.sh && unzip -p dist/portal-pedidos-*.zip portal-pedidos/manifest.json`
Expected: JSON com `name`, `version` (AAAAMMDD-HHMM), `git_commit` (hash curto), `deps_sha256` (64 hex).

- [ ] **Step 3: Commit**

```bash
git add tools/build_package.sh
git commit -m "feat(build): gera manifest.json (versão + deps_sha256) no pacote"
```

---

## Task 2: validação e extração do pacote (`app/updates/package.py`)

**Files:**
- Create: `app/updates/__init__.py` (vazio), `app/updates/package.py`
- Test: `tests/test_update_package.py`

**Interfaces:**
- Produces:
  - `compute_deps_sha256(pyproject_path: Path) -> str` — hash normalizado (idêntico ao build).
  - `PackageError(Exception)` com `.reason: str` legível.
  - `@dataclass StagedPackage(update_id, version, git_commit, built_at, files_count, deps_changed)`.
  - `validate_and_stage(zip_path: Path, staging_root: Path, local_pyproject: Path, *, update_id: str) -> StagedPackage` — valida (zip ok, manifesto, traversal, allowlist, deny-list, zip-bomb), extrai para `staging_root/<update_id>/portal-pedidos/`, computa `deps_changed`. Levanta `PackageError` (nada extraído) se inválido.
- Consumes: nada (stdlib).

Constantes no módulo: `ROOT = "portal-pedidos"`, `ALLOWED_TOP = {"app","scripts","tools","ui.py","main.py","pyproject.toml",".env.example","README.md","INSTALACAO-SERVIDOR.md","manifest.json"}` (+ `*.bat` por sufixo), `DENY_SUBSTR`/`DENY_SUFFIX` = `{.env, .secret.key, config.json, firebird.json}` / `{.db,.sqlite,.sqlite3,.fdb,.fbk,.gbk}` + qualquer membro começando com `portal-pedidos/data/`, `MAX_MEMBERS = 10_000`, `MAX_UNCOMPRESSED = 500*1024*1024`.

- [ ] **Step 1: Testes — deps hash**

```python
# tests/test_update_package.py
from pathlib import Path
import pytest
from app.updates import package as pkg


def _pyproject(tmp_path, deps: list[str]) -> Path:
    body = 'requires-python = ">=3.11"\n'
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
```

- [ ] **Step 2: Rodar (falha: módulo não existe)**

Run: `.venv/bin/pytest tests/test_update_package.py -k deps_sha -q`
Expected: FAIL (ImportError/AttributeError).

- [ ] **Step 3: Implementar `compute_deps_sha256`**

```python
# app/updates/package.py
from __future__ import annotations

import hashlib
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

ROOT = "portal-pedidos"
ALLOWED_TOP = {
    "app", "scripts", "tools", "ui.py", "main.py", "pyproject.toml",
    ".env.example", "README.md", "INSTALACAO-SERVIDOR.md", "manifest.json",
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
    data = tomllib.load(open(pyproject_path, "rb"))
    proj = data.get("project", {})
    deps = list(proj.get("dependencies", []))
    for v in proj.get("optional-dependencies", {}).values():
        deps += list(v)
    norm = "\n".join(sorted(d.strip() for d in deps if d.strip()))
    return hashlib.sha256(norm.encode()).hexdigest()
```

- [ ] **Step 4: Rodar (passa)**

Run: `.venv/bin/pytest tests/test_update_package.py -k deps_sha -q`
Expected: PASS.

- [ ] **Step 5: Testes — validação do zip**

Helper para montar zips + os casos. Adicionar a `tests/test_update_package.py`:

```python
import io, json, zipfile as zf


def _make_zip(tmp_path, members: dict[str, bytes], name="pkg.zip") -> Path:
    p = tmp_path / name
    with zf.ZipFile(p, "w") as z:
        for arc, data in members.items():
            z.writestr(arc, data)
    return p


def _valid_manifest(deps_sha="abc") -> bytes:
    return json.dumps({
        "name": "portal-pedidos", "version": "20260714-1030",
        "built_at": "2026-07-14T10:30:00Z", "git_commit": "deadbee",
        "deps_sha256": deps_sha,
    }).encode()


def _base_members(deps_sha="abc") -> dict[str, bytes]:
    return {
        "portal-pedidos/manifest.json": _valid_manifest(deps_sha),
        "portal-pedidos/pyproject.toml": b'[project]\nname="x"\nversion="0.1.0"\ndependencies=["fastapi"]\n',
        "portal-pedidos/app/__init__.py": b"",
        "portal-pedidos/ui.py": b"# ui\n",
    }


def test_manifesto_ausente_rejeita(tmp_path):
    m = _base_members(); del m["portal-pedidos/manifest.json"]
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(_make_zip(tmp_path, m), tmp_path / "st",
                               _pyproject(tmp_path / "pp", ["fastapi"]), update_id="u1")


def test_name_divergente_rejeita(tmp_path):
    m = _base_members(); m["portal-pedidos/manifest.json"] = json.dumps({"name": "outro"}).encode()
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(_make_zip(tmp_path, m), tmp_path / "st",
                               _pyproject(tmp_path / "pp", ["fastapi"]), update_id="u1")


@pytest.mark.parametrize("bad", [
    "portal-pedidos/../evil.py", "/etc/passwd", "portal-pedidos/../../x",
    "C:\\\\windows\\\\x", "portal-pedidos/data/app_shared.db",
    "portal-pedidos/app/.secret.key", "portal-pedidos/.env",
    "portal-pedidos/x.db", "portal-pedidos/naoexiste_na_allowlist.txt",
])
def test_membros_proibidos_rejeitam(tmp_path, bad):
    m = _base_members(); m[bad] = b"x"
    with pytest.raises(pkg.PackageError):
        pkg.validate_and_stage(_make_zip(tmp_path, m), tmp_path / "st",
                               _pyproject(tmp_path / "pp", ["fastapi"]), update_id="u1")


def test_pacote_valido_extrai_e_reporta(tmp_path):
    sha = pkg.compute_deps_sha256(_pyproject(tmp_path / "pp", ["fastapi"]))
    m = _base_members(deps_sha=sha)
    st = tmp_path / "st"
    res = pkg.validate_and_stage(_make_zip(tmp_path, m), st,
                                 _pyproject(tmp_path / "pp2", ["fastapi"]), update_id="u1")
    assert res.version == "20260714-1030"
    assert res.deps_changed is False  # mesmo sha
    assert (st / "u1" / "portal-pedidos" / "ui.py").read_bytes() == b"# ui\n"


def test_deps_changed_true_quando_hash_difere(tmp_path):
    m = _base_members(deps_sha="hash-antigo-diferente")
    res = pkg.validate_and_stage(_make_zip(tmp_path, m), tmp_path / "st",
                                 _pyproject(tmp_path / "pp", ["fastapi"]), update_id="u1")
    assert res.deps_changed is True
```

- [ ] **Step 6: Rodar (falha)**

Run: `.venv/bin/pytest tests/test_update_package.py -q`
Expected: FAIL (`validate_and_stage`/`StagedPackage` não existem).

- [ ] **Step 7: Implementar `validate_and_stage`**

```python
# app/updates/package.py (append)
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
    rel = name[len(ROOT) + 1:]
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
        update_id=update_id, version=manifest["version"], git_commit=manifest["git_commit"],
        built_at=manifest["built_at"], files_count=files,
        deps_changed=(manifest["deps_sha256"] != local_sha),
    )
```

Precisa `import json` no topo.

- [ ] **Step 8: Rodar (passa)**

Run: `.venv/bin/pytest tests/test_update_package.py -q`
Expected: PASS (todos).

- [ ] **Step 9: Ruff + commit**

```bash
.venv/bin/ruff check app/updates/ tests/test_update_package.py
git add app/updates/__init__.py app/updates/package.py tests/test_update_package.py
git commit -m "feat(update): validação segura + deps_sha256 do pacote (app/updates/package)"
```

---

## Task 3: estado do update (`app/updates/state.py`)

**Files:**
- Create: `app/updates/state.py`
- Test: `tests/test_update_state.py`

**Interfaces:**
- Produces (todas recebem `updates_dir: Path` = `<AppDir>/data/updates`):
  - `read_status(updates_dir) -> dict` — `{"status": "idle", ...}` se não existe.
  - `write_status(updates_dir, **fields) -> None` — merge atômico em `status.json`.
  - `append_history(updates_dir, entry: dict) -> None` — 1 linha JSON em `history.jsonl`.
  - `lock_path(updates_dir) -> Path`; `is_locked(updates_dir) -> bool`; `lock_age_seconds(updates_dir, now_ts: float) -> float | None` (None se sem lock).
- Consumes: nada.

- [ ] **Step 1: Testes**

```python
# tests/test_update_state.py
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
```

(precisa `import pytest` no topo)

- [ ] **Step 2: Rodar (falha)**

Run: `.venv/bin/pytest tests/test_update_state.py -q` → FAIL.

- [ ] **Step 3: Implementar**

```python
# app/updates/state.py
from __future__ import annotations

import json
import os
from pathlib import Path

_STATUS = "status.json"
_LOCK = "update.lock"
_HIST = "history.jsonl"


def _ensure(updates_dir: Path) -> None:
    updates_dir.mkdir(parents=True, exist_ok=True)


def read_status(updates_dir: Path) -> dict:
    p = updates_dir / _STATUS
    if not p.exists():
        return {"status": "idle"}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "idle"}


def write_status(updates_dir: Path, **fields) -> None:
    _ensure(updates_dir)
    cur = read_status(updates_dir)
    cur.update(fields)
    tmp = updates_dir / (_STATUS + ".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    tmp.replace(updates_dir / _STATUS)


def append_history(updates_dir: Path, entry: dict) -> None:
    _ensure(updates_dir)
    with open(updates_dir / _HIST, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def lock_path(updates_dir: Path) -> Path:
    return updates_dir / _LOCK


def is_locked(updates_dir: Path) -> bool:
    return lock_path(updates_dir).exists()


def lock_age_seconds(updates_dir: Path, now_ts: float) -> float | None:
    p = lock_path(updates_dir)
    if not p.exists():
        return None
    return now_ts - os.path.getmtime(p)
```

- [ ] **Step 4: Rodar (passa) + ruff + commit**

```bash
.venv/bin/pytest tests/test_update_state.py -q
.venv/bin/ruff check app/updates/state.py tests/test_update_state.py
git add app/updates/state.py tests/test_update_state.py
git commit -m "feat(update): estado (status/lock/history) do update"
```

---

## Task 4: rotas `/api/admin/update/*`

**Files:**
- Create: `app/web/routes_update.py`
- Modify: `app/web/server.py` (registrar o router — achar onde `app.include_router(routes_environments...)` e espelhar)
- Test: `tests/test_update_routes.py`

**Interfaces:**
- Consumes: `require_admin` (de `app/web/auth.py`), `app.updates.package`, `app.updates.state`, `app.config` (para `AppDir`/porta se preciso).
- Produces: `router = APIRouter(prefix="/api/admin/update", tags=["admin","update"])` com:
  - `POST /upload` (`file: UploadFile`) → 200 resumo | 400/413/422/409.
  - `POST /apply` (`{"update_id": str}`) → 202 | 404/409.
  - `GET /status` → 200 status dict.
- Módulo expõe `updates_dir() -> Path`, `staging_dir() -> Path` (sob `APP_DATA_DIR/updates`), e `_start_updater_task()` (isolado p/ mock no teste — chama `schtasks /run /tn PortalPedidosUpdater`; retorna `bool` "task existe/disparou").

> **Nota de auth para o teste:** os testes de rota do projeto usam `TEST_AUTH_BYPASS=1` (ver `conftest.py`) → `require_admin` devolve admin sintético. Para os casos 401/403, seguir o padrão de `tests/test_firebird_config_api.py` (fixture `real_auth` / sem bypass). Verificar esse arquivo antes de escrever os testes de auth.

- [ ] **Step 1: Ler o padrão de registro de router + auth de teste**

Run: `grep -n "include_router" app/web/server.py; sed -n '1,40p' tests/test_firebird_config_api.py`
Expected: ver como routers são registrados e como os testes de auth admin são montados. (Sem código a mudar; é reconhecimento.)

- [ ] **Step 2: Testes de contrato/auth**

```python
# tests/test_update_routes.py
import io, json, zipfile
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    from app.persistence import db
    db.reset_init_cache()
    yield tmp_path
    db.reset_init_cache()


def _client():
    from app.web.server import app
    return TestClient(app)


def _good_zip(deps_sha) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("portal-pedidos/manifest.json", json.dumps({
            "name": "portal-pedidos", "version": "20260714-1030",
            "built_at": "2026-07-14T10:30:00Z", "git_commit": "deadbee",
            "deps_sha256": deps_sha}))
        z.writestr("portal-pedidos/ui.py", b"# ui\n")
    return buf.getvalue()


def test_status_idle(setup):
    r = _client().get("/api/admin/update/status")
    assert r.status_code == 200 and r.json()["status"] == "idle"


def test_upload_nao_zip_400(setup):
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 400


def test_upload_zip_invalido_422(setup, monkeypatch):
    # zip válido de bytes mas sem manifesto → 422 com motivo
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("portal-pedidos/ui.py", b"x")
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", buf.getvalue(), "application/zip")})
    assert r.status_code == 422 and "manifest" in r.json()["detail"].lower()


def test_upload_valido_200_resumo(setup, monkeypatch):
    from app.updates import package
    # força deps_changed=False fazendo o hash local == o do manifesto
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", _good_zip("SHA"), "application/zip")})
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == "20260714-1030" and body["deps_changed"] is False
    assert body["update_id"]


def test_apply_update_id_errado_404(setup):
    r = _client().post("/api/admin/update/apply", json={"update_id": "nao-existe"})
    assert r.status_code in (404, 409)  # sem staged → 404


def test_apply_dispara_updater(setup, monkeypatch):
    from app.updates import package
    from app.web import routes_update
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    up = _client().post("/api/admin/update/upload",
                        files={"file": ("p.zip", _good_zip("SHA"), "application/zip")}).json()
    called = {}
    monkeypatch.setattr(routes_update, "_start_updater_task",
                        lambda: called.setdefault("ran", True) or True)
    r = _client().post("/api/admin/update/apply", json={"update_id": up["update_id"]})
    assert r.status_code == 202 and called.get("ran")
```

- [ ] **Step 3: Rodar (falha)**

Run: `.venv/bin/pytest tests/test_update_routes.py -q` → FAIL (rotas inexistentes).

- [ ] **Step 4: Implementar `routes_update.py`**

```python
# app/web/routes_update.py
from __future__ import annotations

import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.updates import package, state
from app.web.auth import require_admin

router = APIRouter(prefix="/api/admin/update", tags=["admin", "update"])

MAX_PACKAGE_BYTES = 100 * 1024 * 1024
_UPDATER_TASK = "PortalPedidosUpdater"


def _app_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def _data_dir() -> Path:
    return Path(os.environ.get("APP_DATA_DIR") or (_app_dir() / "data"))


def updates_dir() -> Path:
    return _data_dir() / "updates"


def staging_dir() -> Path:
    return updates_dir() / "staging"


def _current_version() -> str:
    p = _app_dir() / "data" / "applied_update.json"
    if p.exists():
        try:
            import json
            return json.loads(p.read_text())["version"]
        except Exception:
            pass
    return "desconhecida"


def _start_updater_task() -> bool:
    """Dispara a task one-shot. Retorna False se ela não existe (não configurada)."""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", _UPDATER_TASK],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


@router.get("/status")
def status(_=Depends(require_admin)):
    s = state.read_status(updates_dir())
    s.setdefault("current_version", _current_version())
    return s


@router.post("/upload")
async def upload(file: UploadFile = File(...), _=Depends(require_admin)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "envie um arquivo .zip")
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    tmp = Path(tempfile.mkstemp(suffix=".zip")[1])
    size = 0
    try:
        with open(tmp, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_PACKAGE_BYTES:
                    raise HTTPException(413, "pacote excede o limite de 100MB")
                out.write(chunk)
        update_id = uuid.uuid4().hex[:12]
        # limpa staging anterior (só um staged por vez)
        sd = staging_dir()
        if sd.exists():
            import shutil
            shutil.rmtree(sd)
        try:
            res = package.validate_and_stage(
                tmp, sd, _app_dir() / "pyproject.toml", update_id=update_id
            )
        except package.PackageError as e:
            raise HTTPException(422, e.reason) from None
        state.write_status(updates_dir(), status="staged", update_id=res.update_id,
                           version=res.version, deps_changed=res.deps_changed)
        return {
            "update_id": res.update_id, "version": res.version,
            "git_commit": res.git_commit, "built_at": res.built_at,
            "files_count": res.files_count, "deps_changed": res.deps_changed,
            "current_version": _current_version(),
        }
    finally:
        tmp.unlink(missing_ok=True)


class ApplyBody(BaseModel):
    update_id: str


@router.post("/apply", status_code=202)
def apply(body: ApplyBody, _=Depends(require_admin)):
    if state.is_locked(updates_dir()):
        raise HTTPException(409, "há um update em andamento")
    s = state.read_status(updates_dir())
    if s.get("status") != "staged" or s.get("update_id") != body.update_id:
        raise HTTPException(404, "update_id não corresponde ao pacote staged")
    state.write_status(updates_dir(), status="apply_requested", started_at=time.time())
    if not _start_updater_task():
        state.write_status(updates_dir(), status="staged")  # reverte
        raise HTTPException(409, "serviço de update não configurado — rode setup-service.bat no servidor")
    return {"update_id": body.update_id, "status": "apply_requested"}
```

- [ ] **Step 5: Registrar o router em `server.py`**

Achar a linha que inclui os routers (ex.: `app.include_router(routes_environments.router)`) e adicionar ao lado:

```python
from app.web import routes_update  # noqa: E402
app.include_router(routes_update.router)
```

- [ ] **Step 6: Rodar (passa) + ruff**

Run: `.venv/bin/pytest tests/test_update_routes.py -q` → PASS.
Run: `.venv/bin/ruff check app/web/routes_update.py tests/test_update_routes.py`

- [ ] **Step 7: Commit**

```bash
git add app/web/routes_update.py app/web/server.py tests/test_update_routes.py
git commit -m "feat(update): rotas /api/admin/update (upload/apply/status)"
```

---

## Task 5: UI admin `/admin/atualizacao`

**Files:**
- Create: `app/web/static/admin-atualizacao.html`
- Modify: `app/web/server.py` (rota de página `GET /admin/atualizacao` — espelhar `/admin/ambientes`) + item de menu no shell (`app/web/static/js/shell.js`, grupo Configurações, admin-only) se aplicável.
- Test: `tests/test_update_routes.py` (smoke: página serve 200 autenticado)

**Interfaces:**
- Consumes: `/api/admin/update/*`, o shell existente (`tokens.css`, `shell.css`, `shell.js`).
- Produces: página estática. Estados: idle (versão + dropzone) → staged (resumo + Aplicar/Descartar) → em-andamento (timeline por poll do status; erro de rede durante restart = "reiniciando") → resultado (sucesso/rollback).

- [ ] **Step 1: Ler o padrão da página admin existente**

Run: `grep -n "admin/ambientes" app/web/server.py; sed -n '1,30p' app/web/static/admin-ambiente-edit.html`
Expected: ver a rota de página + o esqueleto shell (imports de css/js, gate). Sem mudança; reconhecimento.

- [ ] **Step 2: Rota de página + smoke test**

Adicionar em `server.py` (espelhando a rota de `/admin/ambientes`):

```python
@app.get("/admin/atualizacao")
def admin_atualizacao_page(_user: User = Depends(require_user)):
    return FileResponse(_STATIC / "admin-atualizacao.html")
```

Teste (append a `tests/test_update_routes.py`):

```python
def test_pagina_atualizacao_serve(setup):
    r = _client().get("/admin/atualizacao")
    assert r.status_code == 200 and b"atualiza" in r.content.lower()
```

- [ ] **Step 3: Rodar (falha) → criar `admin-atualizacao.html`**

Criar `app/web/static/admin-atualizacao.html` no padrão shell: `<link>` para `css/tokens.css`+`css/shell.css`, `<script src="js/shell.js">`, container com:
- bloco "Versão atual" (busca `GET /api/admin/update/status`),
- dropzone `<input type=file accept=.zip>` → `POST /upload` (FormData) → mostra card-resumo (version, git_commit, built_at, files_count, badge se `deps_changed`),
- botões "Aplicar atualização" (confirm: *"O portal ficará indisponível 1–3 min. Confirmar?"*) → `POST /apply` → inicia poll,
- poll de `GET /status` a cada 3s: renderiza `phase` como timeline; `fetch` que falha (rede) durante o restart vira fase "reiniciando…"; teto ~5 min; ao voltar `succeeded`/`rolled_back`/`rollback_failed`, mostra resultado.

(HTML/JS: seguir o estilo de `admin-ambiente-edit.html` — fetch + render, sem framework.)

- [ ] **Step 4: Rodar smoke (passa) + commit**

```bash
.venv/bin/pytest tests/test_update_routes.py -k pagina_atualizacao -q
git add app/web/static/admin-atualizacao.html app/web/server.py app/web/static/js/shell.js tests/test_update_routes.py
git commit -m "feat(update): tela admin /admin/atualizacao (upload + progresso)"
```

---

## Task 6: updater `scripts/apply-update.ps1` (Windows — validado no cliente)

**Files:**
- Create: `scripts/apply-update.ps1`
- Test: checklist manual no Windows (§ 13 da spec). PowerShell não roda em CI.

**Interfaces:**
- Consumes: `data/updates/staging/<id>/portal-pedidos/`, `data/updates/status.json` (lê `update_id`, `deps_changed`), o `.env` (porta). Escreve `status.json` (fases), `update.lock`, `backups/update/<id>/`, `data/applied_update.json`, `history.jsonl`.
- Fases (spec § 4, passo 8): lock → re-valida staging → backup → stop (libera porta 3636, mata PID só se sob `.venv`) → clean-replace de `app/` preservando `app/.secret.key` + copia resto da allowlist → pip se `deps_changed` → start → health-check 120s → succeeded (grava applied_update.json, apaga staging+lock, poda backups p/ 2) OU rollback → sempre apaga lock no `finally`.

- [ ] **Step 1: Escrever o script completo**

Criar `scripts/apply-update.ps1` implementando as fases. Pontos obrigatórios (traduzir a spec § 4/8/10 fielmente):
- `$AppDir = Split-Path -Parent $PSScriptRoot`; `$Updates = "$AppDir\data\updates"`.
- Ler `update_id`/`deps_changed` de `status.json`; `$Staging="$Updates\staging\$id\portal-pedidos"`.
- `New-Item $Updates\update.lock`; tudo dentro de `try{}finally{ Remove-Item update.lock }`.
- `Write-Phase` helper que reescreve `status.json` (status=`in_progress`, phase=X, started_at).
- Backup: `Copy-Item app,scripts,tools,ui.py,main.py,pyproject.toml,*.bat -Destination backups\update\$id\ -Recurse`.
- Stop: `Stop-ScheduledTask PortalPedidos`; loop até `Get-NetTCPConnection -LocalPort <porta>` sumir (30s); fallback `Stop-Process` do OwningProcess **só se** `(Get-Process -Id $pid).Path -like "$AppDir\.venv\*"`.
- Apply: preservar `app\.secret.key` (copiar p/ temp), `Remove-Item app -Recurse -Force`, `Move-Item $Staging\app app`, restaurar `.secret.key`; `Copy-Item` dos demais membros da allowlist por cima; NUNCA tocar `.env data config.json logs backups .venv`.
- Pip: se `deps_changed` → `& "$AppDir\.venv\Scripts\pip.exe" install -e $AppDir`; erro → rollback.
- Start+health: `Start-ScheduledTask PortalPedidos`; poll `Invoke-WebRequest http://127.0.0.1:<porta>/health -TimeoutSec 5` por 120s.
- Sucesso: escreve `data\applied_update.json` (`{version, git_commit, applied_at}` do manifesto), status `succeeded`, apaga `$Staging`, poda `backups\update` mantendo os 2 mais novos, `append` em `history.jsonl`.
- Rollback (função): restaura backup por cima, pip se deps mudaram, start, health; status `rolled_back` (com erro original) ou `rollback_failed`.

- [ ] **Step 2: Checklist manual no Windows (documentar no PR, não bloqueia o merge do código)**

- update feliz (sem deps) → app volta, versão nova, pip NÃO rodou.
- update com deps mudadas → pip rodou.
- pacote com `server.py` quebrado → health falha → rollback automático → versão anterior de volta.
- `.env`, `data/*.db`, `app/.secret.key`, `config.json` intactos (hash antes/depois).

- [ ] **Step 3: Commit**

```bash
git add scripts/apply-update.ps1
git commit -m "feat(update): updater out-of-process (apply-update.ps1) — Windows"
```

---

## Task 7: watchdog `scripts/watchdog.ps1` (Windows — validado no cliente)

**Files:**
- Create: `scripts/watchdog.ps1`
- Test: checklist manual (matar processo → religa; suspender processo → religa; lock presente → no-op).

**Interfaces:**
- Consumes: `.env` (porta), `data/updates/update.lock`, `data/updates/watchdog_state.json` (contador). Escreve `logs/watchdog.log`.
- Lógica (spec § 8): se `update.lock` existe e idade < 30 min → no-op; se > 30 min → remove lock (órfão) e loga. `GET /health` timeout 10s. Falha → incrementa contador persistido; 3 seguidas → `Stop-ScheduledTask PortalPedidos` (+ mata PID sob `.venv` se porta presa) → `Start-ScheduledTask` → zera contador → espera 3 ciclos (anti-flap). Sucesso → zera contador. Se task não `Running` → religa direto.

- [ ] **Step 1: Escrever o script + Step 2: checklist Windows + Step 3: commit**

```bash
git add scripts/watchdog.ps1
git commit -m "feat(update): watchdog por health-check (watchdog.ps1) — Windows"
```

---

## Task 8: registrar/remover as 3 tasks + pip condicional no update manual

**Files:**
- Modify: `scripts/setup-service.ps1` (registrar `PortalPedidos` [como hoje] + `PortalPedidosUpdater` [on-demand, sem trigger, SYSTEM, `-File apply-update.ps1`] + `PortalPedidosWatchdog` [trigger repeat 1 min, SYSTEM, `-File watchdog.ps1`]; idempotente)
- Modify: `scripts/uninstall-service.ps1` (remover as 3)
- Modify: `scripts/update.ps1` (pip condicional: computar `deps_sha256` do `pyproject.toml` atual vs o do `data/applied_update.json`/manifesto — rodar `pip install -e` só se mudou; paridade com o updater)
- Test: checklist manual (as 3 tasks aparecem em `Get-ScheduledTask`; desinstalar remove; update manual pula pip quando deps iguais)

**Interfaces:**
- Consumes: `apply-update.ps1`, `watchdog.ps1`. Produces: as tasks registradas.

- [ ] **Step 1: Editar os 3 scripts (seguir o padrão idempotente de `setup-service.ps1` — `Unregister` se existe, `Register-ScheduledTask` com principal SYSTEM/AtStartup ou Repeat).**
- [ ] **Step 2: Checklist Windows.**
- [ ] **Step 3: Commit**

```bash
git add scripts/setup-service.ps1 scripts/uninstall-service.ps1 scripts/update.ps1
git commit -m "feat(update): registra tasks updater+watchdog; pip condicional no update manual"
```

---

## Task 9: fechamento — suíte cheia, ruff, build, PR

- [ ] **Step 1:** `.venv/bin/pytest tests/ -q` → tudo verde.
- [ ] **Step 2:** `.venv/bin/ruff check app/ tests/ tools/` → limpo.
- [ ] **Step 3:** `bash tools/build_package.sh` → novo zip inclui `manifest.json` + `app/updates/` + os `.ps1`; conferir com `unzip -l`.
- [ ] **Step 4:** PR com o checklist manual do Windows (Tasks 6–8) no corpo, marcado como "validar no cliente-teste".

---

## Self-review (feito)

- **Cobertura da spec:** §5 rotas→T4; §6 validação→T2; §8 watchdog→T7; §7/§3 updater→T6; §9 pip→T2(hash)+T6/T8; §10 backup/rollback→T6; §11 UI→T5; §12 manifesto→T1; §13 testes→T2/T3/T4 (unit/rotas) + T6/T7/T8 (checklist Windows). §14 decisões já cravadas pelo founder (task+watchdog, 2 passos, sem assinatura, clean-replace, params do watchdog, 2 backups, sem wheelhouse, /health sem versão).
- **Tipos:** `StagedPackage`/`PackageError`/`compute_deps_sha256`/`validate_and_stage` consistentes entre T2 e T4; `_start_updater_task` isolado p/ mock (T4).
- **Placeholders:** as tasks 6–8 (PowerShell) descrevem o script por fases fiéis à spec em vez de código Python — deliberado, pois não há TDD para PowerShell no Mac; o "teste" é o checklist Windows. Todo o código Python está completo.
