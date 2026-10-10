<#
  Opens TCP 8410 (Ten Forward) to the LAN and the tailnet ONLY.
  Windows often categorises a home LAN as "Public", so the rule must cover every profile
  and is instead scoped by RemoteAddress.
  Run elevated:  powershell -ExecutionPolicy Bypass -File tools\install_firewall.ps1
#>
$ErrorActionPreference = 'Stop'
$name = 'Ten Forward 8410 (LAN + tailnet)'

$id = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $id.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error 'Run this from an elevated PowerShell (Run as administrator).'
    exit 1
}

Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue | Remove-NetFirewallRule

New-NetFirewallRule `
    -DisplayName $name `
    -Description 'Ten Forward music lounge web UI. LAN + Tailscale peers only, never the open internet.' `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort 8410 `
    -Profile Any `
    -RemoteAddress LocalSubnet, '100.64.0.0/10' | Out-Null

Get-NetFirewallRule -DisplayName $name |
    Select-Object DisplayName, Enabled, Direction, Action, Profile |
    Format-List | Out-String | Write-Host
Write-Host 'FIREWALL_RULE_OK'
