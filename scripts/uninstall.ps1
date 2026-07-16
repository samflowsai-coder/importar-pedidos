#Requires -Version 5.1
<#
.SYNOPSIS
    Remove o Portal de Pedidos do sistema.
.DESCRIPTION
    Para e remove o servico agendado, remove o .venv e pergunta
    sobre dados (output/, .env) antes de remover. Nao apaga o
    codigo-fonte - a pasta pode ser removida manualmente depois.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir   = Split-Path -Parent $PSScriptRoot
$TaskName = "PortalPedidos"

. (Join-Path $PSScriptRoot "network.ps1")

function Write-Step([string]$N, [string]$Msg) {
    Write-Host ""
    Write-Host "  [$N] $Msg" -ForegroundColor Cyan
}

function Write-OK([string]$Msg)   { Write-Host "        OK - $Msg" -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host "        AVISO: $Msg" -ForegroundColor Yellow }

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Yellow
Write-Host "   Desinstalar Portal de Pedidos" -ForegroundColor Yellow
Write-Host "  ============================================================" -ForegroundColor Yellow
Write-Host ""
Write-Host "  Esta acao remove o ambiente virtual e o servico agendado." -ForegroundColor White
Write-Host "  Os arquivos de pedido em output\ sao perguntados antes." -ForegroundColor Gray
Write-Host ""

$confirm = (Read-Host "  Deseja continuar? [s/N]").Trim().ToLower()
if ($confirm -ne "s") {
    Write-Host ""
    Write-Host "  Desinstalacao cancelada." -ForegroundColor Gray
    exit 0
}

# -- [1/5] Parar e remover servico --------------------------------------------

Write-Step "1/5" "Removendo servico agendado..."

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-OK "Tarefa '$TaskName' removida."
} else {
    Write-OK "Tarefa '$TaskName' nao estava registrada."
}

# Tarefas auxiliares do auto-update (updater on-demand + watchdog): removidas
# junto -- senao o watchdog continua rodando a cada 1 min PARA SEMPRE numa
# instalacao ja sem .venv, logando erro eternamente.
foreach ($aux in @("PortalPedidosUpdater", "PortalPedidosWatchdog")) {
    $auxTask = Get-ScheduledTask -TaskName $aux -ErrorAction SilentlyContinue
    if ($auxTask) {
        if ($auxTask.State -eq "Running") {
            Stop-ScheduledTask -TaskName $aux -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 1
        }
        Unregister-ScheduledTask -TaskName $aux -Confirm:$false -ErrorAction SilentlyContinue
        Write-OK "Tarefa '$aux' removida."
    }
}

# -- [2/5] Remover regra de firewall ------------------------------------------

Write-Step "2/5" "Removendo regra de firewall (se existir)..."

try {
    Remove-PortalFirewallRule
    Write-OK "Regra 'Portal de Pedidos' removida."
} catch {
    Write-Warn "Nao foi possivel remover a regra de firewall automaticamente."
    Write-Host "        Remova manualmente como Administrador, se necessario:" -ForegroundColor Gray
    Write-Host "        Remove-NetFirewallRule -DisplayName 'Portal de Pedidos'" -ForegroundColor Gray
}

# -- [3/5] Remover .venv -------------------------------------------------------

Write-Step "3/5" "Removendo ambiente virtual (.venv)..."

$venvPath = Join-Path $AppDir ".venv"
if (Test-Path $venvPath) {
    Remove-Item $venvPath -Recurse -Force
    Write-OK ".venv removido."
} else {
    Write-OK ".venv nao encontrado - nada a remover."
}

# -- [4/5] Dados de output -----------------------------------------------------

Write-Step "4/5" "Dados de saida (output\)..."

$outputPath = Join-Path $AppDir "output"
if (Test-Path $outputPath) {
    # @(...): com 0 arquivos (resultado nulo) ou 1 (FileInfo escalar), o .Count
    # lanca sob Set-StrictMode -Version Latest no WinPS 5.1. So funcionava por
    # acaso com >=2 arquivos.
    $fileCount = @(Get-ChildItem $outputPath -Recurse -File -ErrorAction SilentlyContinue).Count
    if ($fileCount -gt 0) {
        Write-Host ""
        Write-Host "  Encontrados $fileCount arquivo(s) em output\." -ForegroundColor White
        $delOutput = (Read-Host "  Remover output\? Os pedidos exportados serao perdidos [s/N]").Trim().ToLower()
        if ($delOutput -eq "s") {
            Remove-Item $outputPath -Recurse -Force
            Write-OK "output\ removido."
        } else {
            Write-OK "output\ mantido."
        }
    } else {
        Write-OK "output\ vazio - mantido."
    }
}

# -- [5/5] Configuracao (.env) -------------------------------------------------

Write-Step "5/5" "Arquivo de configuracao (.env)..."

$envFile = Join-Path $AppDir ".env"
if (Test-Path $envFile) {
    $delEnv = (Read-Host "  Remover .env (contem API keys e configuracoes)? [s/N]").Trim().ToLower()
    if ($delEnv -eq "s") {
        Remove-Item $envFile -Force
        Write-OK ".env removido."
    } else {
        Write-OK ".env mantido."
    }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Desinstalacao concluida." -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  A pasta do sistema pode ser removida manualmente agora." -ForegroundColor Gray
Write-Host ""
