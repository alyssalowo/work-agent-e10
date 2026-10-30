$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -ne 5) { throw 'E10 client components require Windows PowerShell 5.1 (.NET Framework).' }
$e10ConnectionConfiguration = Get-Content -LiteralPath "$PSScriptRoot\connection.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$e10ClientDirectory = $e10ConnectionConfiguration.client_directory
if (-not ('E10ClientAssemblyResolver' -as [type])) {
    Add-Type -Path "$PSScriptRoot\ClientAssemblyResolver.cs"
}
[E10ClientAssemblyResolver]::Initialize($e10ClientDirectory)
if (-not ('E10CredentialStore' -as [type])) {
    Add-Type -Path "$PSScriptRoot\CredentialStore.cs" -ReferencedAssemblies @('System.dll','System.Core.dll','System.Security.dll','System.Web.Extensions.dll')
}
$e10References = @('System.dll','System.Core.dll','System.Web.Extensions.dll','System.ServiceModel.dll','System.Data.dll','System.Xml.dll')
$e10References += @('Digiwin.Mars.Common.dll','Digiwin.Mars.Common.Advanced.dll','Digiwin.Mars.Core.dll','Digiwin.Mars.Network.dll','Digiwin.Mars.AccountSets.dll','Digiwin.Mars.Business.dll','Digiwin.Mars.Business.Utils.dll','Digiwin.Mars.ApplicationUtils.Common.dll','Digiwin.Mars.Torridity.dll','Digiwin.Mars.Utils.dll') | ForEach-Object { Join-Path $e10ClientDirectory ('System\' + $_) }
foreach ($e10Reference in $e10References) {
    if (Test-Path -LiteralPath $e10Reference) { [Reflection.Assembly]::LoadFrom($e10Reference) | Out-Null }
}
$e10References += Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.MO_B002.Business.dll'
$e10References += Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.Common.Business.dll'
[Reflection.Assembly]::LoadFrom((Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.Common.Business.dll')) | Out-Null
[Reflection.Assembly]::LoadFrom((Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.MO_B002.Business.dll')) | Out-Null
$e10References += Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.GenerateIssueReturnService.Business.dll'
[Reflection.Assembly]::LoadFrom((Join-Path $e10ClientDirectory 'Programs\Digiwin.ERP.GenerateIssueReturnService.Business.dll')) | Out-Null
foreach ($e10ManualDll in @('Digiwin.ERP.MO.Business.dll','Digiwin.ERP.CommonManufacture.Business.dll')) {
    $e10ManualPath = Join-Path $e10ClientDirectory ('Programs\' + $e10ManualDll)
    $e10References += $e10ManualPath
    [Reflection.Assembly]::LoadFrom($e10ManualPath) | Out-Null
}
if (-not ('E10AccountSession' -as [type])) {
    Add-Type -Path @("$PSScriptRoot\E10AccountSession.cs", "$PSScriptRoot\E10BusinessRead.cs", "$PSScriptRoot\E10WriteProbe.cs", "$PSScriptRoot\E10DocumentOperations.cs", "$PSScriptRoot\E10OrderGeneration.cs", "$PSScriptRoot\E10ProductionReturn.cs", "$PSScriptRoot\E10ManualWorkorder.cs", "$PSScriptRoot\E10PickingWorkflow.cs", "$PSScriptRoot\E10OrderBatch.cs") -ReferencedAssemblies $e10References
}
[Digiwin.Common.Core.SystemInfo]::SetAccountSetId([string]$e10ConnectionConfiguration.account_set_id)
