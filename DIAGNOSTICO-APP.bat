@echo off
title Diagnostico do app - Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  =============================================
echo   DIAGNOSTICO DO APP - Importar Pedidos
echo  =============================================

echo.
echo  === 1) Quem esta usando a porta 3636 ===
powershell -NoProfile -Command "$c = Get-NetTCPConnection -LocalPort 3636 -ErrorAction SilentlyContinue; if ($c) { $c | Select-Object State,OwningProcess | Format-Table -Auto | Out-String | Write-Host; Get-Process -Id ($c.OwningProcess | Select-Object -Unique) -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path | Format-Table -Auto | Out-String | Write-Host } else { Write-Host '   porta 3636 LIVRE (ninguem escutando)' }"

echo  === 2) Estado das tarefas ===
powershell -NoProfile -Command "Get-ScheduledTask -TaskName PortalPedidos,PortalPedidosUpdater,PortalPedidosWatchdog -ErrorAction SilentlyContinue | Select-Object TaskName,State | Format-Table -Auto | Out-String | Write-Host"

echo  === 3) Ultimas 30 linhas do log mais recente ===
powershell -NoProfile -Command "$f = Get-ChildItem 'logs\*.log' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if ($f) { Write-Host $f.FullName; Get-Content $f.FullName -Tail 30 } else { Write-Host '   sem arquivos em logs\' }"

echo.
echo  === 4) Testando o boot do app em primeiro plano (8s) ===
echo  (se crashar, o traceback aparece abaixo; se subir, encerro o teste)
echo.
powershell -NoProfile -Command "$p = Start-Process '.venv\Scripts\python.exe' -ArgumentList 'ui.py' -RedirectStandardOutput 'app-boot-out.txt' -RedirectStandardError 'app-boot-err.txt' -PassThru -NoNewWindow; Start-Sleep -Seconds 8; if (-not $p.HasExited) { Write-Host '   >>> App AINDA RODANDO apos 8s = SUBIU OK. Encerrando o teste.' -ForegroundColor Green; Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } else { Write-Host ('   >>> App SAIU sozinho, exit code ' + $p.ExitCode + ' = CRASHOU no boot.') -ForegroundColor Red }; Write-Host ''; Write-Host '   ----- ERRO (stderr) -----'; Get-Content 'app-boot-err.txt' -ErrorAction SilentlyContinue; Write-Host '   ----- saida (stdout, fim) -----'; Get-Content 'app-boot-out.txt' -Tail 15 -ErrorAction SilentlyContinue"

echo.
echo  Logs completos do teste salvos em: app-boot-err.txt / app-boot-out.txt
echo.
pause
