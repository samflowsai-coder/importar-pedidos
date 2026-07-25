# Spec: Auto-update pelo portal (upload de pacote via VPN) + watchdog de health-check

**Data:** 2026-07-14
**Status:** proposta — aguardando revisão do founder
**Domínios:** web (`app/web/`), scripts Windows (`scripts/`), build (`tools/build_package.sh`)

---

## 1. Objetivo

Hoje, atualizar o Portal no servidor do cliente (Windows, 192.168.15.4) exige RDP/AnyDesk:
extrair o zip por cima da pasta e rodar `atualizar.bat` (→ `scripts/update.ps1`). O objetivo
é eliminar o RDP do fluxo: o admin, pelo navegador via VPN, sobe o zip do pacote numa tela
admin do próprio portal; o portal valida, faz backup, aplica, reinstala dependências se
necessário e reinicia de forma controlada. Se a nova versão não subir saudável, rollback
automático para a versão anterior.

Requisito acoplado (observado em produção): o app pode **travar** — aceita TCP na 3636 mas
não responde HTTP (nem `GET /health`) por 40s+. Restart-on-crash não cobre processo
pendurado. Sem um **watchdog externo por health-check**, o próprio endpoint de auto-update
fica inalcançável exatamente quando mais precisa. O watchdog entra nesta spec como
pré-requisito operacional do auto-update.

**Pré-requisito de deploy:** a Tarefa Agendada `PortalPedidos` (`setup-service.bat` →
`scripts/setup-service.ps1`) precisa estar ativa no cliente. Hoje o portal roda por
`iniciar.bat` (janela CMD manual) — esse modo NÃO é suportado pelo auto-update (não há
supervisor para religar o processo).

---

## 2. Contexto real do código (verificado)

| Fato | Onde |
|---|---|
| `GET /health` já existe, sem auth, retorna `{"status":"ok","service":"importar-pedidos"}` | `app/web/server.py` (linha ~393) |
| Auth admin: `require_admin` (401 anônimo / 403 não-admin), cookie `portal_session` HttpOnly SameSite=Strict | `app/web/auth.py` |
| Padrão de rota admin: `APIRouter(prefix="/api/admin/environments")` + páginas estáticas em `/admin/...` | `app/web/routes_environments.py`, `app/web/server.py` |
| Padrão de upload: `UploadFile = File(...)`, whitelist de extensão, `MAX_UPLOAD_BYTES = 50MB` | `/api/preview` e `/api/process` em `app/web/server.py` |
| Pacote atual: allowlist (`app scripts tools ui.py main.py pyproject.toml .env.example *.bat README.md INSTALACAO-SERVIDOR.md`), remove segredos/dados, zip com raiz `portal-pedidos/`, **sem manifesto** | `tools/build_package.sh` |
| Update atual: para task `PortalPedidos` → `git pull` (se repo) → `pip install -e` **sempre** → religa task | `scripts/update.ps1` |
| Serviço: Scheduled Task `PortalPedidos`, `pythonw.exe ui.py`, SYSTEM, AtStartup, `RestartCount 5` / `RestartInterval 2min`, `MultipleInstances IgnoreNew` | `scripts/setup-service.ps1` |
| Migração de schema: idempotente, aplicada na inicialização da conexão SQLite (`COLUMN_MIGRATIONS`) — nova instância migra sozinha | `app/persistence/router.py` |
| A preservar no update: `.env`, `data/` (`app_shared.db`, `app_state_<slug>.db`), `app/.secret.key` (`app/security/secret_store.py`), `config.json`, `logs/`, `backups/`, `.venv/` | raiz do app |
| Worker é processo separado (`python -m app.worker`, BlockingScheduler) e **não tem task registrada** nos scripts — não serve de watchdog | `app/worker/scheduler.py`, `scripts/` |
| Não existe lock file de deps; a fonte é `[project].dependencies` do `pyproject.toml` | `pyproject.toml` |

---

## 3. Arquitetura

