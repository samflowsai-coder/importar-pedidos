@echo off
title Diagnostico pip - Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   DIAGNOSTICO - pip (Importar Pedidos)
echo  =============================================
echo.
echo  Rodando pip install -e com saida COMPLETA...
echo  (salvando o log completo em pip-log.txt)
echo.

".venv\Scripts\python.exe" -m pip install -e "%~dp0." --no-warn-script-location > "%~dp0pip-log.txt" 2>&1
echo  pip exit code: %errorlevel%

echo.
echo  ===== ultimas 25 linhas do log do pip =====
powershell -NoProfile -Command "Get-Content '%~dp0pip-log.txt' -Tail 25"

echo.
echo  ===== pacotes-chave ja instalados no .venv =====
".venv\Scripts\python.exe" -m pip list 2>nul | findstr /I "fastapi pydantic uvicorn firebird multipart sqlalchemy cryptography apscheduler prometheus tenacity"

echo.
echo  Log completo salvo em: pip-log.txt (mesma pasta)
echo.
pause
