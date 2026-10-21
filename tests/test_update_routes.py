import io
import json
import zipfile

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
    assert r.status_code == 404  # sem staged → sempre 404


@pytest.mark.parametrize("running_status", ["apply_requested", "in_progress"])
def test_upload_rejeita_409_quando_update_em_andamento(setup, running_status):
    """A janela entre /apply disparar `schtasks /run` e o updater criar
    update.lock (~1-3s) não tem lock no disco ainda. Sem uma guarda de
    ESTADO (além do is_locked), um 2º /upload nessa janela apagaria o
    staging do pacote que está sendo aplicado."""
    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status=running_status, update_id="x")
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", b"conteudo qualquer", "application/zip")})
    assert r.status_code == 409
    assert "andamento" in r.json()["detail"].lower()


def test_upload_409_se_apply_dispara_no_meio_do_upload(setup, monkeypatch):
    """TOCTOU: os guards rodam antes de ler o corpo (até 100MB). Se um /apply
    entrar durante o upload, o upload não pode apagar o staging em uso nem o
    status in_progress."""
    from app.updates import package, state
    from app.web import routes_update

    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    r1 = _client().post("/api/admin/update/upload",
                        files={"file": ("v1.zip", _good_zip("SHA"), "application/zip")})
    assert r1.status_code == 200
    update_id_v1 = r1.json()["update_id"]
    staged_v1 = routes_update.staging_dir() / update_id_v1

    original = package.validate_and_stage
    novo_id = {}

    def stage_e_apply_no_meio(*a, **kw):
        res = original(*a, **kw)
        novo_id["id"] = res.update_id
        state.write_status(routes_update.updates_dir(), status="in_progress",
                           update_id=update_id_v1)
        return res

    monkeypatch.setattr(package, "validate_and_stage", stage_e_apply_no_meio)
    r2 = _client().post("/api/admin/update/upload",
                        files={"file": ("v2.zip", _good_zip("SHA"), "application/zip")})
    assert r2.status_code == 409
    assert "andamento" in r2.json()["detail"].lower()
    assert staged_v1.exists()
    assert not (routes_update.staging_dir() / novo_id["id"]).exists()
    st = state.read_status(routes_update.updates_dir())
    assert st["status"] == "in_progress" and st["update_id"] == update_id_v1


@pytest.mark.parametrize("running_status", ["apply_requested", "in_progress"])
def test_apply_rejeita_409_quando_ja_em_andamento(setup, running_status):
    """Um 2º /apply enquanto o primeiro já está apply_requested/in_progress
    deve ser 409 (conflito), não 404 (que é reservado a update_id que não
    corresponde a nenhum staged legítimo)."""
    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status=running_status, update_id="x")
    r = _client().post("/api/admin/update/apply", json={"update_id": "x"})
    assert r.status_code == 409


def test_apply_idle_com_id_errado_continua_404(setup):
    """Não regride: sem update em andamento, update_id que não bate com
    nenhum staged continua 404 (não vira 409)."""
    r = _client().post("/api/admin/update/apply", json={"update_id": "nao-existe"})
    assert r.status_code == 404


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


def test_fase_do_update_anterior_nao_vaza_para_o_seguinte(setup, monkeypatch):
    """Produção, 18/10/2026: o status.json do update de 16/10 (succeeded,
    phase=healthcheck) foi mesclado no staged e no apply_requested do seguinte;
    a tela leu `healthcheck`, marcou todos os passos e voltou ao `backup`."""
    from app.updates import package, state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status="succeeded", phase="healthcheck",
                       update_id="velho", started_at=1.0, finished_at=2.0,
                       error="falha antiga", rollback_error="rollback antigo")
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    up = _client().post("/api/admin/update/upload",
                        files={"file": ("p.zip", _good_zip("SHA"), "application/zip")}).json()
    staged = _client().get("/api/admin/update/status").json()
    assert staged["status"] == "staged" and staged["update_id"] == up["update_id"]
    for velho in ("phase", "started_at", "finished_at", "error", "rollback_error"):
        assert staged.get(velho) is None, velho

    monkeypatch.setattr(routes_update, "_start_updater_task", lambda: True)
    assert _client().post("/api/admin/update/apply", json={"update_id": up["update_id"]}).status_code == 202
    pedido = _client().get("/api/admin/update/status").json()
    assert pedido["status"] == "apply_requested" and pedido.get("phase") is None
    assert pedido["version"] == "20260714-1030" and pedido.get("finished_at") is None


