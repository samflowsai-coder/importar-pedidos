#Requires -Version 5.1
<#
.SYNOPSIS
    Updater out-of-process do Portal de Pedidos.
.DESCRIPTION
    Executado pela Tarefa Agendada one-shot "PortalPedidosUpdater" (SYSTEM),
    disparada por POST /api/admin/update/apply (app/web/routes_update.py).
    Roda SEMPRE fora do processo web: o app nao pode parar/atualizar a si
    mesmo (pip install -e trava DLL/.pyd do processo em execucao; o apply
    exige parar o app; um filho spawnado pelo app morre junto quando a Task
    Scheduler encerra a task pai).

    Fases (spec docs/superpowers/specs/2026-07-14-auto-update-endpoint-design.md
    Secao 4 passo 8 / Secao 8 / Secao 10):
      lock -> revalida staging -> backup -> stop -> apply (clean-replace app/)
      -> pip condicional -> start -> health-check -> succeeded | rollback

    Contrato de fases (docs/ai + admin-atualizacao.html casam por substring,
    case-insensitive, primeiro hit vence -- strings abaixo sao as usadas
    literalmente neste script):
      backup | stop | apply | pip | start | healthcheck
    Status terminais: succeeded | rolled_back | rollback_failed
    (in_progress durante a execucao; apply_requested/staged/idle sao
    escritos pela camada web, nao por este script).

    Le:   data/updates/status.json (update_id, deps_changed)
          data/updates/staging/<id>/portal-pedidos/  (manifest.json, app/, ...)
          .env (PORTAL_PORT)
    Escreve: data/updates/status.json, data/updates/update.lock,
             backups/update/<id>/, data/applied_update.json,
             data/updates/history.jsonl, logs/update-apply.log
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest sem barra de progresso (evita lentidao)

# ── Caminhos ──────────────────────────────────────────────────────────────────

$AppDir       = Split-Path -Parent $PSScriptRoot
$DataDir      = Join-Path $AppDir "data"
$Updates      = Join-Path $DataDir "updates"
$StagingRoot  = Join-Path $Updates "staging"
$BackupsRoot  = Join-Path $AppDir "backups\update"
$LockPath     = Join-Path $Updates "update.lock"
$StatusPath   = Join-Path $Updates "status.json"
$HistoryPath  = Join-Path $Updates "history.jsonl"
$LogPath      = Join-Path $AppDir "logs\update-apply.log"
$AppTaskName  = "PortalPedidos"

# Allowlist de membros (fora do app/, que tem tratamento clean-replace
# proprio). Mesma lista usada no backup e na aplicacao/rollback -- uma unica
# fonte de verdade para nao divergir entre "o que eu guardo" e "o que eu
# aplico". *.bat e descoberto dinamicamente (nomes variam por cliente).
$AllowlistDirs  = @("scripts", "tools")
$AllowlistFiles = @("ui.py", "main.py", "pyproject.toml")

# Flags de controle (setados durante a execucao; ver Hard Constraints no PR).
# Dois sinais DISTINTOS para o rollback decidir o que precisa ser desfeito:
#   AppStopAttempted -- tentamos parar a task/processo (o servico pode estar
#                        fora do ar mesmo sem nenhum arquivo ter mudado).
#   FilesModified     -- Apply-StagedFiles comecou a mexer em app/ (o
#                        conteudo em disco pode estar parcial/diferente do
#                        backup).
# Sao independentes: por ex., se a task nao esta registrada, Stop-PortalApp
# nao aciona AppStopAttempted mas o fluxo segue para Apply-StagedFiles, que
# aciona FilesModified. O rollback trata os dois.
$script:LockAcquired     = $false
$script:AppStopAttempted = $false
$script:FilesModified    = $false

# ── Logging (best-effort; nunca deve derrubar o updater) ─────────────────────

