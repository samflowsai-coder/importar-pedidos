#Requires -Version 5.1
<#
.SYNOPSIS
    Watchdog do Portal de Pedidos -- religa o app se ele parar de responder.
.DESCRIPTION
    Executado pela Tarefa Agendada "PortalPedidosWatchdog" (SYSTEM), disparada
    a cada 1 minuto (spec
    docs/superpowers/specs/2026-07-14-auto-update-endpoint-design.md Secao 8).
    PowerShell puro, sem depender do .venv (que o update pode estar mexendo)
    nem do processo web (e exatamente quem pode estar travado).

    NUNCA interfere com um update em andamento: a UNICA fonte de verdade para
    "update em andamento" e a presenca de <DataDir>/updates/update.lock. Por
    isso o DataDir e resolvido AQUI de forma IDENTICA a scripts/apply-update.ps1
    e app/web/routes_update.py::_data_dir() -- um APP_DATA_DIR absoluto (deploy
    multi-ambiente, ex. D:\PortalData\MM) resolvido diferente faria o watchdog
    nunca ver o lock certo e religar o app NO MEIO de um clean-replace de app/,
    corrompendo o update em andamento. Bloco de resolucao copiado verbatim de
    scripts/apply-update.ps1 (mesma fonte de verdade) -- ver comentario la para
    o raciocinio completo.

    Logica (spec Secao 8):
      lock fresco (idade < 30min)  -> no-op (update em andamento, nao mexe em nada)
      lock orfao (idade > 30min)   -> remove, loga, segue para o health-check
      janela anti-flap ativa (apos um restart nosso) -> no-op, so decrementa
      tarefa PortalPedidos != Running -> religa direto (nao espera 3 falhas)
      GET /health falha 3x seguidas -> Stop-ScheduledTask -> espera a porta
        liberar -> mata o processo dono da porta SOMENTE se sob .venv\ ->
        Start-ScheduledTask -> zera contador -> ativa anti-flap (3 ciclos)
      GET /health OK -> zera contador

    Le:      .env (PORTAL_PORT, APP_DATA_DIR)
             <DataDir>/updates/update.lock
             <DataDir>/updates/watchdog_state.json (contador + anti-flap)
    Escreve: <DataDir>/updates/watchdog_state.json,
             logs/watchdog.log (sob AppDir, com rotacao simples por tamanho --
             ao contrario do apply-update.ps1, que so roda durante updates,
             este script roda para sempre a cada 1 minuto e um log sem
             nenhuma cap cresceria sem limite ao longo de meses).

    <DataDir> = APP_DATA_DIR (do .env), se setado -- absoluto usado como esta,
    relativo resolvido contra AppDir; senao "<AppDir>\data" (legado).
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest sem barra de progresso (evita lentidao)

# ── Caminhos ──────────────────────────────────────────────────────────────────

$AppDir = Split-Path -Parent $PSScriptRoot

# Le uma chave de um arquivo .env (regex ancorado por linha -- ignora
# comentarios e linhas em branco), desaspando o valor (aspas simples ou
# duplas), necessario porque paths podem vir aspados no .env. Copiada
# verbatim de scripts/apply-update.ps1 -- MESMA logica, para que o DataDir
# resolvido aqui seja identico ao do updater (ver Hard Safety Constraint no
# cabecalho deste arquivo).
function Get-DotEnvValue {
    param([string]$EnvPath, [string]$Key)
    if (-not (Test-Path $EnvPath)) { return $null }
    $value = $null
    (Get-Content $EnvPath -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
        if ($_ -match "^$Key=(.+)$") {
            $v = $Matches[1].Trim()
            if ($v -match '^"(.*)"$') { $v = $Matches[1] }
            elseif ($v -match "^'(.*)'$") { $v = $Matches[1] }
            if ($v) { $value = $v }
        }
    }
    return $value
}