def test_upload_invalido_preserva_staging_valido_anterior(setup, monkeypatch):
    """CRITICAL: um upload que falha a validação não pode apagar o staging de
    um pacote válido anterior nem deixar status.json apontando para um
    update_id cujo staging já não existe mais no disco."""
    from app.updates import package
    from app.web import routes_update

    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    r1 = _client().post("/api/admin/update/upload",
                        files={"file": ("v1.zip", _good_zip("SHA"), "application/zip")})
    assert r1.status_code == 200
    update_id_v1 = r1.json()["update_id"]

    staged_dir_v1 = routes_update.staging_dir() / update_id_v1
    assert staged_dir_v1.exists()

    # zip válido como bytes, mas sem manifest.json → 422
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("portal-pedidos/ui.py", b"x")
    r2 = _client().post("/api/admin/update/upload",
                        files={"file": ("bad.zip", buf.getvalue(), "application/zip")})
    assert r2.status_code == 422

    # o staging do pacote v1 (válido) precisa sobreviver ao upload inválido
    assert staged_dir_v1.exists(), (
        "staging do pacote válido anterior foi apagado por um upload inválido"
    )

    # e o status.json deve continuar coerente com o disco: v1 ainda staged
    status = _client().get("/api/admin/update/status").json()
    assert status["status"] == "staged"
    assert status["update_id"] == update_id_v1


@pytest.mark.parametrize("terminal_status", ["succeeded", "rolled_back", "rollback_failed"])
def test_dismiss_reseta_status_terminal(setup, terminal_status):
    """Bug: um status terminal travava a tela de upload e só saía apagando o
    status.json na mão. /dismiss reseta pra idle (persistente no disco)."""
    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status=terminal_status, version="v1")
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "idle"
    assert state.read_status(routes_update.updates_dir())["status"] == "idle"


@pytest.mark.parametrize("running_status", ["apply_requested", "in_progress"])
def test_dismiss_recusa_409_quando_em_andamento(setup, running_status):
    """Não pode dispensar durante um update em andamento — e o estado em
    andamento NÃO é mexido."""
    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status=running_status, update_id="x")
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 409
    assert state.read_status(routes_update.updates_dir())["status"] == running_status


def test_dismiss_idle_e_noop_200(setup):
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "idle"


def test_dismiss_remove_pacote_staged(setup):
    """Bug: /dismiss era no-op pra 'staged' → não havia como remover um pacote
    staged a não ser aplicando, e ele reaparecia no reload. Agora remove de
    verdade (apaga o dir do disco + volta pra idle)."""
    from app.updates import state
    from app.web import routes_update

    uid = "abc123"
    staged = routes_update.staging_dir() / uid
    staged.mkdir(parents=True)
    (staged / "ui.py").write_text("# x")
    state.write_status(routes_update.updates_dir(), status="staged", update_id=uid, version="v1")

    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "idle"
    assert state.read_status(routes_update.updates_dir())["status"] == "idle"
    assert not staged.exists()


def test_status_apos_upload_carrega_resumo_completo(setup, monkeypatch):
    """Reload da tela (via /status) deve mostrar commit/build/arquivos, não '—'."""
    from app.updates import package

    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    c = _client()
    up = c.post("/api/admin/update/upload",
                files={"file": ("p.zip", _good_zip("SHA"), "application/zip")})
    assert up.status_code == 200
    st = c.get("/api/admin/update/status").json()
    assert st["status"] == "staged"
    assert st["git_commit"] == "deadbee"
    assert st["built_at"] == "2026-07-14T10:30:00Z"
    assert st.get("files_count", 0) >= 1


