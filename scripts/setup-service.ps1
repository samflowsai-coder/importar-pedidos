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

$AppDir           = Split-Path -Parent $PSScriptRoot
$TaskName         = "PortalPedidos"
$UpdaterTaskName  = "PortalPedidosUpdater"
$WatchdogTaskName = "PortalPedidosWatchdog"

. (Join-Path $PSScriptRoot "network.ps1")

function Write-Step([string]$N, [string]$Msg) {
    Write-Host ""
    Write-Host "  [$N] $Msg" -ForegroundColor Cyan
}

function Write-OK([string]$Msg)   { Write-Host "        OK - $Msg" -ForegroundColor Green }
function Write-Fail([string]$Msg) { Write-Host ""; Write-Host "  [ERRO] $Msg" -ForegroundColor Red; Write-Host "" }

# -- [1/4] Verificar pre-requisitos -------------------------------------------

Write-Step "1/4" "Verificando pre-requisitos..."

$VenvPythonW       = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$UiScript          = Join-Path $AppDir "ui.py"
$EnvFile           = Join-Path $AppDir ".env"
$ApplyUpdateScript = Join-Path $PSScriptRoot "apply-update.ps1"
$WatchdogScript    = Join-Path $PSScriptRoot "watchdog.ps1"

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

if (-not (Test-Path $ApplyUpdateScript)) {
    Write-Fail "scripts\apply-update.ps1 nao encontrado."
    exit 1
}

if (-not (Test-Path $WatchdogScript)) {
    Write-Fail "scripts\watchdog.ps1 nao encontrado."
    exit 1
}

Write-OK "Pre-requisitos OK."

# -- [2/4] Registrar tarefa agendada ------------------------------------------

Write-Step "2/4" "Registrando tarefa '$TaskName' no Agendador de Tarefas..."

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

# -- [3/4] Registrar tarefas auxiliares (updater on-demand + watchdog) -------

Write-Step "3/4" "Registrando tarefas auxiliares '$UpdaterTaskName' e '$WatchdogTaskName'..."

# --- PortalPedidosUpdater: on-demand, disparada via 'schtasks /run' pelo
#     endpoint POST /api/admin/update/apply (app/web/routes_update.py). SEM
#     trigger -- so roda quando chamada explicitamente. MultipleInstances
#     IgnoreNew e o guard de SO que faz um segundo /apply (ex.: duplo-clique)
#     virar no-op em vez de rodar dois updaters em paralelo (nao-negociavel).
$existingUpdater = Get-ScheduledTask -TaskName $UpdaterTaskName -ErrorAction SilentlyContinue
if ($existingUpdater) {
    Write-Host "        Removendo tarefa anterior '$UpdaterTaskName'..." -ForegroundColor Gray
    Unregister-ScheduledTask -TaskName $UpdaterTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$updaterAction = New-ScheduledTaskAction `
    -Execute          "powershell.exe" `
    -Argument         "-NoProfile -ExecutionPolicy Bypass -File `"$ApplyUpdateScript`"" `
    -WorkingDirectory $AppDir

$updaterSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances  IgnoreNew

Register-ScheduledTask `
    -TaskName  $UpdaterTaskName `
    -Action    $updaterAction `
    -Settings  $updaterSettings `
    -Principal $principal `
    -Force | Out-Null

Write-OK "Tarefa '$UpdaterTaskName' registrada (on-demand, sem trigger)."

# --- PortalPedidosWatchdog: religa o app se ele parar de responder (health-
#     check a cada 1 min, scripts\watchdog.ps1). MultipleInstances IgnoreNew
#     evita que um ciclo lento (ex.: religando o app) se sobreponha ao
#     proximo disparo do trigger de 1 min (Tarefa 7 depende disso).
$existingWatchdog = Get-ScheduledTask -TaskName $WatchdogTaskName -ErrorAction SilentlyContinue
if ($existingWatchdog) {
    Write-Host "        Removendo tarefa anterior '$WatchdogTaskName'..." -ForegroundColor Gray
    Unregister-ScheduledTask -TaskName $WatchdogTaskName -Confirm:$false -ErrorAction SilentlyContinue
}

$watchdogAction = New-ScheduledTaskAction `
    -Execute          "powershell.exe" `
    -Argument         "-NoProfile -ExecutionPolicy Bypass -File `"$WatchdogScript`"" `
    -WorkingDirectory $AppDir

# Trigger "Once" disparado agora + repeticao a cada 1 min "para sempre"
# ([TimeSpan]::MaxValue e o idioma padrao do Agendador de Tarefas do Windows
# para "sem data de termino" -- nao ha uma opcao "indefinido" dedicada).
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At                 (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration ([TimeSpan]::MaxValue)

$watchdogSettings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable $true `
    -MultipleInstances  IgnoreNew

Register-ScheduledTask `
    -TaskName  $WatchdogTaskName `
    -Action    $watchdogAction `
    -Trigger   $watchdogTrigger `
    -Settings  $watchdogSettings `
    -Principal $principal `
    -Force | Out-Null

Write-OK "Tarefa '$WatchdogTaskName' registrada (repete a cada 1 minuto)."

# -- [4/4] Iniciar agora -------------------------------------------------------

Write-Step "4/4" "Iniciando o servidor..."

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$state = (Get-ScheduledTask -TaskName $TaskName).State
if ($state -eq "Running") {
    Write-OK "Servidor iniciado (estado: $state)."
} else {
    Write-Host "        Estado: $state (pode levar alguns segundos para subir)." -ForegroundColor Yellow
}

# Ler porta e host do .env
$port       = "3636"
$portalHost = "127.0.0.1"
(Get-Content $EnvFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
    if ($_ -match "^PORTAL_PORT=(\d+)")    { $port       = $Matches[1] }
    if ($_ -match "^PORTAL_HOST=([\d.]+)") { $portalHost = $Matches[1] }
}

# Garantir liberacao no firewall quando o Portal escuta na rede (ja elevado aqui)
if ($portalHost -eq "0.0.0.0") {
    try {
        Set-PortalFirewallRule -Port ([int]$port)
        Write-OK "Porta $port liberada no firewall (rede local)."
    } catch {
        Write-Host "        AVISO: nao foi possivel criar a regra de firewall." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Servico configurado!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  O servidor inicia automaticamente com o Windows." -ForegroundColor White
Write-Host "  Acesso neste computador:  http://localhost:$port" -ForegroundColor White
if ($portalHost -eq "0.0.0.0") {
    $lanIp = Get-LanIp
    if ($lanIp) {
        Write-Host "  Acesso de outros PCs:     http://${lanIp}:$port" -ForegroundColor White
    }
}
Write-Host ""
Write-Host "  Para verificar o status:" -ForegroundColor Gray
Write-Host "    Get-ScheduledTask -TaskName '$TaskName','$UpdaterTaskName','$WatchdogTaskName' | Select-Object TaskName, State" -ForegroundColor Gray
Write-Host ""
Write-Host "  Para remover o autostart (e as tarefas de update/watchdog):" -ForegroundColor Gray
Write-Host "    Execute  desinstalar.bat  como Administrador." -ForegroundColor Gray
Write-Host ""
