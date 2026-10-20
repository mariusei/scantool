# Helpers dot-sourced by the release scripts.

function Write-Log {
    param([string]$Message)
    $stamp = Get-Date -Format 'HH:mm:ss'
    Write-Host "[$stamp] $Message"
}

$ToolsRoot = Join-Path $PSScriptRoot 'Tools'
& "$ToolsRoot\build.ps1" -Quiet
