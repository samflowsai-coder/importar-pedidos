@echo off
title Desinstalar — Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS — Desinstalacao
echo  =============================================
echo.
echo  Este script pode precisar de privilegios de Administrador
echo  para remover a tarefa agendada do Windows.
echo.

:: Elevar se necessario (silencioso)
net session >nul 2>&1
if errorlevel 1 (
    echo  Elevando privilegios...
    powershell.exe -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1"

pause
