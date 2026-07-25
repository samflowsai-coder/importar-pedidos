@echo off
title Resetar senha - Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS - Resetar senha
echo  =============================================
echo.
echo  Usuarios cadastrados:
echo.
".venv\Scripts\python.exe" -c "from app.persistence import db, users_repo; db.init(); [print('   ' + u.email + '   [' + u.role + ', ativo=' + str(u.active) + ']') for u in users_repo.list_users()]"

echo.
set /p "EMAIL=  Digite o e-mail do usuario para resetar: "

echo.
echo  Digite a NOVA senha (nao aparece na tela) - duas vezes:
".venv\Scripts\python.exe" tools\create_user.py "%EMAIL%" --reset

echo.
echo  Se apareceu OK acima, use a nova senha em http://localhost:3636
echo.
pause