def test_dismiss_destrava_running_morto(setup):
    """Updater morreu sem escrever status terminal (sem lock + started_at antigo,
    ex.: watchdog já removeu o lock órfão): /dismiss destrava — senão o operador
    voltaria a editar o status.json na mão (o exato bug que o fix ataca)."""
    import time

    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status="in_progress",
                       started_at=time.time() - 3600)
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "idle"
    assert state.read_status(routes_update.updates_dir())["status"] == "idle"


def test_dismiss_running_recente_ainda_409(setup):
    """Um 'in_progress' RECENTE (apply de verdade, ainda na janela de criação do
    lock) NÃO pode ser dispensado."""
    import time

    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status="in_progress",
                       started_at=time.time())
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 409
    assert state.read_status(routes_update.updates_dir())["status"] == "in_progress"


def test_dismiss_lock_presente_409(setup):
    """Com update.lock no disco (updater vivo segurando), /dismiss recusa mesmo
    que o started_at fosse antigo."""
    import time

    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status="in_progress",
                       started_at=time.time() - 3600)
    state.lock_path(routes_update.updates_dir()).touch()
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 409


def test_dismiss_status_desconhecido_e_noop(setup):
    """Status inesperado (nem terminal, nem rodando) é no-op — não apaga nada."""
    from app.updates import state
    from app.web import routes_update

    state.write_status(routes_update.updates_dir(), status="weird")
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "weird"


def test_dismiss_sem_auth_401(setup, real_auth):
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 401


def test_dismiss_operador_nao_admin_403(setup, real_auth):
    c = _client()
    _bootstrap_operator_session(c)
    r = c.post("/api/admin/update/dismiss")
    assert r.status_code == 403


def test_status_sem_auth_401(setup, real_auth):
    r = _client().get("/api/admin/update/status")
    assert r.status_code == 401


def test_upload_sem_auth_401(setup, real_auth):
    r = _client().post("/api/admin/update/upload",
                       files={"file": ("p.zip", b"x", "application/zip")})
    assert r.status_code == 401


def test_apply_sem_auth_401(setup, real_auth):
    r = _client().post("/api/admin/update/apply", json={"update_id": "x"})
    assert r.status_code == 401


def test_pagina_atualizacao_serve(setup):
    r = _client().get("/admin/atualizacao")
    assert r.status_code == 200 and b"atualiza" in r.content.lower()


def test_status_inclui_applied_at_quando_presente(setup):
    """Spec §5: `/status` deve expor `applied_at?` — o front
    (admin-atualizacao.html) já lê `data.applied_at` para mostrar
    'aplicado em <data>' no card de versão atual."""
    (setup / "applied_update.json").write_text(json.dumps({
        "version": "20260701-1200",
        "git_commit": "abc1234",
        "applied_at": "2026-07-01T12:05:00Z",
    }))
    body = _client().get("/api/admin/update/status").json()
    assert body["current_version"] == "20260701-1200"
    assert body["applied_at"] == "2026-07-01T12:05:00Z"


def test_status_sem_applied_update_nao_expoe_applied_at(setup):
    body = _client().get("/api/admin/update/status").json()
    assert body["current_version"] == "desconhecida"
    assert "applied_at" not in body


def _bootstrap_operator_session(c: TestClient) -> None:
    """Cria o 1º admin, cria um operador, faz logout do admin e loga como
    o operador — deixa a sessão do client autenticada como não-admin."""
    r = c.post("/api/auth/bootstrap", json={"email": "admin@x.com", "password": "supersecret1"})
    assert r.status_code == 200, r.text
    r = c.post("/api/admin/users", json={
        "email": "op@x.com", "password": "operpass1", "role": "operator",
    })
    assert r.status_code == 201, r.text
    c.post("/api/auth/logout")
    r = c.post("/api/auth/login", json={"email": "op@x.com", "password": "operpass1"})
    assert r.status_code == 200, r.text


