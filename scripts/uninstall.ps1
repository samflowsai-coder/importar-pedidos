#Requires -Version 5.1
<#
.SYNOPSIS
    Remove o Portal de Pedidos do sistema.
.DESCRIPTION
    Para e remove o servico agendado, remove o .venv e pergunta
    sobre dados (output/, .env) antes de remover. Nao apaga o
    codigo-fonte — a pasta pode ser removida manualmente depois.
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

# ── [1/4] Parar e remover servico ────────────────────────────────────────────

Write-Step "1/4" "Removendo servico agendado..."

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -eq "Running") {
        Stop-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 2
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-OK "Tarefa '$TaskName' removida."
} else {
    Write-OK "Tarefa '$TaskName' nao estava registrada."
}

# ── [2/4] Remover .venv ───────────────────────────────────────────────────────

Write-Step "2/4" "Removendo ambiente virtual (.venv)..."

$venvPath = Join-Path $AppDir ".venv"
if (Test-Path $venvPath) {
    Remove-Item $venvPath -Recurse -Force
    Write-OK ".venv removido."
} else {
    Write-OK ".venv nao encontrado — nada a remover."
}

# ── [3/4] Dados de output ─────────────────────────────────────────────────────

Write-Step "3/4" "Dados de saida (output\)..."

$outputPath = Join-Path $AppDir "output"
if (Test-Path $outputPath) {
    $fileCount = (Get-ChildItem $outputPath -Recurse -File -ErrorAction SilentlyContinue).Count
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
        Write-OK "output\ vazio — mantido."
    }
}

# ── [4/4] Configuracao (.env) ─────────────────────────────────────────────────

Write-Step "4/4" "Arquivo de configuracao (.env)..."

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
