<#
.SYNOPSIS
Publish the release packages to the staging share.

.EXAMPLE
./basic.ps1 -Version 1.2.0
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$Version,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$script:StagingShare = '\\staging\releases'
$Retries = 3

. $PSScriptRoot\Common.ps1
Import-Module "$PSScriptRoot/Tools/Tools.psd1" -Force

enum Channel { Stable; Preview }

class Package {
    [string]$Name
    [Channel]$Channel

    Package([string]$name) {
        $this.Name = $name
        $this.Channel = [Channel]::Stable
    }

    [string] Describe() {
        return Format-PackageName -Name $this.Name
    }
}

# Format a package name for the log.
function Format-PackageName([string]$Name) {
    "$Name ($Version)"
}

function Copy-Package {
    <#
    .SYNOPSIS
    Copy one package, retrying on failure.
    #>
    param([Package]$Package, [int]$Attempts = $Retries)

    function Test-Share {
        Test-Path -Path $script:StagingShare
    }

    if (-not (Test-Share)) {
        throw "share not reachable"
    }
    foreach ($attempt in 1..$Attempts) {
        try {
            Copy-Item -Path $Package.Name -Destination $script:StagingShare
            Write-Log "copied $($Package.Describe())"
            return
        }
        catch {
            Write-Log "attempt $attempt failed"
        }
    }
}

filter Select-Preview {
    if ($_.Channel -eq [Channel]::Preview) { $_ }
}

function Invoke-Publish {
    $packages = Get-Tool -Pattern '*.nupkg' | ForEach-Object { [Package]::new($_) }
    $packages | Select-Preview | ForEach-Object { Copy-Package -Package $_ }
    Register-ArgumentCompleter -CommandName Copy-Package -ScriptBlock ${function:Format-PackageName}
}

if (-not $DryRun) {
    Invoke-Publish
}