Quatro peças, com uma decisão estrutural central: **o processo web nunca aplica o update
em si mesmo**. O endpoint só recebe, valida e agenda; quem aplica é um **updater
out-of-process** (Tarefa Agendada one-shot), porque:

1. `pip install -e` com deps novas pode falhar em DLLs/`.pyd` carregadas pelo processo em
   execução (lock de arquivo no Windows);
2. o apply exige parar o app — um processo não consegue se parar e continuar orquestrando;
3. filho spawnado pelo app pode morrer junto quando o Task Scheduler encerra a task.

### Componentes

```
[Browser admin via VPN]
   │ upload zip (multipart)
   ▼
[A. Rotas /api/admin/update/*  (app/web/routes_update.py — novo)]
   valida zip → grava staging em data/updates/staging/<id>/ → status "staged"
   │ POST apply
   ▼ Start-ScheduledTask "PortalPedidosUpdater"
[B. Updater  (scripts/apply-update.ps1 — novo; task one-shot, SYSTEM)]
   lock → backup → stop app → aplica arquivos → pip se deps mudaram
   → start app → health-check → OK ou rollback → escreve status
[C. Watchdog  (scripts/watchdog.ps1 — novo; task a cada 1 min, SYSTEM)]
   GET /health com timeout; N falhas seguidas → mata + religa o app
   (pausa enquanto data/updates/update.lock existir)
[D. UI admin  (app/web/static/admin-atualizacao.html — novo)]
   página /admin/atualizacao com upload, resumo do pacote, apply, progresso
```

### Arquivos novos / alterados

- **Novo** `app/web/routes_update.py` — `APIRouter(prefix="/api/admin/update")`, registrado
  em `app/web/server.py` como os routers existentes.
- **Novo** `app/updates/` (módulo puro-Python, testável sem FastAPI):
  - `package.py` — validação do zip (estrutura, manifesto, traversal, limites) e extração
    segura para staging;
  - `state.py` — leitura/escrita de `data/updates/status.json`, `update.lock`,
    `data/updates/history.jsonl`.
- **Novo** `scripts/apply-update.ps1` — o updater (fases, backup, rollback).
- **Novo** `scripts/watchdog.ps1` — health-check + restart.
- **Alterado** `scripts/setup-service.ps1` — passa a registrar TRÊS tasks:
  `PortalPedidos` (como hoje), `PortalPedidosUpdater` (on-demand, sem trigger) e
  `PortalPedidosWatchdog` (a cada 1 minuto). Idempotente como hoje.
- **Alterado** `scripts/uninstall-service.ps1` — remove as três tasks.
- **Alterado** `tools/build_package.sh` — gera `manifest.json` na raiz do pacote (§ 8).
- **Novo** `app/web/static/admin-atualizacao.html` + rota de página `GET /admin/atualizacao`
  (mesmo padrão de `/admin/ambientes`: estática, gate real nas APIs).
- **Alterado** `scripts/update.ps1` — permanece como caminho manual (RDP) de fallback;
  ganha só o pip condicional (§ 9). Não é removido.

### Layout em disco (novo, tudo fora do zip e preservado)

```
data/updates/
├── staging/<update_id>/portal-pedidos/   # zip extraído e validado, aguardando apply
├── status.json                            # estado corrente (fase, update_id, erro)
├── history.jsonl                          # 1 linha por tentativa (auditoria)
└── update.lock                            # existe = update em andamento (pausa watchdog)
backups/update/<update_id>/                # cópia da versão anterior p/ rollback
```

`data/` já é preservado por contrato; `backups/` já existe como conceito (`BACKUP_DIR`).

---

## 4. Fluxo passo-a-passo

### Fase 1 — Upload e validação (dentro do app, síncrono)

1. Admin abre `/admin/atualizacao`, seleciona o zip, envia para
   `POST /api/admin/update/upload`.
2. O handler streama o corpo para arquivo temporário em chunks (não carrega 100MB em RAM),
   abortando se exceder `MAX_PACKAGE_BYTES`.
