#Requires -Version 5.1
<#
.SYNOPSIS
    Instalacao idempotente do Portal de Pedidos.
.DESCRIPTION
    Verifica Python 3.11+, cria .venv, instala dependencias,
    configura .env interativamente e cria o primeiro usuario admin.
    Seguro para executar mais de uma vez.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$AppDir = Split-Path -Parent $PSScriptRoot

# ── Helpers de output ─────────────────────────────────────────────────────────

function Write-Step([string]$N, [string]$Msg) {
    Write-Host ""
    Write-Host "  [$N] $Msg" -ForegroundColor Cyan
}

function Write-OK([string]$Msg) {
    Write-Host "        OK — $Msg" -ForegroundColor Green
}

function Write-Warn([string]$Msg) {
    Write-Host "        AVISO: $Msg" -ForegroundColor Yellow
}

function Write-Fail([string]$Msg) {
    Write-Host ""
    Write-Host "  [ERRO] $Msg" -ForegroundColor Red
    Write-Host ""
}

# ── [1/6] Python 3.11+ ────────────────────────────────────────────────────────

Write-Step "1/6" "Verificando Python 3.11+..."

function Find-Python311 {
    # Tentar executaveis comuns
    foreach ($exe in @("python3.11", "python3", "python")) {
        try {
            $v = & $exe --version 2>&1
            if ($v -match "Python (\d+)\.(\d+)" -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) {
                return $exe
            }
        } catch {}
    }
    # Tentar Python Launcher (py.exe) com versao explicita
    try {
        $v = & py @("-3.11", "--version") 2>&1
        if ($v -match "Python 3\.1[1-9]") { return "py311" }
    } catch {}
    return $null
}

$PythonCmd = Find-Python311

