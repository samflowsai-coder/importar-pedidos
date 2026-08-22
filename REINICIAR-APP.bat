@echo off
title Reiniciar app - Importar Pedidos
cd /d "%~dp0"
chcp 65001 >nul

echo.
echo  Reiniciando o Portal de Pedidos...
echo.

powershell -NoProfile -Command "Stop-ScheduledTask -TaskName PortalPedidos -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2; Start-ScheduledTask -TaskName PortalPedidos; Write-Host '   tarefa disparada, aguardando o boot (12s)...'; Start-Sleep -Seconds 12; $s = (Get-ScheduledTask -TaskName PortalPedidos).State; Write-Host ('   Estado da tarefa: ' + $s)"

echo.
echo  Testando http://localhost:3636/health ...
powershell -NoProfile -Command "try { $r = Invoke-WebRequest 'http://localhost:3636/health' -UseBasicParsing -TimeoutSec 8; Write-Host ('   HTTP ' + $r.StatusCode + ' -> APP NO AR!') -ForegroundColor Green } catch { Write-Host ('   sem resposta ainda: ' + $_.Exception.Message) -ForegroundColor Yellow; Write-Host '   (espere mais 30s e abra http://localhost:3636 no navegador)' }"

echo.
pause