def test_status_operador_nao_admin_403(setup, real_auth):
    c = _client()
    _bootstrap_operator_session(c)
    r = c.get("/api/admin/update/status")
    assert r.status_code == 403


def test_upload_operador_nao_admin_403(setup, real_auth):
    c = _client()
    _bootstrap_operator_session(c)
    r = c.post("/api/admin/update/upload",
               files={"file": ("p.zip", b"x", "application/zip")})
    assert r.status_code == 403


def test_apply_operador_nao_admin_403(setup, real_auth):
    c = _client()
    _bootstrap_operator_session(c)
    r = c.post("/api/admin/update/apply", json={"update_id": "x"})
    assert r.status_code == 403


# ── POST /restart: reinicia o portal pelo mesmo updater, sem pacote ──────────

def test_restart_dispara_updater_202_e_grava_status_kind_restart(setup, monkeypatch):
    from app.updates import state
    from app.web import routes_update
    called = {}
    monkeypatch.setattr(routes_update, "_start_updater_task",
                        lambda: called.setdefault("ran", True) or True)
    r = _client().post("/api/admin/update/restart")
    assert r.status_code == 202 and called.get("ran")
    s = state.read_status(routes_update.updates_dir())
    assert s["kind"] == "restart" and s["status"] == "apply_requested"
    assert isinstance(s["started_at"], float)
    assert s["requested_by"] == "test@portal.local"


@pytest.mark.parametrize("running_status", ["apply_requested", "in_progress"])
def test_restart_rejeita_409_quando_update_em_andamento(setup, monkeypatch, running_status):
    """Guard de ESTADO (status rodando, sem lock no disco ainda)."""
    from app.updates import state
    from app.web import routes_update
    called = {}
    monkeypatch.setattr(routes_update, "_start_updater_task",
                        lambda: called.setdefault("ran", True) or True)
    state.write_status(routes_update.updates_dir(), status=running_status, update_id="x")
    r = _client().post("/api/admin/update/restart")
    assert r.status_code == 409 and "andamento" in r.json()["detail"].lower()
    assert not called


def test_restart_rejeita_409_com_lock_no_disco(setup, monkeypatch):
    """Guard de LOCK: updater vivo segura update.lock mesmo sem status rodando."""
    from app.updates import state
    from app.web import routes_update
    monkeypatch.setattr(routes_update, "_start_updater_task", lambda: True)
    lock = state.lock_path(routes_update.updates_dir())
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("pid=1")
    r = _client().post("/api/admin/update/restart")
    assert r.status_code == 409 and "andamento" in r.json()["detail"].lower()


def test_restart_rejeita_409_com_pacote_encenado_e_preserva_status(setup, monkeypatch):
    from app.updates import state
    from app.web import routes_update
    called = {}
    monkeypatch.setattr(routes_update, "_start_updater_task",
                        lambda: called.setdefault("ran", True) or True)
    state.write_status(routes_update.updates_dir(), status="staged", update_id="abc",
                       version="v1")
    r = _client().post("/api/admin/update/restart")
    assert r.status_code == 409
    assert "pacote" in r.json()["detail"].lower()
    s = state.read_status(routes_update.updates_dir())
    assert s["status"] == "staged" and s["update_id"] == "abc" and "kind" not in s
    assert not called


def test_restart_falha_ao_disparar_tarefa_reverte_e_409(setup, monkeypatch):
    from app.updates import state
    from app.web import routes_update
    monkeypatch.setattr(routes_update, "_start_updater_task", lambda: False)
    r = _client().post("/api/admin/update/restart")
    assert r.status_code == 409 and "setup-service" in r.json()["detail"]
    assert state.read_status(routes_update.updates_dir())["status"] == "idle"


