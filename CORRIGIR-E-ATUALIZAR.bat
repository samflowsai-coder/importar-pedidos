@echo off
title Corrigir encoding + Atualizar - Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   IMPORTAR PEDIDOS - Corrigir encoding .ps1
echo  =============================================
echo.
echo  Ajustando os scripts .ps1 (remove caracteres que o
echo  PowerShell 5.1 nao le sem BOM)...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; Get-ChildItem '.\scripts\*.ps1' | ForEach-Object { $t=[IO.File]::ReadAllText($_.FullName); $t=$t.Replace([char]0x2014,'-').Replace([char]0x2500,'-'); [IO.File]::WriteAllText($_.FullName,$t,(New-Object Text.UTF8Encoding($false))); Write-Host ('   ok: '+$_.Name) }"

if errorlevel 1 (
    echo.
    echo  [ERRO] Nao consegui ajustar os scripts. Veja as mensagens acima.
    echo.
    pause
    exit /b 1
)

echo.
echo  Scripts ajustados. Rodando a atualizacao...
echo.

call "%~dp0atualizar.bat"
