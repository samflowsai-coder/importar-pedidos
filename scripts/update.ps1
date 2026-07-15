#Requires -Version 5.1
<#
.SYNOPSIS
    Atualiza o Portal de Pedidos para a versao mais recente.
.DESCRIPTION
    Se o repositorio git estiver presente: executa git pull.
    Em seguida atualiza dependencias (pip condicional -- so roda se
    deps_sha256 do manifest.json do pacote novo mudou em relacao ao
    registrado em applied_update.json, paridade com o auto-updater
    scripts/apply-update.ps1) e reinicia o servico (se registrado).
    Idempotente.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir   = Split-Path -Parent $PSScriptRoot
$TaskName = "PortalPedidos"
$EnvFile  = Join-Path $AppDir ".env"

function Write-Step([string]$N, [string]$Msg) {
    Write-Host ""
    Write-Host "  [$N] $Msg" -ForegroundColor Cyan
}

function Write-OK([string]$Msg)   { Write-Host "        OK — $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "        AVISO: $Msg" -ForegroundColor Yellow }
function Write-Fail([string]$Msg) { Write-Host ""; Write-Host "  [ERRO] $Msg" -ForegroundColor Red; Write-Host "" }

# Le uma chave de um arquivo .env (regex ancorado por linha -- ignora
# comentarios e linhas em branco), desaspando o valor (aspas simples ou
# duplas). Copiada verbatim de scripts/apply-update.ps1 -- MESMA logica, para
# que o DataDir resolvido aqui seja identico ao do updater/watchdog (ver
# comentario completo em apply-update.ps1).
function Get-DotEnvValue {
    param([string]$EnvPath, [string]$Key)
    if (-not (Test-Path $EnvPath)) { return $null }
    $value = $null
    (Get-Content $EnvPath -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
        if ($_ -match "^$Key=(.+)$") {
            $v = $Matches[1].Trim()
            if ($v -match '^"(.*)"$') { $v = $Matches[1] }
            elseif ($v -match "^'(.*)'$") { $v = $Matches[1] }
            if ($v) { $value = $v }
        }
    }
    return $value
}

# Resolve o data dir EXATAMENTE como scripts/apply-update.ps1, scripts/watchdog.ps1
# e o web (app/web/routes_update.py::_data_dir()). Sem isso, um APP_DATA_DIR
# absoluto (deploy multi-ambiente, ex. D:\PortalData\MM) faria o update
# manual ler/gravar applied_update.json num lugar diferente do usado pelo
# auto-updater -- a decisao de pip condicional (e o registro apos o update)
# ficaria dessincronizada entre os dois caminhos.
$AppDataDirRaw = Get-DotEnvValue -EnvPath $EnvFile -Key "APP_DATA_DIR"
if ($AppDataDirRaw -and [System.IO.Path]::IsPathRooted($AppDataDirRaw)) {
    $DataDir = $AppDataDirRaw
} elseif ($AppDataDirRaw) {
    $DataDir = Join-Path $AppDir $AppDataDirRaw
} else {
    $DataDir = Join-Path $AppDir "data"
}

$ManifestPath      = Join-Path $AppDir "manifest.json"
$AppliedUpdatePath = Join-Path $DataDir "applied_update.json"

# Le um campo de um JSON, tolerante a arquivo ausente/corrompido (retorna
# $null nesses casos -- o chamador trata $null como "desconhecido").
function Get-JsonValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    try {
        $raw = Get-Content -Path $Path -Raw -Encoding UTF8 -ErrorAction Stop
        if (-not $raw) { return $null }
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($obj -is [System.Management.Automation.PSCustomObject] -and $obj.PSObject.Properties[$Key]) {
            return $obj.$Key
        }
        return $null
    } catch {
        return $null
    }
}

# Escreve um hashtable como JSON UTF-8 SEM BOM -- critico: Python
# `Path.read_text(encoding="utf-8")` (app/web/routes_update.py::_current_version)
# nao tolera BOM. Copiada verbatim de scripts/apply-update.ps1 (mesma razao).
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

# Grava/atualiza applied_update.json preservando campos existentes que nao
# fazem parte de $Fields (merge, mesma semantica de Write-Phase em
# apply-update.ps1) -- assim os dois caminhos (auto/manual) nunca se pisam.
function Write-AppliedUpdate {
    param([hashtable]$Fields)
    $cur = @{}
    if (Test-Path $AppliedUpdatePath) {
        try {
            $raw = Get-Content -Path $AppliedUpdatePath -Raw -Encoding UTF8
            if ($raw) {
                $obj = $raw | ConvertFrom-Json -ErrorAction Stop
                if ($obj -is [System.Management.Automation.PSCustomObject]) {
                    foreach ($prop in $obj.PSObject.Properties) { $cur[$prop.Name] = $prop.Value }
                }
            }
        } catch {
            $cur = @{}   # JSON corrompido -- comeca do zero, mesmo fallback do updater
        }
    }
    foreach ($k in $Fields.Keys) { $cur[$k] = $Fields[$k] }
    Write-JsonNoBom -Path $AppliedUpdatePath -Data $cur
}

$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
$VenvPip    = Join-Path $AppDir ".venv\Scripts\pip.exe"

