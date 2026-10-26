param([Parameter(Mandatory=$true)][string]$CredentialPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$owner = ([IO.File]::GetAccessControl($CredentialPath)).GetOwner([Security.Principal.SecurityIdentifier]).Value
@{current_sid=$identity.User.Value; owner_sid=$owner; same_user=($identity.User.Value -eq $owner)} | ConvertTo-Json -Compress