function Write-Log {
    param([string]$Message)
    try {
        $logDir = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Path $logDir -Force -ErrorAction SilentlyContinue | Out-Null
        $line = "[$(Get-Date -Format o)] $Message"
        Add-Content -Path $LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {
        # log e diagnostico, nunca deve interromper o fluxo do updater
    }
}

# ── Tempo ─────────────────────────────────────────────────────────────────────

function Get-UnixTime {
    return [int64](([DateTimeOffset]::UtcNow).ToUnixTimeSeconds())
}

function Get-IsoNow {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

# ── status.json (merge semantics equivalentes a app/updates/state.py) ────────
# state.write_status() faz cur.update(fields) -- so sobrescreve as chaves
# passadas, preservando as demais (update_id, version, deps_changed setados
# pelo upload). Replicamos aqui para nao perder esses campos quando o
# updater assume a escrita do arquivo.

function Read-StatusRaw {
    $result = @{}
    if (Test-Path $StatusPath) {
        try {
            $raw = Get-Content -Path $StatusPath -Raw -Encoding UTF8
            if ($raw) {
                $obj = $raw | ConvertFrom-Json -ErrorAction Stop
                if ($obj -is [System.Management.Automation.PSCustomObject]) {
                    foreach ($prop in $obj.PSObject.Properties) { $result[$prop.Name] = $prop.Value }
                }
            }
        } catch {
            $result = @{}   # JSON corrompido -- mesmo fallback de state.read_status()
        }
    }
    return $result
}

# Escreve um hashtable como JSON UTF-8 SEM BOM (critico: Python
# `Path.read_text(encoding="utf-8")` nao tolera BOM -- json.loads quebraria
# com "Expecting value: line 1 column 1"). Set-Content -Encoding UTF8 do
# Windows PowerShell 5.1 grava BOM por padrao, entao usamos .NET direto.
function Write-JsonNoBom {
    param([string]$Path, [hashtable]$Data)
    $dir = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
    $json = $Data | ConvertTo-Json -Depth 10 -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $tmp = Join-Path $dir ([Guid]::NewGuid().ToString("N") + ".tmp")
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    Move-Item -Path $tmp -Destination $Path -Force
}

function Write-Phase {
    param(
        [Parameter(Mandatory = $true)][string]$Status,
        [string]$Phase = $null,
        [hashtable]$Extra = @{}
    )
    $cur = Read-StatusRaw
    $cur["status"] = $Status
    if ($Phase) { $cur["phase"] = $Phase }
    foreach ($k in $Extra.Keys) { $cur[$k] = $Extra[$k] }
    Write-JsonNoBom -Path $StatusPath -Data $cur
    Write-Log "status=$Status phase=$Phase"
}

function Append-History {
    param([hashtable]$Entry)
    New-Item -ItemType Directory -Path $Updates -Force -ErrorAction SilentlyContinue | Out-Null
    $Entry["ts"] = Get-IsoNow
    $line = ($Entry | ConvertTo-Json -Depth 10 -Compress)
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($HistoryPath, $line + "`n", $utf8NoBom)
}

# ── .env / porta ──────────────────────────────────────────────────────────────

function Get-PortalPort {
    $port = 3636
    $envFile = Join-Path $AppDir ".env"
    if (Test-Path $envFile) {
        (Get-Content $envFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
            if ($_ -match "^PORTAL_PORT=(\d+)") { $port = [int]$Matches[1] }
        }
    }
    return [int]$port
}

# ── Validacao do staging ──────────────────────────────────────────────────────

function Assert-StagingValid {
    param([string]$StagingPath, [string]$UpdateId)
    if ($UpdateId -notmatch '^[A-Za-z0-9_-]+$') {
        throw "update_id com caracteres inesperados: '$UpdateId'"
    }
    if (-not (Test-Path $StagingPath)) {
        throw "staging nao encontrado para update_id '$UpdateId': $StagingPath"
    }
    $manifestPath = Join-Path $StagingPath "manifest.json"
    if (-not (Test-Path $manifestPath)) {
        throw "manifest.json ausente no staging: $manifestPath"
    }
    $appPath = Join-Path $StagingPath "app"
    if (-not (Test-Path $appPath)) {
        throw "diretorio app/ ausente no staging: $appPath"
    }
}

function Get-StagedManifest {
    param([string]$StagingPath)
    $manifestPath = Join-Path $StagingPath "manifest.json"
    try {
        $raw = Get-Content -Path $manifestPath -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "manifest.json invalido em $manifestPath -- $($_.Exception.Message)"
    }
    foreach ($field in @("version", "git_commit")) {
        if (-not $obj.PSObject.Properties[$field] -or -not $obj.$field) {
            throw "manifest.json em $manifestPath sem campo obrigatorio: $field"
        }
    }
    return $obj
}

# ── Backup / restore ──────────────────────────────────────────────────────────

function Backup-CurrentInstall {
    param([string]$Destination)
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    foreach ($name in (@("app") + $AllowlistDirs)) {
        $src = Join-Path $AppDir $name
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $Destination $name) -Recurse -Force
        }
    }
    foreach ($name in $AllowlistFiles) {
        $src = Join-Path $AppDir $name
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $Destination $name) -Force
        }
    }
    Get-ChildItem -Path $AppDir -Filter "*.bat" -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $Destination $_.Name) -Force
    }
}

