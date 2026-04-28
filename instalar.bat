@echo off
title Instalacao — Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS — Instalacao
echo  =============================================
echo.

:: Verificar PowerShell
powershell.exe -Command "exit 0" >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] PowerShell nao encontrado.
    echo  Necessario Windows 10 ou superior.
    pause
    exit /b 1
)

:: Executar script de instalacao via PowerShell
:: -ExecutionPolicy Bypass e por-processo, nao altera politica do sistema
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1"

if errorlevel 1 (
    echo.
    echo  A instalacao nao foi concluida. Veja as mensagens acima.
    echo.
    pause
    exit /b 1
)

pause
