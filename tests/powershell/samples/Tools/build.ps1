param([switch]$Quiet)

Import-Module (Join-Path $PSScriptRoot 'Tools.psm1')
if (-not $Quiet) { Get-Tool -Pattern '*' }