# Preserva app/.secret.key ao redor de um clean-replace de app/: copia para
# um arquivo temporario ANTES do Remove-Item/Move-Item, restaura DEPOIS.
# Duas funcoes simples (nao um scriptblock passado por parametro) de
# proposito: um scriptblock invocado via `&` dentro de outra funcao roda
# com o escopo do CHAMADOR como pai (PowerShell nao faz closure lexica por
# padrao), entao nao enxergaria variaveis locais da funcao que o criou --
# um bug sutil de escopo que preferimos nao introduzir aqui.
# Nunca toca em .env, data/, config.json, firebird.json, logs/, backups/, .venv/.
function Backup-SecretKey {
    $secretSrc = Join-Path $AppDir "app\.secret.key"
    if (-not (Test-Path $secretSrc)) { return $null }
    $secretTmp = Join-Path ([System.IO.Path]::GetTempPath()) ("secret.key." + [Guid]::NewGuid().ToString("N"))
    Copy-Item -Path $secretSrc -Destination $secretTmp -Force
    return $secretTmp
}

function Restore-SecretKey {
    param([string]$SecretTmp)
    if (-not $SecretTmp) { return }
    try {
        $appDest = Join-Path $AppDir "app"
        Copy-Item -Path $SecretTmp -Destination (Join-Path $appDest ".secret.key") -Force
    } finally {
        Remove-Item -Path $SecretTmp -Force -ErrorAction SilentlyContinue
    }
}

# Copia demais membros da allowlist "por cima" (merge, nao clean-replace --
# decisao #4 da spec so cobre app/, para eliminar modulos-fantasma; scripts/
# tools/arquivos soltos sao simples o bastante para so sobrescrever).
function Copy-AllowlistOverTop {
    param([string]$SourceRoot)
    foreach ($name in $AllowlistDirs) {
        $src = Join-Path $SourceRoot $name
        if (Test-Path $src) {
            $dst = Join-Path $AppDir $name
            New-Item -ItemType Directory -Path $dst -Force -ErrorAction SilentlyContinue | Out-Null
            Copy-Item -Path (Join-Path $src '*') -Destination $dst -Recurse -Force
        }
    }
    foreach ($name in $AllowlistFiles) {
        $src = Join-Path $SourceRoot $name
        if (Test-Path $src) {
            Copy-Item -Path $src -Destination (Join-Path $AppDir $name) -Force
        }
    }
    Get-ChildItem -Path $SourceRoot -Filter "*.bat" -File -ErrorAction SilentlyContinue | ForEach-Object {
        Copy-Item -Path $_.FullName -Destination (Join-Path $AppDir $_.Name) -Force
    }
}

# Aplica o pacote staged: clean-replace de app/ (remove + move, evita
# modulos-fantasma de arquivos removidos entre versoes) preservando
# .secret.key, depois copia o resto da allowlist por cima.
function Apply-StagedFiles {
    param([string]$StagingPath)
    $stagedApp = Join-Path $StagingPath "app"
    if (-not (Test-Path $stagedApp)) { throw "pacote staged sem diretorio app/: $stagedApp" }

    # A partir daqui o conteudo de app/ (e depois o resto da allowlist) pode
    # ficar parcial/diferente do backup -- se qualquer coisa falhar dai pra
    # frente, o rollback PRECISA restaurar o backup, mesmo que Stop-PortalApp
    # nunca tenha achado a task (ex.: task nao registrada mas porta livre).
    $script:FilesModified = $true

    $secretTmp = Backup-SecretKey
    $appDest = Join-Path $AppDir "app"
    if (Test-Path $appDest) { Remove-Item -Path $appDest -Recurse -Force }
    Move-Item -Path $stagedApp -Destination $appDest
    Restore-SecretKey -SecretTmp $secretTmp

    Copy-AllowlistOverTop -SourceRoot $StagingPath
}

