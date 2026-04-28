@echo off
title Atualizacao — Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS — Atualizacao
echo  =============================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\update.ps1"

if errorlevel 1 (
    echo.
    echo  Atualizacao nao foi concluida. Veja as mensagens acima.
    echo.
)

pause
