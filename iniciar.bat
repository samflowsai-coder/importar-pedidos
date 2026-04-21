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

cls
echo.
echo  ============================================
echo   IMPORTAR PEDIDOS
echo  ============================================
echo.
echo   Iniciando servidor em http://localhost:3636
echo.
echo   NAO feche esta janela enquanto estiver
echo   usando o sistema.
echo.
echo   Para encerrar: feche esta janela (Ctrl+C)
echo  ============================================
echo.

:: Abrir o browser apos 3 segundos (em segundo plano)
start /b cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:3636"

:: Iniciar servidor (mantém a janela aberta)
.venv\Scripts\python.exe ui.py