# Resolve o data dir EXATAMENTE como scripts/apply-update.ps1 e o web
# (app/web/routes_update.py::_data_dir()):
#   def _data_dir(): return Path(os.environ.get("APP_DATA_DIR") or (_app_dir() / "data"))
# Sem isso, um APP_DATA_DIR absoluto (deploy multi-ambiente) faria este
# script procurar o update.lock no lugar ERRADO -- nunca o encontraria, e
# religaria o app no meio de um update em andamento (a catastrofe que este
# script existe para nunca causar).
$AppDataDirRaw = Get-DotEnvValue -EnvPath (Join-Path $AppDir ".env") -Key "APP_DATA_DIR"
if ($AppDataDirRaw -and [System.IO.Path]::IsPathRooted($AppDataDirRaw)) {
    # Absoluto -- usar como esta (paridade com Path(os.environ["APP_DATA_DIR"])
    # quando o valor ja e absoluto).
    $DataDir = $AppDataDirRaw
} elseif ($AppDataDirRaw) {
    # Relativo (ex. "data_mm") -- Python resolveria contra o CWD do
    # processo, que em producao E o AppDir (o app roda com cwd = raiz da
    # instalacao); replicamos isso resolvendo contra $AppDir.
    $DataDir = Join-Path $AppDir $AppDataDirRaw
} else {
    # Ausente/vazio -- comportamento legado inalterado.
    $DataDir = Join-Path $AppDir "data"
}

$Updates       = Join-Path $DataDir "updates"
$LockPath      = Join-Path $Updates "update.lock"
$StateFilePath = Join-Path $Updates "watchdog_state.json"
$LogPath       = Join-Path $AppDir "logs\watchdog.log"
$AppTaskName   = "PortalPedidos"

# Limiares (spec Secao 8 / decisao #5 da spec -- valores cravados).
$LockOrphanMinutes  = 30
$FailureThreshold   = 3
$AntiFlapSkipCycles = 3
$HealthTimeoutSec   = 10
$PortFreeWaitSec    = 30

# ── Logging (best-effort; nunca deve derrubar o watchdog) ────────────────────

