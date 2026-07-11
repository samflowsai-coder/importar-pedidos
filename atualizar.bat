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
    pause
    exit /b 1
)

echo.
if exist "%~dp0ligar-flowpcp.bat" goto _flowprompt
set "RESP="
set /p "RESP=Configurar / ligar a integracao FlowPCP agora? (S/N): "
if /I "%RESP%"=="S" call "%~dp0configurar-integracao.bat"
goto _fim

:_flowprompt
set "RESP="
set /p "RESP=Ligar FlowPCP (pedido -> Flow + catalogo local) agora? (S/N): "
if /I "%RESP%"=="S" call "%~dp0ligar-flowpcp.bat"

:_fim
pause
