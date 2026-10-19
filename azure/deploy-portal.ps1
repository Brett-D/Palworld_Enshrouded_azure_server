# Deploys the portal (portal/) to the Function App named in gameservers.config.json.
param([string]$ConfigPath)
. (Join-Path $PSScriptRoot "common.ps1")

$cfg = if ($ConfigPath) { Get-GsConfig $ConfigPath } else { Get-GsConfig }
Use-GsSubscription $cfg

$portal = Join-Path $RepoRoot "portal"
$zip = Join-Path ([IO.Path]::GetTempPath()) "gameservers-portal.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Get-ChildItem $portal -Exclude "__pycache__").FullName -DestinationPath $zip

az functionapp deployment source config-zip -g $cfg.resourceGroup -n $cfg.functionApp --src $zip --build-remote true -o none
$failed = $LASTEXITCODE -ne 0
Remove-Item $zip
if ($failed) { throw "Portal deployment failed" }
Write-Host "Portal deployed: https://$($cfg.functionApp).azurewebsites.net (the first request can take up to a minute)."
