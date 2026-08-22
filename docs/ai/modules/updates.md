# Módulo: updates (auto-update do portal)

## Responsabilidade
Atualizar a instalação do cliente **pelo próprio portal**, sem ninguém mexer em arquivo
no servidor. O admin sobe o `.zip` em `/admin/atualizacao`, o portal valida e encena o
pacote, e um processo **separado** (Tarefa Agendada do Windows) aplica — porque
`pip install -e` trava DLL/`.pyd` do processo em execução, então o app não pode se
atualizar de dentro de si mesmo.

**Este é o caminho oficial de upgrade.** O `atualizar.bat` manual só existe para
recuperar uma instalação que não sobe.

## Arquivos críticos
- `app/updates/package.py` — validação e staging do `.zip` (allowlist, limites, manifest).
- `app/updates/state.py` — `status.json`, `update.lock` e `history.jsonl` no disco;
  escrita atômica via `tempfile` + `replace`.
- `app/web/routes_update.py` — API admin-only, prefixo **`/api/admin/update`**.
- `app/web/static/admin-atualizacao.html` — a tela (`GET /admin/atualizacao`).
- `scripts/apply-update.ps1` — o updater de verdade (roda fora do app).
- `tools/build_package.sh` — gera o pacote do lado do dev.

## Rotas (todas `require_admin`)

| Rota | O que faz |
|---|---|
| `GET /api/admin/update/status` | Estado atual + `current_version`/`applied_at` lidos de `applied_update.json` |
| `POST /api/admin/update/upload` | Recebe o `.zip`, valida, encena. Limite **100MB**. Só **um** pacote staged por vez |
| `POST /api/admin/update/apply` | Exige `update_id` == o staged; dispara `schtasks /run /tn PortalPedidosUpdater`. Responde **202** |
| `POST /api/admin/update/dismiss` | Volta pra idle. Pacote staged é apagado do disco de verdade |

## Validação do pacote (`validate_and_stage`)

Rejeita com `PackageError` → **HTTP 422** e a razão vai pra tela:

- Raiz obrigatória `portal-pedidos/`; qualquer membro fora dela é recusado.
- **Allowlist de topo:** `app`, `scripts`, `tools`, `ui.py`, `main.py`, `pyproject.toml`,
  `.env.example`, `README.md`, `INSTALACAO-SERVIDOR.md`, `manifest.json`, `wheelhouse`
  (wheels para `pip install` **offline**, quando o servidor não alcança o PyPI).
- **Denylist explícita:** `.env`, `.secret.key`, `config.json`, `firebird.json`, e
  qualquer `.db/.sqlite/.sqlite3/.fdb/.fbk/.gbk`. Segredo e dado do cliente nunca
  entram num pacote.
- Sem symlink, sem path traversal, ≤ **10.000** membros, ≤ **500MB** descomprimidos.
- `manifest.json` obrigatório, com `name` conferido e campos exigidos presentes.

**`deps_changed`** é decidido comparando o sha256 das dependências do `pyproject.toml`
do **pacote** com o do `pyproject.toml` **já instalado no cliente** (`compute_deps_sha256`)
— é isso que faz o updater pular ou rodar a fase `pip`.

## Concorrência: dois guards, não um

`update.lock` só nasce **depois** que o processo updater arranca — o que é assíncrono,
~1–3s após o `/apply` disparar o `schtasks`. Nessa janela existe status
`apply_requested` sem lock no disco. Por isso todo caminho de escrita checa **os dois**:
`state.is_locked()` **e** `_reject_if_update_running()` (status ∈
`apply_requested | in_progress`). Sem o segundo, um segundo `/upload` nessa janela
corromperia o staging do pacote em aplicação.

**Updater morto:** status "rodando" **sem** lock e com `started_at` mais velho que
`_STALE_RUNNING_SECONDS = 180` significa que o updater morreu sem escrever status
terminal. Só nesse caso o `/dismiss` destrava; caso contrário responde 409.

## Fases e estados (lado do `apply-update.ps1`)

```
lock → revalida staging → backup → stop → apply (clean-replace de app/)
     → pip condicional → start → health-check → succeeded | rollback
```

Fases reportadas: `backup | stop | apply | pip | start | healthcheck`.
Estados terminais: `succeeded | rolled_back | rollback_failed`.

Backup em `backups/update/<id>/` — **sempre sob o AppDir**, nunca sob o DataDir.

## Armadilha resolvida: `APP_DATA_DIR` divergente

O `.ps1` resolve o data dir **exatamente** como `routes_update._data_dir()`
(`APP_DATA_DIR` absoluto usa como está; relativo resolve contra o AppDir, que é o CWD do
app em produção; ausente cai em `<AppDir>/data`). Se divergisse, o web leria
`status.json` num lugar e o updater escreveria em outro — a UI ficaria pendurada em "em
andamento" **para sempre**, em silêncio.

## Testes
- `tests/test_update_package.py` — validação, allowlist/denylist, limites, `deps_changed`.
- `tests/test_update_routes.py` — API, 409 de concorrência, 422 de pacote inválido, dismiss.
- `tests/test_update_state.py` — status/lock/history, escrita atômica.

```bash
.venv/bin/pytest tests/test_update_package.py tests/test_update_routes.py tests/test_update_state.py -v
```
