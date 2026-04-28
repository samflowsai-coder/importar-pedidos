@echo off
title Servico Windows — Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS — Registrar servico
echo  =============================================
echo.
echo  Este script requer privilegios de Administrador.
echo  Se aparecer um aviso de UAC, clique em "Sim".
echo.

:: Verificar se esta rodando como administrador
net session >nul 2>&1
if errorlevel 1 (
    echo  Elevando privilegios...
    powershell.exe -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup-service.ps1"

if errorlevel 1 (
    echo.
    echo  Registro nao foi concluido. Veja as mensagens acima.
    echo.
)

pause
