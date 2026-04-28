#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Registra o Portal de Pedidos como tarefa agendada do Windows (autostart).
.DESCRIPTION
    Cria uma tarefa no Agendador de Tarefas que inicia o servidor automaticamente
    com o Windows, sem precisar de usuario logado. Idempotente.
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
function Write-Fail([string]$Msg) { Write-Host ""; Write-Host "  [ERRO] $Msg" -ForegroundColor Red; Write-Host "" }

# ── [1/3] Verificar pre-requisitos ───────────────────────────────────────────

Write-Step "1/3" "Verificando pre-requisitos..."

$VenvPythonW = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$UiScript    = Join-Path $AppDir "ui.py"
$EnvFile     = Join-Path $AppDir ".env"

if (-not (Test-Path $VenvPythonW)) {
    Write-Fail "pythonw.exe nao encontrado em .venv\Scripts\."
    Write-Host "  Execute instalar.bat primeiro." -ForegroundColor White
    exit 1
}

if (-not (Test-Path $EnvFile)) {
    Write-Fail ".env nao encontrado."
    Write-Host "  Execute instalar.bat primeiro." -ForegroundColor White
    exit 1
}

Write-OK "Pre-requisitos OK."

# ── [2/3] Registrar tarefa agendada ──────────────────────────────────────────

Write-Step "2/3" "Registrando tarefa '$TaskName' no Agendador de Tarefas..."

# Remover tarefa anterior se existir (idempotente)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "        Removendo tarefa anterior..." -ForegroundColor Gray
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction `
    -Execute    $VenvPythonW `
    -Argument   "`"$UiScript`"" `
    -WorkingDirectory $AppDir

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit      ([TimeSpan]::Zero) `
    -RestartCount            5 `
    -RestartInterval         (New-TimeSpan -Minutes 2) `
    -StartWhenAvailable      $true `
    -MultipleInstances       IgnoreNew

# Rodar como SYSTEM para iniciar sem usuario logado
$principal = New-ScheduledTaskPrincipal `
    -UserId     "SYSTEM" `
    -LogonType  ServiceAccount `
    -RunLevel   Highest

$task = Register-ScheduledTask `
    -TaskName  $TaskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Principal $principal `
    -Force

Write-OK "Tarefa '$TaskName' registrada."

# ── [3/3] Iniciar agora ───────────────────────────────────────────────────────

Write-Step "3/3" "Iniciando o servidor..."

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running") {
    Write-OK "Servidor iniciado (estado: $state)."
} else {
    Write-Host "        Estado: $state (pode levar alguns segundos para subir)." -ForegroundColor Yellow
}

# Ler porta do .env
$port = "3636"
(Get-Content $EnvFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
    if ($_ -match "^PORTAL_PORT=(\d+)") { $port = $Matches[1] }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Servico configurado!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  O servidor inicia automaticamente com o Windows." -ForegroundColor White
Write-Host "  Acesso: http://localhost:$port" -ForegroundColor White
Write-Host ""
Write-Host "  Para verificar o status:" -ForegroundColor Gray
Write-Host "    Get-ScheduledTask -TaskName '$TaskName' | Select-Object State" -ForegroundColor Gray
Write-Host ""
Write-Host "  Para remover o autostart:" -ForegroundColor Gray
Write-Host "    Execute  desinstalar.bat  como Administrador." -ForegroundColor Gray
Write-Host ""