3. `app/updates/package.py` valida (§ 6.2). Falhou → 4xx com motivo; staging apagado.
4. Passou → extrai para `data/updates/staging/<update_id>/`, computa `deps_changed`
   (§ 9) e responde o resumo: versão do manifesto, commit, nº de arquivos,
   `deps_changed`, `update_id`. Status vira `staged`.
5. Só existe **um** staged por vez: novo upload substitui o anterior.

### Fase 2 — Apply (fora do app, assíncrono)

6. Admin revisa o resumo na tela e chama `POST /api/admin/update/apply` com o `update_id`.
7. O handler confere: `update_id` corresponde ao staged; não há `update.lock`; a task
   `PortalPedidosUpdater` existe (senão 409 "serviço não configurado — rode
   setup-service.bat"). Grava `status.json` fase `apply_requested` e executa
   `Start-ScheduledTask PortalPedidosUpdater` (via `subprocess` → `schtasks /run`).
   Responde 202 imediatamente.
8. `apply-update.ps1` (SYSTEM, independente do processo web) executa, escrevendo
   `status.json` a cada fase:
   a. cria `update.lock` (watchdog pausa);
   b. re-valida o staging (manifesto presente, paths esperados);
   c. **backup**: copia `app/ scripts/ tools/ ui.py main.py pyproject.toml *.bat` para
      `backups/update/<update_id>/`;
   d. **stop**: `Stop-ScheduledTask PortalPedidos`; espera a porta 3636 liberar (até 30s);
      fallback: mata o PID dono da porta **somente se** o executável do processo estiver
      dentro de `<AppDir>\.venv\` (nunca mata processo alheio);
   e. **aplica**: remove `app/` **preservando `app/.secret.key`** e move o `app/` do
      staging para o lugar (evita módulos-fantasma de arquivos deletados entre versões);
      demais itens da allowlist são copiados por cima. `.env`, `data/`, `config.json`,
      `logs/`, `backups/`, `.venv/` nunca são tocados (o pacote não os contém — e o
      updater também os pula por deny-list, defesa em profundidade);
   f. **pip condicional**: se `deps_changed` → `.venv\Scripts\pip.exe install -e <AppDir>`;
      falha do pip → rollback (passo h);
   g. **start + health-check**: `Start-ScheduledTask PortalPedidos`; poll de
      `GET http://127.0.0.1:<porta>/health` (timeout 5s/tentativa) por até 120s.
      Saudável → status `succeeded` (com versão nova), grava `data/applied_update.json`,
      apaga staging e `update.lock`, poda backups antigos (retém os 2 últimos). Fim.
   h. **rollback**: restaura o backup por cima, roda `pip install -e` se deps tinham
      mudado, religa a task, health-check de novo. Sucesso → status
      `rolled_back` (com o erro original); falha → status `rollback_failed`
      (intervenção manual via RDP — caso limite documentado). Apaga `update.lock` sempre
      (finally).
9. A nova instância sobe e a **auto-migração idempotente** de schema roda na primeira
   conexão SQLite (`app/persistence/router.py::_apply_column_migrations`) — nada a fazer
   no updater.

### Fase 3 — Acompanhamento

10. A UI faz poll de `GET /api/admin/update/status`. Durante o restart o app está fora do
    ar: a UI trata erro de rede como "reiniciando..." e continua tentando (com teto de
    ~5 min antes de declarar timeout visual). Quando a nova instância responde, mostra
    `succeeded`/`rolled_back` lido do `status.json`.

---

## 5. Contrato das rotas

Todas com `Depends(require_admin)`. Router: `app/web/routes_update.py`,
`prefix="/api/admin/update"`, seguindo `routes_environments.py`.

### `POST /api/admin/update/upload`
- **Corpo:** multipart, campo `file` (`UploadFile`), somente `.zip`.
- **Limite:** `MAX_PACKAGE_BYTES = 100MB` (pacote atual é só código, ~poucos MB; 2× folga).
- **200:** `{"update_id", "version", "git_commit", "built_at", "files_count",
  "deps_changed": bool, "current_version"}`
- **Erros:** 400 extensão/zip corrompido; 413 tamanho; 422 validação do pacote (motivo
  legível: sem manifesto, path traversal, membro proibido, zip bomb...); 409 update em
  andamento (`update.lock` presente).

### `POST /api/admin/update/apply`
- **Corpo:** `{"update_id": str}`.
- **202:** `{"update_id", "status": "apply_requested"}`.
- **Erros:** 404 `update_id` não é o staged atual; 409 update em andamento; 409 task
  `PortalPedidosUpdater` não registrada (mensagem manda rodar `setup-service.bat`).

### `GET /api/admin/update/status`
- **200:** `{"status": "idle|staged|apply_requested|in_progress|succeeded|rolled_back|
  rollback_failed", "update_id"?, "phase"?, "error"?, "started_at"?, "finished_at"?,
  "current_version", "applied_at"?}`. Fonte: `status.json` + `applied_update.json` +
  fallback `pyproject.toml`. `phase` espelha os passos 8a–8h para a UI.

### `GET /admin/atualizacao`
- Página estática (`admin-atualizacao.html`), mesmo gate das outras páginas `/admin/*`
  (redirect para `/login` sem cookie; enforcement real nas APIs).

**Fora do contrato (YAGNI):** endpoint de "restart avulso" e endpoint de download de
pacote remoto. Restart fora de update é papel do watchdog; pacote chega por upload.

---

## 6. Segurança

### 6.1 Superfície
- Upload de zip que vira código executando como SYSTEM = **RCE por design** para quem
  tiver sessão admin. Mitigação em camadas: `require_admin` (bcrypt + sessão HttpOnly
  SameSite=Strict + rate-limit de login já existentes), exposição só na VPN/LAN (como o
  resto do portal), validação estrutural do pacote, e auditoria (`history.jsonl` +
  loguru com email do admin, `update_id`, sha256 do zip).
- Assinatura criptográfica do pacote (HMAC/Ed25519 com chave embarcada no build) fica
  como **decisão em aberto** (§ 14, #3) — v1 recomendada sem assinatura: o único emissor de
  pacote é o founder, o canal é VPN, e admin já é god-mode no produto (configura banco,
  diretórios, ambientes).

### 6.2 Validação do pacote (em `app/updates/package.py`)
1. Zip válido (`zipfile.ZipFile` + `testzip()`).
2. **Manifesto obrigatório**: `portal-pedidos/manifest.json` presente e com
   `{"name": "portal-pedidos", "version", "built_at", "git_commit", "deps_sha256"}`;
   `name` divergente → rejeita.
3. **Anti path-traversal**: todo membro precisa, após normalização, ficar sob a raiz
   `portal-pedidos/`; rejeita absolutos, `..`, drive letters (`C:\`), e symlinks
   (`external_attr` de link).
4. **Anti zip bomb**: caps de nº de membros (10.000) e tamanho descomprimido total
   (500MB), verificados ANTES de extrair.
5. **Allowlist de raiz**: só extrai membros cujo primeiro segmento sob `portal-pedidos/`
   está em `{app, scripts, tools, ui.py, main.py, pyproject.toml, .env.example, *.bat,
   README.md, INSTALACAO-SERVIDOR.md, manifest.json}` (espelho do `build_package.sh`).
6. **Deny-list de proteção** (defesa em profundidade — o build já exclui): rejeita o
   pacote inteiro se contiver `.env`, `*.db|sqlite*`, `.secret.key`, `config.json`,
   `firebird.json`, `data/`, `*.fdb|fbk|gbk`.
7. Extração com destino calculado por `Path.resolve()` + checagem `is_relative_to` do
   staging (nunca extração direta de nome de membro).

### 6.3 Preservação de segredos/dados
Garantida por três camadas independentes: build (allowlist + delete de segredos no
`build_package.sh`), validação (6.2 itens 5–6) e updater (deny-list no apply, 8e —
inclusive `app/.secret.key` explicitamente preservado no replace de `app/`).

### 6.4 Concorrência
- Single-flight: `update.lock` + status ≠ `in_progress` checados no upload e no apply.
- `MultipleInstances IgnoreNew` já protege a task `PortalPedidos` de instância dupla.

---

## 7. Restart no Windows — trade-offs e recomendação

O problema: a Scheduled Task só tem *restart on failure* (`RestartCount 5`, janela de
2 min). Um `sys.exit(0)` limpo **não** religa; exit ≠ 0 religa, mas no máximo 5 vezes e
nada cobre processo **pendurado** (o caso observado). Opções:

| Opção | Prós | Contras |
|---|---|---|
| **A. Scheduled Task + task watchdog (recomendada)** | Zero dependência nova; infra já instalada no cliente; o watchdog é necessário DE QUALQUER FORMA para o caso pendurado — e, existindo, também cobre crash, exit limpo e esgotamento do RestartCount; updater controla stop/start explicitamente (não depende de semântica de exit code) | Duas tasks a mais para gerir; granularidade mínima de 1 min |
| B. NSSM (app como serviço real) | Restart automático em qualquer exit, throttling nativo, gestão de serviço padrão | Binário third-party a embarcar (falsos-positivos de AV ocasionais); migração task→serviço no cliente; **não resolve o processo pendurado** — watchdog continuaria necessário; mais uma peça pra atualizar |
| C. Supervisor Python (wrapper que faz spawn/monitor do uvicorn) | Controle fino (podia observar o event loop) | Quem supervisiona o supervisor? Volta ao mesmo problema um nível acima; mais código nosso rodando 24/7; atualizar o supervisor durante o auto-update é um problema circular |

**Decisão recomendada: A.** O watchdog é obrigatório pelo requisito do processo pendurado;
uma vez que existe, ele subsume todos os cenários de restart — tornando NSSM e supervisor
redundantes. O restart do fluxo de update nunca depende de exit code: é sempre
`Stop-ScheduledTask` → apply → `Start-ScheduledTask`, comandado pelo updater.
Consequência: **não** mexemos na semântica de exit do app nem adicionamos
"exit não-zero forçado" — desnecessário com watchdog.

---

## 8. Watchdog por health-check

- **Onde roda:** Tarefa Agendada `PortalPedidosWatchdog`, SYSTEM, trigger a cada
  **1 minuto** (`Repeat` indefinido), executando `powershell -File scripts/watchdog.ps1`.
  NÃO roda no worker (`python -m app.worker` nem sequer tem task registrada nos scripts
  atuais) nem no processo web (é exatamente quem pode estar travado). PowerShell puro:
  não depende do `.venv` (que o update pode estar mexendo).
- **Checagem:** `Invoke-WebRequest http://127.0.0.1:<porta>/health` (porta lida do `.env`,
  default 3636) com **timeout de 10s**. `GET /health` já existe sem auth e é trivial —
  se o event loop está pendurado, ele não responde, que é o sinal desejado. Não é preciso
  criar endpoint novo.
- **Política:** contador de falhas consecutivas persistido em
  `data/updates/watchdog_state.json`. **3 falhas seguidas (~3 min)** →
  1. loga em `logs/watchdog.log`;
  2. `Stop-ScheduledTask PortalPedidos`; se a porta continuar ocupada, mata o PID dono da
     porta (via `Get-NetTCPConnection`) **somente se** o path do executável estiver sob
     `<AppDir>\.venv\`;
  3. `Start-ScheduledTask PortalPedidos`;
  4. zera o contador.
  Se a task nem está `Running` (crash + RestartCount esgotado, ou exit limpo), religa
  direto na primeira checagem.
- **Interação com o updater:** se `data/updates/update.lock` existe, o watchdog **no-op**
  (o app está legitimamente parado). Lock com mais de **30 min** é considerado órfão
  (updater morreu no meio): o watchdog o remove, loga, e volta a atuar — isso também é a
  rede de segurança para updater abortado entre stop e start.
- **Backoff anti-flap:** após um restart disparado pelo watchdog, espera 3 ciclos (3 min)
  antes de contar falhas de novo (a subida do uvicorn + imports leva dezenas de segundos
  na máquina do cliente).

---

## 9. `pip install -e` condicional

- Não existe lock file no projeto; a fonte de verdade é `[project].dependencies` (e
  `[project.optional-dependencies]`) do `pyproject.toml`.
- O `build_package.sh` grava no `manifest.json` o campo `deps_sha256` = SHA-256 do bloco
  de dependências normalizado (lista ordenada, sem whitespace).
- No upload, `app/updates/package.py` computa o mesmo hash do `pyproject.toml` **local**
  e compara → `deps_changed`, persistido no `status.json` do staged para o updater ler.
- Updater: `deps_changed=true` → roda `.venv\Scripts\pip.exe install -e <AppDir>` (após
  aplicar arquivos, app parado); `false` → pula (economiza ~1 min e evita rede à toa; o
  cliente pode estar sem saída pra PyPI — nesse caso deps novas exigem pacote wheelhouse,
  fora de escopo v1, ver § 14, #7).
- `scripts/update.ps1` (caminho manual) adota a mesma comparação para paridade.

---

## 10. Backup e rollback

- **Backup:** antes do apply, cópia física de `app/ scripts/ tools/ ui.py main.py
  pyproject.toml *.bat` para `backups/update/<update_id>/` (código é pequeno; cópia
  integral é mais simples e confiável que diff). `.venv` NÃO entra no backup — rollback
  de deps é refeito por `pip install -e` sobre o `pyproject.toml` restaurado.
- **Gatilhos de rollback:** falha na extração/apply, falha do pip, health-check da nova
  versão não passar em 120s.
- **Rollback:** restaurar backup por cima → `pip install -e` (se deps tinham mudado) →
  `Start-ScheduledTask` → health-check. Resultado vai para `status.json`
  (`rolled_back` | `rollback_failed`) e `history.jsonl`.
- **Retenção:** manter os **2** backups mais recentes; podar os demais ao fim de um
  update bem-sucedido.
- **Migração de schema no rollback:** as migrações são aditivas/idempotentes
  (`COLUMN_MIGRATIONS` só adiciona colunas); código antigo rodando sobre schema já
  migrado é compatível por construção — mesma garantia do processo manual de hoje.
  Migração destrutiva algum dia exigirá plano próprio (fora de escopo).

---

## 11. UI admin (`/admin/atualizacao`)

Mesmo padrão das páginas existentes (estática + shell: `tokens.css`, `shell.css`,
`shell.js`; item de menu no grupo Configurações, admin-only no shell, enforcement nas
APIs). Estados da tela:

1. **Idle:** versão atual (`current_version` + `applied_at`), dropzone para o zip.
2. **Staged:** card-resumo do pacote (versão, commit, data do build, nº de arquivos,
   badge "dependências mudaram — atualização ~2 min mais lenta"), botões
   "Aplicar atualização" (com confirm que avisa: *o portal ficará indisponível por
   1–3 minutos*) e "Descartar".
3. **Em andamento:** timeline das fases (backup → parando → aplicando → dependências →
   reiniciando → verificando) via poll do status; erro de rede durante o restart é
   renderizado como fase "reiniciando", não como falha.
4. **Resultado:** sucesso (nova versão) ou rollback (erro original em destaque,
   instrução de fallback manual).

---

## 12. Mudança no build (`tools/build_package.sh`)

Gerar `manifest.json` na raiz `portal-pedidos/` do stage, antes do zip:

```json
{
  "name": "portal-pedidos",
  "version": "20260714-1030",        // stamp AAAAMMDD-HHMM (fonte de verdade de versão)
  "built_at": "2026-07-14T10:30:00Z",
  "git_commit": "4902315",
  "deps_sha256": "<sha256 do bloco de dependências do pyproject>"
}
```

`version` = stamp de build (o `pyproject.toml` está parado em `0.1.0` e não versiona
releases; o stamp já é a convenção do nome do zip `portal-pedidos-AAAAMMDD.zip`).
Pacotes sem manifesto (antigos) são rejeitados pelo upload com mensagem clara —
o fluxo manual por RDP continua funcionando para eles.

---

## 13. Testes

**Unit (pytest, rodam no mac/CI) — `tests/test_update_package.py`:**
- validação do zip: manifesto ausente/`name` errado → rejeita; membro com `..`, path
  absoluto e drive letter → rejeita; membro fora da allowlist de raiz → rejeita;
  membro em deny-list (`.env`, `*.db`, `.secret.key`) → rejeita; zip bomb (caps de
  contagem/tamanho) → rejeita; pacote válido mínimo → extrai para staging e o conteúdo
  bate;
- `deps_sha256`: mesmo bloco de deps → `deps_changed=false`; dep adicionada/alterada →
  `true`; whitespace/ordem não afetam o hash;
- `state.py`: transições de status, lock presente bloqueia novo upload/apply, lock órfão
  (>30 min) detectável.

**Rotas (pytest + TestClient) — `tests/test_update_routes.py`:**
- `upload`/`apply`/`status` sem sessão → 401; com operador → 403 (padrão de
  `test_firebird_config_api.py`);
- upload > `MAX_PACKAGE_BYTES` → 413; não-zip → 400; zip inválido → 422 com motivo;
- upload válido → 200 com resumo; segundo upload substitui o staged;
- apply com `update_id` errado → 404; com lock presente → 409; sem a task registrada →
  409 (mock do disparo `schtasks`);
- status reflete `status.json` em cada estado.

**No Windows do cliente-teste (manual, checklist no PR — PowerShell não roda em CI):**
- `setup-service.bat` registra as 3 tasks; `desinstalar.bat` remove as 3;
- update feliz fim-a-fim pela UI (com e sem deps mudadas — conferir que o pip só roda
  quando mudou);
- rollback: pacote com `app/web/server.py` propositalmente quebrado → app não passa no
  health-check → volta a versão anterior sozinho;
- watchdog: (a) matar o `pythonw.exe` → religado em ≤2 min; (b) simular hang (`SIGSTOP`
  não existe no Windows — usar `Suspend-Process`/PsSuspend no processo) → religado em
  ~4 min; (c) durante um update (lock presente) o watchdog não interfere;
- preservação: após update, `.env`, `data/*.db`, `app/.secret.key`, `config.json`
  intactos (comparar hashes antes/depois).

---

## 14. Decisões a cravar no build (founder)

| # | Decisão | Recomendação nesta spec |
|---|---|---|
| 1 | Supervisor: Scheduled Task + watchdog vs NSSM vs supervisor Python | Scheduled Task + watchdog (§ 7) |
| 2 | Nome/forma das rotas: dois passos (`upload` → `apply`) vs um `POST /api/admin/update` único | Dois passos, com gate de revisão humana (§ 5) |
| 3 | Formato do manifesto e se assina o pacote (HMAC/Ed25519) | `manifest.json` simples, sem assinatura na v1 (§ 6.1, § 12) |
| 4 | Apply de `app/`: clean-replace (preservando `.secret.key`) vs extract-over-top (paridade com o manual) | Clean-replace — elimina módulos-fantasma (§ 4, 8e) |
| 5 | Parâmetros do watchdog: intervalo 1 min / 3 falhas / timeout 10s / lock órfão 30 min | Os valores citados (§ 8) |
| 6 | Retenção de backups (quantos) e local | 2 últimos em `backups/update/` (§ 10) |
| 7 | Cliente sem acesso a PyPI: exigir wheelhouse no pacote quando `deps_changed`? | Fora da v1; se o pip falhar, rollback automático cobre (§ 9) |
| 8 | Expor versão no `GET /health` (hoje não expõe) | Não — versão só no status admin, `/health` continua mínimo |

## 15. Fora de escopo (YAGNI explícito)

- Download automático de pacote de um servidor central / canal de release.
- Endpoint de restart avulso sem update.
- Assinatura de pacote (decisão #3 — pode entrar depois sem quebrar o formato).
- Task/serviço para o worker `python -m app.worker` (gap real, mas é outra feature).
- Migrações destrutivas de schema com downgrade coordenado.
- Barra de progresso em tempo real via WebSocket/SSE — poll de status basta.
