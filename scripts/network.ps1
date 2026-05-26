#Requires -Version 5.1
<#
.SYNOPSIS
    Helpers de rede compartilhados do Portal de Pedidos.
.DESCRIPTION
    Dot-source este arquivo para usar:
      - Set-PortalFirewallRule  : libera a porta na rede LOCAL (perfis Particular/Dominio)
      - Remove-PortalFirewallRule : remove a regra
      - Get-LanIp               : descobre o IP da maquina na rede local

    A regra de firewall e criada apenas para os perfis Private e Domain.
    O perfil Public (redes nao confiaveis) NUNCA e liberado, entao o Portal
    nao fica acessivel fora da rede local mesmo escutando em 0.0.0.0.
#>

$script:PortalFirewallRule = "Portal de Pedidos"

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Executa um trecho de PowerShell elevado (UAC), aguarda e propaga falha.
function Invoke-Elevated([string]$Script) {
    $bytes = [System.Text.Encoding]::Unicode.GetBytes($Script)
    $enc   = [Convert]::ToBase64String($bytes)
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", $enc
    if ($p.ExitCode -ne 0) { throw "processo elevado retornou codigo $($p.ExitCode)" }
}

function Set-PortalFirewallRule([int]$Port) {
    $rule  = $script:PortalFirewallRule
    $inner = @"
Get-NetFirewallRule -DisplayName '$rule' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName '$rule' -Description 'Acesso ao Portal de Pedidos na rede local' -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Private,Domain | Out-Null
"@
    if (Test-IsAdmin) { Invoke-Expression $inner } else { Invoke-Elevated $inner }
}

function Remove-PortalFirewallRule {
    $rule  = $script:PortalFirewallRule
    $inner = @"
Get-NetFirewallRule -DisplayName '$rule' -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
"@
    if (Test-IsAdmin) { Invoke-Expression $inner } else { Invoke-Elevated $inner }
}

# Retorna o IPv4 da maquina na rede local, ou $null se nao encontrar.
function Get-LanIp {
    try {
        $ip = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object {
                $_.IPAddress -ne "127.0.0.1" -and
                $_.IPAddress -notlike "169.254.*" -and
                $_.PrefixOrigin -ne "WellKnown"
            } |
            Sort-Object SkipAsSource |
            Select-Object -First 1
        if ($ip) { return $ip.IPAddress }
    } catch {}
    return $null
}