if (-not (Test-Path $VenvPython)) {
    Write-Fail ".venv nao encontrado. Execute instalar.bat primeiro."
    exit 1
}

# ── [1/4] Parar servico (se registrado) ──────────────────────────────────────

Write-Step "1/4" "Verificando servico ativo..."

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$serviceWasRunning = $false

if ($task -and $task.State -eq "Running") {
    Write-Host "        Parando '$TaskName' para atualizar..." -ForegroundColor Gray
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    $serviceWasRunning = $true
    Write-OK "Servico parado."
} else {
    Write-OK "Servico nao estava ativo (ou nao registrado)."
}

# ── [2/4] Atualizar codigo-fonte (git pull) ───────────────────────────────────

Write-Step "2/4" "Atualizando codigo-fonte..."

$isGit = Test-Path (Join-Path $AppDir ".git")
if ($isGit) {
    $gitOk = $false
    try { $null = & git --version 2>&1; $gitOk = ($LASTEXITCODE -eq 0) } catch {}

    if ($gitOk) {
        Push-Location $AppDir
        try {
            $pullOut = & git pull 2>&1
            if ($LASTEXITCODE -ne 0) {
                Write-Warn "git pull retornou erro: $pullOut"
                Write-Host "        Continuando com as dependencias existentes..." -ForegroundColor Gray
            } else {
                Write-OK "Codigo atualizado: $($pullOut | Select-Object -Last 1)"
            }
        } finally {
            Pop-Location
        }
    } else {
        Write-Warn "git nao encontrado no PATH — pulando atualizacao de codigo."
    }
} else {
    Write-OK "Nao e um repositorio git — pulando git pull."
}

# ── [3/4] Atualizar dependencias (pip condicional) ────────────────────────────

Write-Step "3/4" "Verificando dependencias..."

# Paridade com scripts/apply-update.ps1 (fase "pip"): so reinstala se o
# deps_sha256 do manifest.json do pacote novo (gravado por tools/build_package.sh
# na raiz do pacote -- ja presente em $AppDir apos o usuario extrair o zip por
# cima, ANTES de rodar este script) mudou em relacao ao ultimo registrado em
# applied_update.json. Ausencia/erro de leitura em qualquer um dos dois lados
# faz o pip RODAR (default seguro) -- so pula quando os dois hashes existem
# e sao iguais.
$manifestDeps    = Get-JsonValue -Path $ManifestPath -Key "deps_sha256"
$manifestVersion = Get-JsonValue -Path $ManifestPath -Key "version"
$manifestCommit  = Get-JsonValue -Path $ManifestPath -Key "git_commit"
$appliedDeps     = Get-JsonValue -Path $AppliedUpdatePath -Key "deps_sha256"

$runPip = $true
if (-not (Test-Path $ManifestPath)) {
    $pipReason = "manifest.json nao encontrado em '$AppDir' -- nao e possivel comparar deps, pip executado (default seguro)."
} elseif (-not $manifestDeps) {
    $pipReason = "manifest.json sem campo deps_sha256 -- pip executado (default seguro)."
} elseif (-not $appliedDeps) {
    $pipReason = "applied_update.json ausente ou sem deps_sha256 (primeira atualizacao rastreada) -- pip executado (default seguro)."
} elseif ($manifestDeps -ne $appliedDeps) {
    $pipReason = "deps_sha256 mudou -- pip executado."
} else {
    $runPip = $false
    $pipReason = "deps inalteradas, pip pulado."
}

if ($runPip) {
    Write-Host "        $pipReason" -ForegroundColor Gray
    & $VenvPip install -e $AppDir --quiet --no-warn-script-location 2>&1 | Out-Null

    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Falha ao atualizar dependencias."
        exit 1
    }
    Write-OK "Dependencias atualizadas."
} else {
    Write-OK $pipReason
}

# Registra o estado desta atualizacao para a PROXIMA decisao de pip
# condicional (deste script ou do auto-updater -- mesmo arquivo, mesmo
# $DataDir, mesmo formato). So grava quando ha manifest.json novo para
# extrair version/git_commit/deps_sha256; sem ele (ex.: fluxo git sem
# pacote) nao ha dado novo confiavel -- preserva o que ja estava registrado.
if (Test-Path $ManifestPath) {
    Write-AppliedUpdate -Fields @{
        version     = $manifestVersion
        git_commit  = $manifestCommit
        deps_sha256 = $manifestDeps
        applied_at  = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    }
    Write-OK "Estado registrado em applied_update.json (versao $manifestVersion)."
}

# ── [4/4] Reiniciar servico ───────────────────────────────────────────────────

Write-Step "4/4" "Reiniciando servico..."

$taskAfter = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($taskAfter) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-OK "Servico reiniciado (estado: $state)."
} elseif ($serviceWasRunning) {
    Write-Warn "Tarefa agendada nao encontrada para reiniciar. Use iniciar.bat."
} else {
    Write-OK "Servico nao estava registrado — nada a reiniciar."
}

$port = "3636"
(Get-Content $EnvFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
    if ($_ -match "^PORTAL_PORT=(\d+)") { $port = $Matches[1] }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Atualizacao concluida!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Acesso: http://localhost:$port" -ForegroundColor White
Write-Host ""
