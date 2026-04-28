#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Remove a tarefa agendada do Portal de Pedidos do Windows.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "PortalPedidos"

Write-Host ""
Write-Host "  Removendo autostart do Portal de Pedidos..." -ForegroundColor Cyan
Write-Host ""

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $task) {
    Write-Host "  Tarefa '$TaskName' nao encontrada — nada a remover." -ForegroundColor Yellow
    exit 0
}

# Parar se estiver rodando
if ($task.State -eq "Running") {
    Write-Host "        Parando servidor..." -ForegroundColor Gray
    Stop-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "        OK — tarefa '$TaskName' removida." -ForegroundColor Green
Write-Host ""
Write-Host "  O servidor nao iniciara mais automaticamente com o Windows." -ForegroundColor White
Write-Host "  Use iniciar.bat para iniciar manualmente quando necessario." -ForegroundColor Gray
Write-Host ""
