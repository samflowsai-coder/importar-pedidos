@echo off
title Importar Pedidos — ERP
cd /d "%~dp0"
chcp 65001 >nul

:: Verificar instalacao
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  [ERRO] Sistema nao instalado.
    echo  Execute "instalar.bat" primeiro.
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo.
    echo  [ERRO] Arquivo .env nao encontrado.
    echo  Execute "instalar.bat" primeiro.
    echo.
    pause
    exit /b 1
)

:: Verificar versao do Python no venv (deve ser 3.11+)
for /f "tokens=2" %%v in ('".venv\Scripts\python.exe" --version 2^>^&1') do set VENV_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("%VENV_VER%") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)
if %PY_MAJOR% LSS 3 (
    echo.
    echo  [ERRO] Python %VENV_VER% no .venv e incompativel. Necessario 3.11+.
    echo  Execute "instalar.bat" para recriar o ambiente.
    echo.
    pause
    exit /b 1
)
if %PY_MAJOR% EQU 3 if %PY_MINOR% LSS 11 (
    echo.
    echo  [ERRO] Python %VENV_VER% no .venv e incompativel. Necessario 3.11+.
    echo  Execute "instalar.bat" para recriar o ambiente.
    echo.
    pause
    exit /b 1
)

:: Ler porta e host do .env
set PORTAL_PORT=3636
set PORTAL_HOST=127.0.0.1
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="PORTAL_PORT" set PORTAL_PORT=%%b
    if "%%a"=="PORTAL_HOST" set PORTAL_HOST=%%b
)

:: Se escutando na rede (0.0.0.0), descobrir o IP local desta maquina
set LAN_IP=
if "%PORTAL_HOST%"=="0.0.0.0" (
    for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue ^| Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' -and $_.PrefixOrigin -ne 'WellKnown' } ^| Sort-Object SkipAsSource ^| Select-Object -First 1 -ExpandProperty IPAddress"') do set LAN_IP=%%i
)

cls
echo.
echo  ============================================
echo   IMPORTAR PEDIDOS
echo  ============================================
echo.
echo   Acesso neste computador:
echo     http://localhost:%PORTAL_PORT%
if "%PORTAL_HOST%"=="0.0.0.0" (
    echo.
    if defined LAN_IP (
        echo   Acesso de outros PCs/celulares na rede:
        echo     http://%LAN_IP%:%PORTAL_PORT%
    ) else (
        echo   Acesso de outros PCs na rede: use o IP desta maquina
        echo     (descubra com o comando: ipconfig^)
    )
)
echo.
echo   NAO feche esta janela enquanto estiver
echo   usando o sistema.
echo.
echo   Para encerrar: feche esta janela (Ctrl+C)
echo  ============================================
echo.

:: Iniciar servidor (mantém a janela aberta)
.venv\Scripts\python.exe ui.py
