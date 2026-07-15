#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Remove as tarefas agendadas do Portal de Pedidos do Windows.
.DESCRIPTION
    Remove as 3 tarefas registradas por scripts/setup-service.ps1:
    'PortalPedidos' (servidor), 'PortalPedidosUpdater' (auto-update on-demand)
    e 'PortalPedidosWatchdog' (health-check a cada 1 min). Idempotente --
    tarefas ja ausentes nao geram erro.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName         = "PortalPedidos"
$UpdaterTaskName  = "PortalPedidosUpdater"
$WatchdogTaskName = "PortalPedidosWatchdog"

Write-Host ""
Write-Host "  Removendo autostart do Portal de Pedidos..." -ForegroundColor Cyan
Write-Host ""

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($task) {
    # Parar se estiver rodando
    if ($task.State -eq "Running") {
        Write-Host "        Parando servidor..." -ForegroundColor Gray
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Write-Host "        OK — tarefa '$TaskName' removida." -ForegroundColor Green
} else {
    Write-Host "        Tarefa '$TaskName' nao encontrada — nada a remover." -ForegroundColor Yellow
}

# As duas tarefas auxiliares (updater on-demand, sem estado "Running"
# persistente entre ciclos; watchdog, disparado a cada 1 min) so precisam de
# Unregister -- nenhuma delas mantem um processo de servidor de longa duracao
# para parar antes.
foreach ($auxTaskName in @($UpdaterTaskName, $WatchdogTaskName)) {
    $auxTask = Get-ScheduledTask -TaskName $auxTaskName -ErrorAction SilentlyContinue
    if ($auxTask) {
        Unregister-ScheduledTask -TaskName $auxTaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "        OK — tarefa '$auxTaskName' removida." -ForegroundColor Green
    } else {
        Write-Host "        Tarefa '$auxTaskName' nao encontrada — nada a remover." -ForegroundColor Yellow
    }
}

Write-Host ""
Write-Host "  O servidor nao iniciara mais automaticamente com o Windows." -ForegroundColor White
Write-Host "  O auto-update e o watchdog tambem foram desativados." -ForegroundColor White
Write-Host "  Use iniciar.bat para iniciar manualmente quando necessario." -ForegroundColor Gray
Write-Host ""
