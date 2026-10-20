function Get-Tool {
    param([string]$Pattern)
    Resolve-ToolPath | Get-ChildItem -Filter $Pattern
}

function Resolve-ToolPath {
    Join-Path $PSScriptRoot 'bin'
}

function Remove-StaleTool {
    Get-ChildItem -Path (Resolve-ToolPath) | Remove-Item
}

Export-ModuleMember -Function Get-Tool