if (-not $PythonCmd) {
    Write-Warn "Python 3.11+ nao encontrado no sistema."
    Write-Host ""

    # Tentar winget (disponivel no Windows 10 1709+ e Windows 11)
    $hasWinget = $false
    try { $null = & winget --version 2>&1; $hasWinget = ($LASTEXITCODE -eq 0) } catch {}

    if ($hasWinget) {
        Write-Host "  Instalando Python 3.11 via winget..." -ForegroundColor Yellow
        & winget install --id Python.Python.3.11 --source winget `
            --accept-package-agreements --accept-source-agreements --silent
        # Atualizar PATH na sessao atual
        $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                    [Environment]::GetEnvironmentVariable("Path", "User")
        $PythonCmd = Find-Python311
        if (-not $PythonCmd) {
            Write-Fail "Python instalado, mas nao encontrado no PATH ainda."
            Write-Host "  Feche esta janela, reabra e execute instalar.bat novamente." -ForegroundColor White
            exit 1
        }
        Write-OK "Python 3.11 instalado via winget."
    } else {
        Write-Fail "winget nao disponivel. Instale o Python 3.11 manualmente:"
        Write-Host "  https://www.python.org/downloads/release/python-3110/" -ForegroundColor White
        Write-Host "  IMPORTANTE: marque 'Add Python to PATH' durante a instalacao." -ForegroundColor White
        try { Start-Process "https://www.python.org/downloads/release/python-3110/" } catch {}
        exit 1
    }
} else {
    $verDisplay = if ($PythonCmd -eq "py311") {
        (& py @("-3.11", "--version") 2>&1)
    } else {
        (& $PythonCmd --version 2>&1)
    }
    Write-OK "$verDisplay detectado."
}

# ── [2/6] Ambiente virtual ────────────────────────────────────────────────────

Write-Step "2/6" "Verificando ambiente virtual (.venv)..."

$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
$VenvPythonW = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
$VenvPip    = Join-Path $AppDir ".venv\Scripts\pip.exe"
$venvOk = $false

if (Test-Path $VenvPython) {
    try {
        $v = & $VenvPython --version 2>&1
        if ($v -match "Python (\d+)\.(\d+)" -and [int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11) {
            $venvOk = $true
            Write-OK ".venv existente com $v — mantido."
        }
    } catch {}
}

if (-not $venvOk) {
    $venvPath = Join-Path $AppDir ".venv"
    if (Test-Path $venvPath) {
        Write-Warn ".venv com versao incompativel — recriando..."
        Remove-Item $venvPath -Recurse -Force
    }
    Write-Host "        Criando .venv..." -ForegroundColor Gray

    if ($PythonCmd -eq "py311") {
        & py @("-3.11", "-m", "venv", $venvPath)
    } else {
        & $PythonCmd -m venv $venvPath
    }

    if (-not (Test-Path $VenvPython)) {
        Write-Fail "Falha ao criar .venv."
        exit 1
    }
    Write-OK ".venv criado."
}

# ── [3/6] Dependencias ────────────────────────────────────────────────────────

Write-Step "3/6" "Instalando/atualizando dependencias (pode levar alguns minutos)..."
Write-Host "        Aguarde..." -ForegroundColor Gray

& $VenvPip install -e $AppDir --quiet --no-warn-script-location 2>&1 | Out-Null

if ($LASTEXITCODE -ne 0) {
    # Tentar novamente com output para diagnostico
    Write-Fail "Falha ao instalar dependencias."
    Write-Host "  Tentando novamente com detalhes:" -ForegroundColor Yellow
    & $VenvPip install -e $AppDir --no-warn-script-location
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Verifique a conexao com a internet e tente novamente." -ForegroundColor White
        exit 1
    }
}
Write-OK "Dependencias instaladas."

# ── [4/6] Configurar .env ─────────────────────────────────────────────────────

Write-Step "4/6" "Configurando .env..."

$EnvFile = Join-Path $AppDir ".env"

if (Test-Path $EnvFile) {
    Write-OK ".env ja existe — mantido. (Para reconfigurar: delete .env e rode instalar.bat novamente.)"
} else {
    Write-Host ""
    Write-Host "  Vamos configurar o sistema. Responda as perguntas abaixo." -ForegroundColor White
    Write-Host "  Pressione Enter para aceitar o valor padrao [entre colchetes]." -ForegroundColor Gray
    Write-Host ""

    # OPENROUTER_API_KEY
    Write-Host "  OpenRouter API Key — necessaria para processar PDFs complexos." -ForegroundColor White
    Write-Host "  Obtencao gratuita em: https://openrouter.ai/keys" -ForegroundColor Gray
    $ApiKey = ""
    while ([string]::IsNullOrWhiteSpace($ApiKey)) {
        $ApiKey = (Read-Host "  OPENROUTER_API_KEY").Trim()
        if ([string]::IsNullOrWhiteSpace($ApiKey)) {
            Write-Host "  A chave e obrigatoria." -ForegroundColor Yellow
        }
    }

    # EXPORT_MODE
    Write-Host ""
    Write-Host "  Modo de exportacao (vale para todas as empresas):" -ForegroundColor White
    Write-Host "    [1] xlsx  — gera arquivo .xlsx para importar no ERP (recomendado)" -ForegroundColor Gray
    Write-Host "    [2] db    — escreve direto no Firebird (avancado)" -ForegroundColor Gray
    Write-Host "    [3] both  — gera .xlsx E escreve no Firebird" -ForegroundColor Gray
    $modeInput = (Read-Host "  Escolha [1/2/3] (Enter = 1)").Trim()
    $ExportMode = switch ($modeInput) { "2" { "db" } "3" { "both" } default { "xlsx" } }

    # Porta
    Write-Host ""
    $portInput = (Read-Host "  Porta do servidor [3636]").Trim()
    $Port = if ($portInput -match "^\d+$") { $portInput } else { "3636" }

    # Construir .env
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add("# Portal de Pedidos - gerado por instalar.bat em $(Get-Date -Format 'yyyy-MM-dd HH:mm')")
    $lines.Add("# Para alterar: edite este arquivo e reinicie o servidor.")
    $lines.Add("#")
    $lines.Add("# Multi-ambiente (MM, Nasmar, ...): pastas e Firebird de cada empresa")
    $lines.Add("# sao configurados no Portal apos o primeiro login, em")
    $lines.Add("# /admin/ambientes -- nao precisa preencher nada aqui.")
    $lines.Add("")
    $lines.Add("OPENROUTER_API_KEY=$ApiKey")
    $lines.Add("EXPORT_MODE=$ExportMode")
    $lines.Add("")
    $lines.Add("# APP_DATA_DIR: onde ficam os SQLite (app_shared.db + app_state_<slug>.db)")
    $lines.Add("APP_DATA_DIR=data/")
    $lines.Add("LOG_DIR=logs/")
    $lines.Add("")
    $lines.Add("PORTAL_HOST=127.0.0.1")
    $lines.Add("PORTAL_PORT=$Port")
    $lines.Add("PORTAL_RELOAD=false")
    # Portal serve em HTTP local; cookie Secure=true bloqueia o login.
    # Trocar para 1 ao colocar TLS reverso (nginx/IIS) na frente.
    $lines.Add("PORTAL_COOKIE_SECURE=0")
    $lines.Add("")
    $lines.Add("# Sessao e retencao")
    $lines.Add("SESSION_TTL_HOURS=24")
    $lines.Add("RETENTION_DAYS=180")

    $lines | Set-Content -Path $EnvFile -Encoding UTF8
    Write-OK ".env criado com modo '$ExportMode' na porta $Port."
}

# ── [5/6] Diretorios ──────────────────────────────────────────────────────────

Write-Step "5/6" "Verificando diretorios..."

# input/ e output/ ficam aqui apenas como exemplo. Cada empresa configura
# suas proprias pastas em /admin/ambientes apos o primeiro login.
foreach ($d in @("input", "output", "logs", "data")) {
    $p = Join-Path $AppDir $d
    if (-not (Test-Path $p)) {
        New-Item -ItemType Directory -Path $p | Out-Null
    }
}
Write-OK "data\  logs\  presentes (input\ e output\ servem so como exemplo)."

# ── [6/6] Primeiro usuario admin ──────────────────────────────────────────────

Write-Step "6/6" "Configurando usuario administrador..."

# Carregar .env para que app.persistence encontre o caminho do DB
$envContent = Get-Content $EnvFile -Encoding UTF8
foreach ($line in $envContent) {
    if ($line -match "^\s*([^#][^=]+)=(.*)$") {
        $k = $Matches[1].Trim(); $v = $Matches[2].Trim()
        if (-not [Environment]::GetEnvironmentVariable($k)) {
            [Environment]::SetEnvironmentVariable($k, $v, "Process")
        }
    }
}

$AppDirFwd = $AppDir.Replace("\", "/")
$checkScript = @"
import sys, os
os.chdir('$AppDirFwd')
from app.persistence import db, users_repo
db.init()
sys.exit(0 if users_repo.count_active_users() > 0 else 1)
"@

& $VenvPython -c $checkScript 2>$null
$usersExist = ($LASTEXITCODE -eq 0)

if ($usersExist) {
    Write-OK "Usuarios ja configurados — pulando."
} else {
    Write-Host ""
    Write-Host "  Criando o primeiro usuario administrador do sistema." -ForegroundColor White
    Write-Host "  Estas credenciais serao usadas para acessar o Portal." -ForegroundColor Gray
    Write-Host ""

    $AdminEmail = ""
    while ($AdminEmail -notmatch "^[^@\s]+@[^@\s]+\.[^@\s]+$") {
        $AdminEmail = (Read-Host "  E-mail do admin").Trim()
        if ($AdminEmail -notmatch "@") { Write-Host "  Digite um e-mail valido." -ForegroundColor Yellow }
    }

    $pw1 = $pw2 = $null
    do {
        $pw1 = Read-Host "  Senha (minimo 8 caracteres)" -AsSecureString
        $pw2 = Read-Host "  Confirme a senha"           -AsSecureString
        $p1  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                   [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw1))
        $p2  = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                   [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw2))
        if ($p1 -ne $p2)      { Write-Host "  Senhas nao conferem. Tente novamente." -ForegroundColor Yellow }
        elseif ($p1.Length -lt 8) { Write-Host "  Senha muito curta (minimo 8 caracteres)." -ForegroundColor Yellow; $p1 = "" }
    } while ($p1 -ne $p2 -or $p1.Length -lt 8)

    # Pipar senha via stdin para create_user.py (modo nao-interativo)
    $result = $p1 | & $VenvPython (Join-Path $AppDir "tools\create_user.py") $AdminEmail --role admin 2>&1

    if ($LASTEXITCODE -eq 0) {
        Write-OK "Admin '$AdminEmail' criado."
    } elseif ($result -match "ja existe") {
        Write-OK "Usuario '$AdminEmail' ja existe — mantido."
    } else {
        Write-Warn "Falha ao criar admin: $result"
        Write-Host ""
        Write-Host "  Para criar manualmente apos a instalacao:" -ForegroundColor Gray
        Write-Host "  .venv\Scripts\python.exe tools\create_user.py SEU@EMAIL --role admin" -ForegroundColor Gray
    }
}

# ── Finalizacao ───────────────────────────────────────────────────────────────

$port = "3636"
$envLines2 = Get-Content $EnvFile -Encoding UTF8 -ErrorAction SilentlyContinue
foreach ($l in $envLines2) {
    if ($l -match "^PORTAL_PORT=(\d+)") { $port = $Matches[1] }
}

Write-Host ""
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host "   Instalacao concluida!" -ForegroundColor Green
Write-Host "  ============================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Para iniciar o sistema:" -ForegroundColor White
Write-Host "    Duplo-clique em  iniciar.bat" -ForegroundColor White
Write-Host "    Acesso em        http://localhost:$port" -ForegroundColor White
Write-Host ""
Write-Host "  Proximos passos no navegador:" -ForegroundColor White
Write-Host "    1. Login com o admin recem-criado" -ForegroundColor Gray
Write-Host "    2. Configuracoes -> Ambientes -> + Novo ambiente" -ForegroundColor Gray
Write-Host "       Cadastre uma entrada para CADA empresa (ex: MM, Nasmar)" -ForegroundColor Gray
Write-Host "       informando pastas e dados do Firebird de cada uma." -ForegroundColor Gray
Write-Host "    3. Apos sair, ao logar de novo o sistema pede para escolher" -ForegroundColor Gray
Write-Host "       o ambiente ativo da sessao." -ForegroundColor Gray
Write-Host ""
Write-Host "  Para iniciar automaticamente com o Windows (recomendado):" -ForegroundColor Gray
Write-Host "    Execute  setup-service.bat  como Administrador." -ForegroundColor Gray
Write-Host ""
