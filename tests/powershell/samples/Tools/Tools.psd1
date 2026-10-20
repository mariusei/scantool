@{
    RootModule        = 'Tools.psm1'
    ModuleVersion     = '1.0.0'
    FunctionsToExport = @('Get-Tool')
    PrivateData       = @{
        PSData = @{ Tags = @('release') }
    }
}
