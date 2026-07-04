@echo off
title Sincronizar Catalogo — Fire para Flow
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo  ERRO: .venv nao encontrado. Rode instalar.bat primeiro.
    echo.
    pause
    exit /b 1
)

echo.
echo  Sincronizar catalogo de produtos Fire -^> FlowPCP
echo.
echo     [1] Simular (dry-run)  - so relatorio, NAO escreve
echo     [2] Promover           - grava de verdade no catalogo do Flow
echo.
set "MODO="
set /p "MODO=Escolha (1/2): "

if "%MODO%"=="2" goto promover

echo.
echo  Simulando (dry-run)...
echo.
".venv\Scripts\python.exe" "tools\sync_catalogo_fire.py" --slug mm
goto fim

:promover
echo.
set "CONF="
set /p "CONF=PROMOVER grava no catalogo do Flow. Digite PROMOVER para confirmar: "
if /I not "%CONF%"=="PROMOVER" (
    echo  Cancelado.
    goto fim
)
echo.
echo  Promovendo (escreve no Flow)...
echo.
".venv\Scripts\python.exe" "tools\sync_catalogo_fire.py" --slug mm --apply

:fim
echo.
pause
