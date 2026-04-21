@echo off
title Instalacao — Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS — Instalacao
echo  =============================================
echo.

:: Verificar Python
python --version >nul 2>&1
if errorlevel 1 (
    echo  [ERRO] Python nao encontrado no sistema.
    echo.
    echo  Instale o Python 3.11 em: https://python.org/downloads
    echo  IMPORTANTE: marque "Add Python to PATH" durante a instalacao.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo  Python encontrado: %PYVER%
echo.

:: Criar ambiente virtual
echo  [1/3] Criando ambiente virtual...
python -m venv .venv
if errorlevel 1 (
    echo  [ERRO] Falha ao criar ambiente virtual.
    pause
    exit /b 1
)
echo         OK.

:: Instalar dependencias
echo  [2/3] Instalando dependencias (pode levar alguns minutos)...
.venv\Scripts\pip install -e . --quiet --no-warn-script-location
if errorlevel 1 (
    echo  [ERRO] Falha ao instalar dependencias.
    echo  Verifique sua conexao com a internet e tente novamente.
    pause
    exit /b 1
)
echo         OK.

:: Configurar .env
echo  [3/3] Configurando ambiente...
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo         Arquivo .env criado.
) else (
    echo         Arquivo .env ja existe, mantido.
)
echo.

echo  =============================================
echo   Instalacao concluida com sucesso!
echo  =============================================
echo.
echo  Proximo passo:
echo    Execute "iniciar.bat" para abrir o sistema.
echo.
pause