def test_restart_apos_terminal_descarta_campos_do_update_anterior(setup, monkeypatch):
    from app.updates import state
    from app.web import routes_update
    monkeypatch.setattr(routes_update, "_start_updater_task", lambda: True)
    state.write_status(routes_update.updates_dir(), status="rolled_back",
                       update_id="velho", error="boom")
    assert _client().post("/api/admin/update/restart").status_code == 202
    s = state.read_status(routes_update.updates_dir())
    assert s["kind"] == "restart" and "error" not in s and "update_id" not in s


def test_upload_e_apply_apos_restart_nao_herdam_kind_restart(setup, monkeypatch):
    """O status.json é um só e o merge preserva chaves: sem limpar `kind`, um
    update feito depois de um restart seria executado como restart."""
    from app.updates import package, state
    from app.web import routes_update
    monkeypatch.setattr(package, "compute_deps_sha256", lambda p: "SHA")
    monkeypatch.setattr(routes_update, "_start_updater_task", lambda: True)
    c = _client()
    assert c.post("/api/admin/update/restart").status_code == 202
    state.write_status(routes_update.updates_dir(), status="succeeded")  # updater terminou
    up = c.post("/api/admin/update/upload",
                files={"file": ("p.zip", _good_zip("SHA"), "application/zip")}).json()
    assert state.read_status(routes_update.updates_dir()).get("kind") is None
    assert c.post("/api/admin/update/apply", json={"update_id": up["update_id"]}).status_code == 202
    assert state.read_status(routes_update.updates_dir()).get("kind") is None


def test_dismiss_limpa_restart_failed(setup):
    from app.updates import state
    from app.web import routes_update
    state.write_status(routes_update.updates_dir(), status="restart_failed", kind="restart")
    r = _client().post("/api/admin/update/dismiss")
    assert r.status_code == 200 and r.json()["status"] == "idle"


def test_restart_sem_auth_401(setup, real_auth):
    assert _client().post("/api/admin/update/restart").status_code == 401


def test_restart_operador_nao_admin_403(setup, real_auth):
    c = _client()
    _bootstrap_operator_session(c)
    assert c.post("/api/admin/update/restart").status_code == 403


def test_updater_ps1_tem_ramo_restart_sem_backup_nem_pip():
    """O .ps1 não roda no Mac: trava por texto o ramo `restart` (sem backup,
    staging, pip nem arquivos) e que o fluxo de atualização segue intacto."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "apply-update.ps1").read_text(
        encoding="utf-8")
    start = src.index("function Invoke-Restart")
    end = src.index("\n}\n", start)
    body = src[start:end]
    for fase in ("stop", "start", "healthcheck"):
        assert f'-Phase "{fase}"' in body
    for proibido in ("Backup-CurrentInstall", "Apply-StagedFiles", "Install-Dependencies",
                     "Assert-StagingValid", "Copy-Item", "Remove-Item"):
        assert proibido not in body
    assert "restart_failed" in body and 'kind = "restart"' in body
    # falha inesperada no ramo: despacho tem catch que grava restart_failed
    despacho = src[src.index("if ($isRestart)"):src.index("exit 0", src.index("if ($isRestart)"))]
    assert "} catch {" in despacho and '-Status "restart_failed"' in despacho
    assert '-eq "restart"' in src
    # o fluxo de atualização continua lá
    assert "Backup-CurrentInstall -Destination $backupDir" in src


def test_pagina_atualizacao_tem_botao_reiniciar_com_confirmacao(setup):
    body = _client().get("/admin/atualizacao").content.decode()
    assert "Reiniciar o portal" in body
    assert "Reiniciar o portal agora? Quem estiver usando perde a conexão por alguns segundos." in body
    assert "PORTAL_PORT" in body and "O reinício não foi confirmado" in body
    assert "RESTART_POLL_CAP_MS" in body
