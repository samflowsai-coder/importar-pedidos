#Requires -Version 5.1
<#
.SYNOPSIS
    Atualiza o Portal de Pedidos para a versao mais recente.
.DESCRIPTION
    Se o repositorio git estiver presente: executa git pull.
    Em seguida atualiza dependencias e reinicia o servico (se registrado).
    Idempotente.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir   = Split-Path -Parent $PSScriptRoot
$TaskName = "PortalPedidos"

function Write-Step([string]$N, [string]$Msg) {
    Write-Host ""
    Write-Host "  [$N] $Msg" -ForegroundColor Cyan
}

function Write-OK([string]$Msg)   { Write-Host "        OK — $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "        AVISO: $Msg" -ForegroundColor Yellow }
function Write-Fail([string]$Msg) { Write-Host ""; Write-Host "  [ERRO] $Msg" -ForegroundColor Red; Write-Host "" }

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

# ── [3/4] Atualizar dependencias ──────────────────────────────────────────────

Write-Step "3/4" "Atualizando dependencias..."

& $VenvPip install -e $AppDir --quiet --no-warn-script-location 2>&1 | Out-Null

if ($LASTEXITCODE -ne 0) {
    Write-Fail "Falha ao atualizar dependencias."
    exit 1
}
Write-OK "Dependencias atualizadas."

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
$envFile = Join-Path $AppDir ".env"
(Get-Content $envFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
    if ($_ -match "^PORTAL_PORT=(\d+)") { $port = $Matches[1] }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Atualizacao concluida!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Acesso: http://localhost:$port" -ForegroundColor White
Write-Host ""
