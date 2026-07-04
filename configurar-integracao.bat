@echo off
title Configurar Integracao FlowPCP
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

".venv\Scripts\python.exe" "tools\configurar_flowpcp.py" %*
pause