function Write-Log {
    param([string]$Message)
    try {
        $logDir = Split-Path -Parent $LogPath
        New-Item -ItemType Directory -Path $logDir -Force -ErrorAction SilentlyContinue | Out-Null

        # Rotacao simples por tamanho: este script roda a cada 1 minuto para
        # sempre (diferente do apply-update.ps1, que so roda durante updates)
        # -- sem alguma cap o log cresceria sem limite ao longo de meses.
        if (Test-Path $LogPath) {
            $existing = Get-Item -Path $LogPath -ErrorAction SilentlyContinue
            if ($existing -and $existing.Length -gt 10MB) {
                Move-Item -Path $LogPath -Destination "$LogPath.old" -Force -ErrorAction SilentlyContinue
            }
        }

        $line = "[$(Get-Date -Format o)] $Message"
        Add-Content -Path $LogPath -Value $line -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch {
        # log e diagnostico, nunca deve interromper o watchdog
    }
}

# ── Tempo ─────────────────────────────────────────────────────────────────────

function Get-IsoNow {
    return (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
}

# ── .env / porta (copiada verbatim de scripts/apply-update.ps1) ─────────────

function Get-PortalPort {
    $port = 3636
    $envFile = Join-Path $AppDir ".env"
    if (Test-Path $envFile) {
        (Get-Content $envFile -Encoding UTF8 -ErrorAction SilentlyContinue) | ForEach-Object {
            if ($_ -match "^PORTAL_PORT=(\d+)") { $port = [int]$Matches[1] }
        }
    }
    return [int]$port
}

# ── watchdog_state.json ──────────────────────────────────────────────────────
# Forma: { "consecutive_failures": int, "skip_cycles": int, "last_updated": iso }
# consecutive_failures -- falhas de health-check seguidas (zera em sucesso ou
#                          apos um restart).
# skip_cycles          -- ciclos restantes da janela anti-flap (decrementa 1
#                          por execucao; 0 = watchdog age normalmente).
#
# Degrada para o padrao (contador=0, sem anti-flap) em QUALQUER situacao
# anormal -- arquivo ausente, JSON corrompido, tipo inesperado -- mesma
# postura de tolerancia de app/updates/state.py::read_status() (que devolve
# {"status": "idle"} para qualquer excecao ou tipo que nao seja dict). Nunca
# lanca: um contador perdido custa, no maximo, uma falha "esquecida" -- nunca
# um crash do watchdog.
function Get-WatchdogState {
    $default = @{ consecutive_failures = 0; skip_cycles = 0 }
    if (-not (Test-Path $StateFilePath)) { return $default }
    try {
        $raw = Get-Content -Path $StateFilePath -Raw -Encoding UTF8
        if (-not $raw) { return $default }
        $obj = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($obj -isnot [System.Management.Automation.PSCustomObject]) { return $default }

        $failures = 0
        $skip = 0
        if ($obj.PSObject.Properties["consecutive_failures"]) { $failures = [int]$obj.consecutive_failures }
        if ($obj.PSObject.Properties["skip_cycles"]) { $skip = [int]$obj.skip_cycles }
        return @{ consecutive_failures = $failures; skip_cycles = $skip }
    } catch {
        Write-Log "watchdog_state.json corrompido/invalido -- tratando como padrao (contador=0, skip=0). $($_.Exception.Message)"
        return $default
    }
}

# Escreve o estado como JSON UTF-8 SEM BOM (mesma preocupacao de
# apply-update.ps1::Write-JsonNoBom -- Set-Content -Encoding UTF8 do Windows
# PowerShell 5.1 grava BOM por padrao; embora hoje nenhum leitor Python
# consuma este arquivo, manter o mesmo idioma evita a armadilha caso um dia
# consuma). Escrita atomica (tmp + Move-Item) para nunca deixar o arquivo
# parcialmente escrito se o processo for interrompido no meio.
function Save-WatchdogState {
    param([hashtable]$State)
    $dir = Split-Path -Parent $StateFilePath
    New-Item -ItemType Directory -Path $dir -Force -ErrorAction SilentlyContinue | Out-Null
    $data = @{
        consecutive_failures = $State.consecutive_failures
        skip_cycles          = $State.skip_cycles
        last_updated         = Get-IsoNow
    }
    $json = $data | ConvertTo-Json -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    $tmp = Join-Path $dir ([Guid]::NewGuid().ToString("N") + ".tmp")
    [System.IO.File]::WriteAllText($tmp, $json, $utf8NoBom)
    Move-Item -Path $tmp -Destination $StateFilePath -Force
}

# ── Restart do app ────────────────────────────────────────────────────────────
# Para a task PortalPedidos, espera a porta liberar (ate $PortFreeWaitSec) e,
# se ainda presa, mata o processo dono da porta SOMENTE se o executavel
# estiver sob <AppDir>\.venv\ (nunca mata processo alheio -- mesma guarda de
# scripts/apply-update.ps1::Stop-PortalApp). Religa a task e SEMPRE ativa a
# janela anti-flap no finally, mesmo se algum passo falhar/lancar -- caso
# contrario um restart que falha (ex.: Start-ScheduledTask lanca por algum
# motivo transiente) faria o watchdog tentar de novo no proximo ciclo, 1
# minuto depois, num loop de tentativas em vez de esperar o app estabilizar.
function Restart-PortalApp {
    param([string]$Reason)
    Write-Log "Restart do app iniciado (motivo: $Reason)."
    try {
        $task = Get-ScheduledTask -TaskName $AppTaskName -ErrorAction SilentlyContinue
        if ($task) {
            try {
                Stop-ScheduledTask -TaskName $AppTaskName -ErrorAction Stop
            } catch {
                Write-Log "Stop-ScheduledTask '$AppTaskName': $($_.Exception.Message)"
            }
        } else {
            Write-Log "tarefa '$AppTaskName' nao registrada -- nao ha o que parar; tentando mesmo assim liberar a porta."
        }

        $port = Get-PortalPort
        $deadline = (Get-Date).AddSeconds($PortFreeWaitSec)
        while ((Get-Date) -lt $deadline) {
            $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            if (-not $conns) { break }
            Start-Sleep -Milliseconds 500
        }

        $venvPrefix = Join-Path $AppDir ".venv\"
        $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        foreach ($c in $conns) {
            $procPath = $null
            try {
                $proc = Get-Process -Id $c.OwningProcess -ErrorAction Stop
                $procPath = $proc.Path
            } catch {
                continue
            }
            if ($procPath -and $procPath.StartsWith($venvPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                Write-Log "Matando PID $($c.OwningProcess) ($procPath) para liberar a porta $port."
                Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
            } else {
                Write-Log "Porta $port ocupada por PID $($c.OwningProcess) fora de .venv ($procPath) -- NAO matando (processo alheio)."
            }
        }

        if (-not $task) {
            Write-Log "Sem tarefa '$AppTaskName' registrada -- impossivel religar. Rode setup-service.bat no servidor."
            return
        }

        Start-ScheduledTask -TaskName $AppTaskName
        Write-Log "Tarefa '$AppTaskName' reiniciada."
    } catch {
        Write-Log "ERRO durante o restart: $($_.Exception.Message)"
    } finally {
        # Zera o contador e ativa a janela anti-flap incondicionalmente: a
        # tentativa de restart foi feita (com ou sem sucesso); o proximo
        # health-check so deve contar de novo depois do app ter tempo de
        # subir (spec Secao 8 -- "espera 3 ciclos antes de contar falhas de novo").
        Save-WatchdogState -State @{ consecutive_failures = 0; skip_cycles = $AntiFlapSkipCycles }
        Write-Log "Contador zerado; janela anti-flap ativada ($AntiFlapSkipCycles ciclos)."
    }
}

# ── Execucao principal ────────────────────────────────────────────────────────

try {
    # 1) Lock: update em andamento? A idade e medida pelo LastWriteTime do
    #    arquivo (nao pelo conteudo) -- funciona mesmo que o formato interno
    #    do lock mude no updater, e evita depender de parsing.
    if (Test-Path $LockPath) {
        $lockAgeMin = ((Get-Date).ToUniversalTime() - (Get-Item -Path $LockPath).LastWriteTimeUtc).TotalMinutes
        if ($lockAgeMin -lt $LockOrphanMinutes) {
            Write-Log "update em andamento (lock com $([math]::Round($lockAgeMin, 1)) min) -- watchdog em pausa."
            exit 0
        } else {
            Write-Log "lock orfao detectado (idade $([math]::Round($lockAgeMin, 1)) min > $LockOrphanMinutes min) -- removendo e retomando health-check."
            Remove-Item -Path $LockPath -Force -ErrorAction SilentlyContinue
            # NAO sai -- um updater morto no meio da atualizacao deixa o app
            # parado; o watchdog precisa seguir para o health-check normal
            # (e religar se preciso) na mesma execucao.
        }
    }

    $state = Get-WatchdogState

    # 2) Anti-flap: acabamos de reiniciar o app -- da tempo dele subir (uvicorn
    #    + imports) antes de agir de novo.
    if ($state.skip_cycles -gt 0) {
        $state.skip_cycles -= 1
        Save-WatchdogState -State $state
        Write-Log "janela anti-flap ativa -- pulando ciclo (restam $($state.skip_cycles))."
        exit 0
    }

    # 3) Estado da tarefa: se nem esta Running, religa direto (nao espera as
    #    3 falhas do health-check -- cobre crash com RestartCount esgotado ou
    #    exit limpo).
    $task = Get-ScheduledTask -TaskName $AppTaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        Write-Log "tarefa '$AppTaskName' nao registrada -- nada a monitorar (rode setup-service.bat no servidor)."
        exit 0
    }
    if ($task.State -ne "Running") {
        Write-Log "tarefa '$AppTaskName' nao esta Running (estado atual: $($task.State)) -- religando direto."
        Restart-PortalApp -Reason "task_nao_running"
        exit 0
    }

    # 4) Health-check HTTP. Uma excecao aqui (timeout, conexao recusada,
    #    status != 2xx) e o SINAL ESPERADO de app fora do ar/pendurado -- nao
    #    e um erro do watchdog, entao e capturada e contada como falha, nunca
    #    deixada subir e derrubar o script.
    $port = Get-PortalPort
    $uri = "http://127.0.0.1:$port/health"
    $healthy = $false
    try {
        $resp = Invoke-WebRequest -Uri $uri -TimeoutSec $HealthTimeoutSec -UseBasicParsing -ErrorAction Stop
        $healthy = ($resp.StatusCode -eq 200)
    } catch {
        $healthy = $false
        Write-Log "health-check falhou: $($_.Exception.Message)"
    }

    if ($healthy) {
        if ($state.consecutive_failures -gt 0) {
            Write-Log "health-check OK -- contador resetado (estava em $($state.consecutive_failures))."
        }
        $state.consecutive_failures = 0
        Save-WatchdogState -State $state
    } else {
        $state.consecutive_failures += 1
        Write-Log "falha de health-check consecutiva #$($state.consecutive_failures)."
        if ($state.consecutive_failures -ge $FailureThreshold) {
            Restart-PortalApp -Reason "health_check_${FailureThreshold}x"
        } else {
            Save-WatchdogState -State $state
        }
    }
} catch {
    # Rede de seguranca final: qualquer excecao nao prevista em algum outro
    # nivel (ex.: falha ao criar diretorio, Get-Item lancando por permissao)
    # e logada, nunca propagada -- o watchdog roda a cada 1 minuto para
    # sempre; um crash aqui so deve custar UM ciclo perdido, nunca travar a
    # Tarefa Agendada em estado de erro.
    Write-Log "ERRO inesperado no watchdog: $($_.Exception.Message)"
}