# Restaura o backup por cima (rollback). Usa Copy (nao Move) para preservar
# o backup intacto -- ele so e podado apos um SUCESSO subsequente.
function Restore-Backup {
    param([string]$BackupDir)
    $backupApp = Join-Path $BackupDir "app"
    if (-not (Test-Path $backupApp)) { throw "backup incompleto/ausente (sem app/): $backupApp" }

    $secretTmp = Backup-SecretKey
    $appDest = Join-Path $AppDir "app"
    if (Test-Path $appDest) { Remove-Item -Path $appDest -Recurse -Force }
    Copy-Item -Path $backupApp -Destination $appDest -Recurse -Force
    Restore-SecretKey -SecretTmp $secretTmp

    Copy-AllowlistOverTop -SourceRoot $BackupDir
}

function Remove-OldBackups {
    param([int]$Keep = 2)
    if (-not (Test-Path $BackupsRoot)) { return }
    $dirs = Get-ChildItem -Path $BackupsRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object CreationTime -Descending
    if (-not $dirs -or $dirs.Count -le $Keep) { return }
    $dirs | Select-Object -Skip $Keep | ForEach-Object {
        Write-Log "Podando backup antigo: $($_.FullName)"
        Remove-Item -Path $_.FullName -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ── Stop / start / health ─────────────────────────────────────────────────────

# Para a task PortalPedidos e espera a porta liberar (ate 30s). Fallback:
# mata o processo dono da porta SOMENTE se o executavel estiver sob
# <AppDir>\.venv\ (nunca mata processo alheio). Marca $script:AppStopAttempted
# assim que tenta parar (nao so quando confirma sucesso) -- a partir desse
# ponto o SERVICO nao pode mais ser considerado "intocado" (pode estar
# parado), entao um rollback subsequente deve sempre tentar religar, mesmo
# se esta funcao lancar por timeout.
function Stop-PortalApp {
    param([switch]$IgnoreErrors)
    $task = Get-ScheduledTask -TaskName $AppTaskName -ErrorAction SilentlyContinue
    if ($task) {
        $script:AppStopAttempted = $true
        try {
            Stop-ScheduledTask -TaskName $AppTaskName -ErrorAction Stop
        } catch {
            Write-Log "Stop-ScheduledTask '$AppTaskName': $($_.Exception.Message)"
        }
    }

    $port = Get-PortalPort
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if (-not $conns) { return }
        Start-Sleep -Milliseconds 500
    }

    $venvPrefix = Join-Path $AppDir ".venv\"
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $procPath = $null
        try {
            $proc = Get-Process -Id $c.OwningProcess -ErrorAction Stop
            $procPath = $proc.Path
        } catch {
            continue
        }
        if ($procPath -and $procPath.StartsWith($venvPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Log "Matando PID $($c.OwningProcess) ($procPath) para liberar a porta $port."
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        } else {
            Write-Log "Porta $port ocupada por PID $($c.OwningProcess) fora de .venv ($procPath) -- NAO matando (processo alheio)."
        }
    }

    Start-Sleep -Seconds 1
    $stillBound = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($stillBound -and -not $IgnoreErrors) {
        throw "porta $port continua ocupada apos parar a tarefa e tentar liberar o processo (.venv)"
    }
}

function Start-PortalApp {
    $task = Get-ScheduledTask -TaskName $AppTaskName -ErrorAction SilentlyContinue
    if (-not $task) { throw "tarefa '$AppTaskName' nao registrada -- rode setup-service.bat no servidor" }
    if ($task.State -ne "Running") {
        Start-ScheduledTask -TaskName $AppTaskName
    }
}

function Wait-Healthy {
    param([int]$TimeoutSec = 120)
    $port = Get-PortalPort
    $uri = "http://127.0.0.1:$port/health"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $uri -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { return $true }
        } catch {
            # ainda subindo, ou pendurado (nao responde) -- tenta de novo ate o timeout
        }
        Start-Sleep -Seconds 3
    }
    return $false
}

function Install-Dependencies {
    $pip = Join-Path $AppDir ".venv\Scripts\pip.exe"
    if (-not (Test-Path $pip)) { throw "pip.exe nao encontrado em $pip" }
    $out = & $pip install -e $AppDir --no-warn-script-location 2>&1
    if ($LASTEXITCODE -ne 0) {
        $tail = (($out | Select-Object -Last 20) -join " | ")
        throw "pip install -e falhou (exit $LASTEXITCODE): $tail"
    }
}

# ── Rollback ───────────────────────────────────────────────────────────────────

