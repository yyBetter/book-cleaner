<#
.SYNOPSIS
    Open Windows Defender Firewall TCP port 8765 on Private LAN for Android wireless scanning.
#>
$RuleName = "BookCleaner-LAN-8765"
$Port = 8765

$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    Write-Host "Firewall rule '$RuleName' already exists." -ForegroundColor Green
} else {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -LocalPort $Port `
        -Protocol TCP `
        -Action Allow `
        -Profile Private | Out-Null
    Write-Host "Created inbound firewall rule '$RuleName' for TCP port $Port (Private profile)." -ForegroundColor Green
}
