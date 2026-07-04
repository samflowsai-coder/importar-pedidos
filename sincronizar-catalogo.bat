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
echo  Enviando TODOS os produtos do Fire para o FlowPCP (full load, dry-run)...
echo.
".venv\Scripts\python.exe" "tools\sync_catalogo_fire.py" --slug mm
pause