function Invoke-Rollback {
    param(
        [string]$UpdateId,
        [bool]$DepsChanged,
        [string]$OriginalError,
        $StartedAt,
        [string]$BackupDir
    )

    if (-not $script:AppStopAttempted -and -not $script:FilesModified) {
        # Falha ocorreu antes de qualquer tentativa de parar o app e antes de
        # tocar em qualquer arquivo (ex.: status.json sem update_id, staging
        # invalido/ausente, falha no backup fisico). Nada foi alterado -- o
        # app segue rodando a versao anterior sem interrupcao. Nao ha o que
        # restaurar nem por que reiniciar.
        Write-Log "Rollback trivial: falha antes do stop/apply, nada foi alterado. Erro original: $OriginalError"
        Write-Phase -Status "rolled_back" -Phase "healthcheck" -Extra @{
            error = $OriginalError; finished_at = (Get-UnixTime)
        }
        Append-History @{
            update_id = $UpdateId; result = "rolled_back"; error = $OriginalError
            note = "falha antes do stop/apply -- nada alterado"; started_at = $StartedAt
            finished_at = (Get-UnixTime)
        }
        return
    }

    Write-Phase -Status "in_progress" -Phase "stop" -Extra @{ error = $OriginalError }
    try {
        Stop-PortalApp -IgnoreErrors   # idempotente -- garante estado parado antes de sobrescrever

        if ($script:FilesModified) {
            if (-not (Test-Path (Join-Path $BackupDir "app"))) {
                throw "backup incompleto/ausente em '$BackupDir' -- impossivel restaurar automaticamente"
            }

            Write-Phase -Status "in_progress" -Phase "apply" -Extra @{ error = $OriginalError }
            Restore-Backup -BackupDir $BackupDir

            if ($DepsChanged) {
                Write-Phase -Status "in_progress" -Phase "pip" -Extra @{ error = $OriginalError }
                Install-Dependencies
            }
        } else {
            Write-Log "Arquivos nao foram modificados (falha ocorreu antes do apply) -- pulando restore/pip, so religando o app."
        }

        Write-Phase -Status "in_progress" -Phase "start" -Extra @{ error = $OriginalError }
        Start-PortalApp

        Write-Phase -Status "in_progress" -Phase "healthcheck" -Extra @{ error = $OriginalError }
        $healthy = Wait-Healthy -TimeoutSec 120
        if (-not $healthy) { throw "health-check da versao restaurada tambem falhou" }

        Write-Phase -Status "rolled_back" -Phase "healthcheck" -Extra @{
            error = $OriginalError; finished_at = (Get-UnixTime)
        }
        Append-History @{
            update_id = $UpdateId; result = "rolled_back"; error = $OriginalError
            started_at = $StartedAt; finished_at = (Get-UnixTime)
        }
        Write-Log "Rollback concluido com sucesso. Erro original: $OriginalError"
    } catch {
        $rollbackError = $_.Exception.Message
        Write-Log "ROLLBACK FALHOU: $rollbackError (erro original: $OriginalError) -- intervencao manual necessaria"
        Write-Phase -Status "rollback_failed" -Phase "healthcheck" -Extra @{
            error = $OriginalError; rollback_error = $rollbackError; finished_at = (Get-UnixTime)
        }
        Append-History @{
            update_id = $UpdateId; result = "rollback_failed"; error = $OriginalError
            rollback_error = $rollbackError; started_at = $StartedAt; finished_at = (Get-UnixTime)
        }
    }
}

# ── Execucao principal ────────────────────────────────────────────────────────

Write-Log "==== apply-update.ps1 iniciado (PID $PID) ===="

# 1) Lock -- guarda atomica. New-Item falha se o arquivo ja existe: e assim
#    que detectamos uma segunda instancia concorrente. CRITICO: se falhar
#    aqui, saimos IMEDIATAMENTE sem tocar no lock nem no status.json
#    existentes -- eles pertencem a primeira instancia. So marcamos
#    $script:LockAcquired = $true DEPOIS do New-Item ter sucesso; o finally
#    mais abaixo so remove o lock se ESTA instancia o criou.
try {
    New-Item -ItemType Directory -Path $Updates -Force -ErrorAction Stop | Out-Null
    New-Item -ItemType File -Path $LockPath -Value "pid=$PID;started_at=$(Get-IsoNow)" -ErrorAction Stop | Out-Null
    $script:LockAcquired = $true
    Write-Log "Lock adquirido: $LockPath"
} catch {
    Write-Log "NAO foi possivel adquirir o lock (outra instancia rodando, ou lock orfao) -- encerrando sem tocar em status/lock existentes. $($_.Exception.Message)"
    exit 1
}

