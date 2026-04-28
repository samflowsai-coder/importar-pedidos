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

:: Ler porta do .env
set PORTAL_PORT=3636
for /f "tokens=1,2 delims==" %%a in (.env) do (
    if "%%a"=="PORTAL_PORT" set PORTAL_PORT=%%b
)

cls
echo.
echo  ============================================
echo   IMPORTAR PEDIDOS
echo  ============================================
echo.
echo   Iniciando servidor em http://localhost:%PORTAL_PORT%
echo.
echo   NAO feche esta janela enquanto estiver
echo   usando o sistema.
echo.
echo   Para encerrar: feche esta janela (Ctrl+C)
echo  ============================================
echo.

:: Abrir o browser apos 3 segundos (em segundo plano)
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:%PORTAL_PORT%"

:: Iniciar servidor (mantém a janela aberta)
.venv\Scripts\python.exe ui.py