$updateId   = $null
$depsChanged = $false
$startedAt  = $null
$backupDir  = $null

try {
    # 2) Le update_id / deps_changed de status.json (escrito pelo /apply)
    $statusData = Read-StatusRaw
    $updateId = $statusData["update_id"]
    if (-not $updateId) { throw "status.json sem update_id -- nada para aplicar" }
    $depsChanged = ($statusData["deps_changed"] -eq $true)

    $staging = Join-Path (Join-Path $StagingRoot $updateId) "portal-pedidos"
    $backupDir = Join-Path $BackupsRoot $updateId
    $startedAt = Get-UnixTime

    # 3) Fase "backup" (cobre lock + revalidacao + backup fisico -- um unico
    #    passo na timeline da UI)
    Write-Phase -Status "in_progress" -Phase "backup" -Extra @{
        update_id = $updateId; started_at = $startedAt; error = $null
        rollback_error = $null; finished_at = $null
    }

    Assert-StagingValid -StagingPath $staging -UpdateId $updateId
    $manifest = Get-StagedManifest -StagingPath $staging

    Backup-CurrentInstall -Destination $backupDir
    Write-Log "Backup criado em $backupDir"

    # 4) Fase "stop"
    Write-Phase -Status "in_progress" -Phase "stop"
    Stop-PortalApp
    Write-Log "App parado."

    # 5) Fase "apply"
    Write-Phase -Status "in_progress" -Phase "apply"
    Apply-StagedFiles -StagingPath $staging
    Write-Log "Arquivos aplicados (app/ substituido, allowlist copiada por cima)."

    # 6) Fase "pip" (condicional)
    if ($depsChanged) {
        Write-Phase -Status "in_progress" -Phase "pip"
        Install-Dependencies
        Write-Log "Dependencias reinstaladas (deps_changed=true)."
    } else {
        Write-Log "deps_changed=false -- pip nao executado."
    }

    # 7) Fase "start" + "healthcheck"
    Write-Phase -Status "in_progress" -Phase "start"
    Start-PortalApp
    Write-Log "App reiniciado, aguardando health-check."

    Write-Phase -Status "in_progress" -Phase "healthcheck"
    $healthy = Wait-Healthy -TimeoutSec 120
    if (-not $healthy) { throw "health-check nao respondeu 200 em /health apos 120s" }

    # 8) Sucesso
    $appliedAt = Get-IsoNow
    $appliedUpdatePath = Join-Path $DataDir "applied_update.json"
    Write-JsonNoBom -Path $appliedUpdatePath -Data @{
        version = $manifest.version; git_commit = $manifest.git_commit; applied_at = $appliedAt
    }

    Write-Phase -Status "succeeded" -Phase "healthcheck" -Extra @{
        version = $manifest.version; git_commit = $manifest.git_commit; applied_at = $appliedAt
        error = $null; rollback_error = $null; finished_at = (Get-UnixTime)
    }

    Remove-Item -Path (Join-Path $StagingRoot $updateId) -Recurse -Force -ErrorAction SilentlyContinue
    Remove-OldBackups -Keep 2
    Append-History @{
        update_id = $updateId; result = "succeeded"; version = $manifest.version
        git_commit = $manifest.git_commit; started_at = $startedAt; finished_at = (Get-UnixTime)
    }
    Write-Log "Atualizacao concluida com sucesso: versao $($manifest.version) ($($manifest.git_commit))."

} catch {
    $originalError = $_.Exception.Message
    Write-Log "ERRO durante a aplicacao: $originalError"
    Invoke-Rollback -UpdateId $updateId -DepsChanged $depsChanged -OriginalError $originalError `
        -StartedAt $startedAt -BackupDir $backupDir
} finally {
    # CRITICO: so remove o lock se ESTA instancia o criou (Hard Constraint 2).
    # Uma segunda instancia cujo New-Item falhou nunca chega a este bloco
    # (saiu no "exit 1" acima), entao nunca apaga o lock da primeira.
    if ($script:LockAcquired) {
        Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
        Write-Log "Lock liberado."
    }
    Write-Log "==== apply-update.ps1 finalizado ===="
}
