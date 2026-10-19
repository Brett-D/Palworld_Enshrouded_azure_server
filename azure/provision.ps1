# Creates a complete game server deployment from gameservers.config.json:
# resource group, storage (backups + config with 14-day backup retention), network, VM with Docker,
# the manager scripts, the portal Function App, managed identities with least-privilege roles,
# nightly auto-shutdown and an optional budget alert.
#
# Generated passwords are saved next to the config as <name>.secrets.json (git-ignored).
# Safe to re-run: existing resources are reused and saved passwords are kept.
param(
    [string]$ConfigPath = (Join-Path (Split-Path $PSScriptRoot -Parent) "gameservers.config.json"),
    [switch]$SkipBudget
)
. (Join-Path $PSScriptRoot "common.ps1")

$cfg = Get-GsConfig $ConfigPath
$rg = $cfg.resourceGroup; $loc = $cfg.location; $sa = $cfg.storageAccount; $app = $cfg.functionApp
$vmName = $cfg.vm.name; $games = @($cfg.games)
$secretsPath = ($ConfigPath -replace '\.config\.json$', '') + ".secrets.json"
if ($secretsPath -eq $ConfigPath) { $secretsPath = "$ConfigPath.secrets.json" }

function Step([string]$Text) { Write-Host "`n==> $Text" -ForegroundColor Cyan }
function Exists([scriptblock]$Query) { $out = & $Query 2>$null; return ($LASTEXITCODE -eq 0 -and $out) }
function Assign-Role([string]$Principal, [string]$Role, [string]$Scope) {
    $existing = az role assignment list --assignee $Principal --role $Role --scope $Scope --query "[0].id" -o tsv 2>$null
    if (-not $existing) {
        az role assignment create --assignee-object-id $Principal --assignee-principal-type ServicePrincipal --role $Role --scope $Scope -o none
        if ($LASTEXITCODE -ne 0) { throw "Could not assign '$Role'" }
    }
}

Step "Checking subscription and names"
Use-GsSubscription $cfg
$subId = $cfg.subscriptionId
$appExists = Exists { az functionapp show -g $rg -n $app --query id -o tsv }
if ($appExists -and -not (Test-Path $secretsPath)) {
    throw "Function App '$app' already exists but $secretsPath is missing. Provisioning would replace the live passwords; use deploy-vm.ps1 / deploy-portal.ps1 to update an existing deployment."
}
if (-not (Exists { az storage account show -g $rg -n $sa --query id -o tsv })) {
    if ((az storage account check-name -n $sa --query nameAvailable -o tsv) -ne "true") { throw "Storage account name '$sa' is taken; pick another in the config" }
}
if (-not $appExists) {
    $body = @{ name = $app; type = "Microsoft.Web/sites" } | ConvertTo-Json -Compress
    $tmp = New-TemporaryFile; [IO.File]::WriteAllText($tmp, $body)
    $avail = az rest --method post --url "https://management.azure.com/subscriptions/$subId/providers/Microsoft.Web/checknameavailability?api-version=2022-03-01" --body "@$tmp" --query nameAvailable -o tsv
    Remove-Item $tmp
    if ($avail -ne "true") { throw "Function App name '$app' is taken; pick another in the config" }
}
foreach ($ns in "Microsoft.Compute", "Microsoft.Network", "Microsoft.Storage", "Microsoft.Web", "Microsoft.Consumption") {
    if ((az provider show -n $ns --query registrationState -o tsv) -ne "Registered") { az provider register -n $ns --wait -o none }
}

Step "Passwords"
if (Test-Path $secretsPath) {
    $secrets = Get-Content $secretsPath -Raw | ConvertFrom-Json
    Write-Host "Using saved passwords from $secretsPath"
} else {
    $secrets = [pscustomobject]@{
        portalPassword = New-GsPassword 20
        playerPassword = New-GsPassword 12
        joinPassword   = New-GsPassword 10
        adminPassword  = New-GsPassword 14
    }
    $secrets | ConvertTo-Json | Set-Content $secretsPath
    Write-Host "Generated new passwords and saved them to $secretsPath (keep this file private)"
}

Step "Resource group $rg"
az group create -n $rg -l $loc --tags purpose=gameservers -o none

Step "Storage account $sa"
if (-not (Exists { az storage account show -g $rg -n $sa --query id -o tsv })) {
    az storage account create -g $rg -n $sa -l $loc --sku Standard_LRS --kind StorageV2 --allow-blob-public-access false --min-tls-version TLS1_2 -o none
    if ($LASTEXITCODE -ne 0) { throw "Storage account creation failed" }
}
$saId = az storage account show -g $rg -n $sa --query id -o tsv
foreach ($c in "backups", "config") { az storage container-rm create --storage-account $sa -g $rg -n $c --public-access off -o none }
az storage account management-policy create --account-name $sa -g $rg --policy "@$(Join-Path $PSScriptRoot 'lifecycle-policy.json')" -o none
# Deleted or overwritten blobs (backups, settings, roles) stay recoverable for 7 days.
Invoke-AzCli storage account blob-service-properties update --account-name $sa -g $rg `
    --enable-delete-retention true --delete-retention-days 7 `
    --enable-container-delete-retention true --container-delete-retention-days 7 -o none

Step "Network"
$allowedIp = $cfg.vm.sshAllowedIp
if (-not $allowedIp -or $allowedIp -eq "auto") { $allowedIp = (Invoke-RestMethod "https://api.ipify.org").Trim() }
az network nsg create -g $rg -n "$vmName-nsg" -l $loc -o none
az network nsg rule create -g $rg --nsg-name "$vmName-nsg" -n allow-ssh --priority 110 --protocol Tcp --destination-port-ranges 22 --source-address-prefixes $allowedIp --access Allow --direction Inbound -o none
$prio = 100
foreach ($g in $games) {
    az network nsg rule create -g $rg --nsg-name "$vmName-nsg" -n "allow-$g" --priority $prio --protocol Udp --destination-port-ranges $GamePorts[$g] --access Allow --direction Inbound -o none
    $prio++
}
az network public-ip create -g $rg -n "$vmName-ip" -l $loc --sku Standard --allocation-method Static -o none
az network vnet create -g $rg -n "$vmName-vnet" -l $loc --address-prefixes 10.10.0.0/16 --subnet-name default --subnet-prefixes 10.10.0.0/24 --network-security-group "$vmName-nsg" -o none
$ip = az network public-ip show -g $rg -n "$vmName-ip" --query ipAddress -o tsv
Write-Host "Static IP: $ip   SSH allowed from: $allowedIp"

Step "VM $vmName"
$key = $cfg.vm.sshKeyPath
if (-not (Test-Path $key)) {
    New-Item -ItemType Directory -Force (Split-Path $key) | Out-Null
    ssh-keygen -t ed25519 -f $key -N '""' -C gameservers | Out-Null
    Write-Host "Created SSH key $key"
}
if (-not (Exists { az vm show -g $rg -n $vmName --query id -o tsv })) {
    az vm create -g $rg -n $vmName -l $loc --size $cfg.vm.size --image $cfg.vm.image --accelerated-networking true `
        --admin-username $cfg.vm.adminUser --ssh-key-values "$key.pub" --authentication-type ssh `
        --vnet-name "$vmName-vnet" --subnet default --nsg '""' --public-ip-address "$vmName-ip" `
        --os-disk-size-gb $cfg.vm.osDiskGb --storage-sku StandardSSD_LRS --assign-identity '[system]' `
        --custom-data (Join-Path $RepoRoot "vm\cloud-init.yaml") --tags purpose=gameservers -o none
    if ($LASTEXITCODE -ne 0) { throw "VM creation failed" }
} else {
    az vm start -g $rg -n $vmName -o none
}
$vmId = az vm show -g $rg -n $vmName --query id -o tsv
$vmPrincipal = az vm show -g $rg -n $vmName --query identity.principalId -o tsv
Assign-Role $vmPrincipal "Virtual Machine Contributor" $vmId
foreach ($c in "backups", "config") { Assign-Role $vmPrincipal "Storage Blob Data Contributor" "$saId/blobServices/default/containers/$c" }
az vm auto-shutdown -g $rg -n $vmName --time $cfg.power.nightlyShutdownUtc -o none

Step "Waiting for the VM to finish first-boot setup"
for ($i = 0; $i -lt 40; $i++) {
    $r = ssh -i $key -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 -o BatchMode=yes "$($cfg.vm.adminUser)@$ip" "cloud-init status --wait >/dev/null; command -v docker" 2>$null
    if ($LASTEXITCODE -eq 0 -and $r) { break }
    Start-Sleep 15
}
if (-not $r) { throw "The VM did not become reachable over SSH from $allowedIp" }

Step "Game server config files"
foreach ($g in $games) {
    $text = [IO.File]::ReadAllText((Join-Path $RepoRoot "vm\$g\docker-compose.yml"))
    $text = $text.Replace("__TIMEZONE__", $cfg.timezone).Replace("__SERVER_PASSWORD__", $secrets.joinPassword).Replace("__ADMIN_PASSWORD__", $secrets.adminPassword)
    if ($g -eq "palworld") { $text = $text.Replace("__MAX_PLAYERS__", [string][int]$cfg.palworld.maxPlayers).Replace("__DESCRIPTION__", $cfg.palworld.description) }
    if ($text -match "__[A-Z_]+__") { throw "Unfilled placeholder in $g compose file" }
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($text -replace "`r", "")))
    Invoke-GsSsh $cfg $ip @"
set -e
sudo mkdir -p /opt/$g
echo '$b64' | base64 -d | sudo tee /opt/$g/docker-compose.yml >/dev/null
sudo chmod 600 /opt/$g/docker-compose.yml
sudo grep -q '^services:' /opt/$g/docker-compose.yml || { echo "compose file for $g is empty or invalid" >&2; exit 1; }
cd /opt/$g && sudo docker compose config -q && sudo docker compose pull -q
echo "$g ready"
"@
}

Step "Manager scripts"
& (Join-Path $PSScriptRoot "deploy-vm.ps1") -ConfigPath $ConfigPath -VmHost $ip

Step "Portal Function App $app"
if (-not $appExists) {
    az functionapp create -g $rg -n $app -s $sa --flexconsumption-location $loc --runtime python --runtime-version 3.12 `
        --instance-memory 512 --maximum-instance-count 2 --tags purpose=gameservers -o none
    if ($LASTEXITCODE -ne 0) { throw "Function App creation failed" }
}
$appPrincipal = az functionapp identity assign -g $rg -n $app --query principalId -o tsv
# Flex Consumption runs Python apps one request at a time per instance by default. The portal's calls
# just wait on Azure APIs, so let one instance serve the page's parallel requests.
Invoke-AzCli functionapp scale config set -g $rg -n $app --trigger-type http --trigger-settings perInstanceConcurrency=16 -o none
Assign-Role $appPrincipal "Virtual Machine Contributor" $vmId
Assign-Role $appPrincipal "Storage Blob Data Contributor" "$saId/blobServices/default/containers/config"
Assign-Role $appPrincipal "Storage Blob Data Reader" "$saId/blobServices/default/containers/backups"
Assign-Role $appPrincipal "Storage Blob Delegator" $saId

$settings = [ordered]@{
    VM_SUBSCRIPTION_ID = $subId; VM_RESOURCE_GROUP = $rg; VM_NAME = $vmName
    STORAGE_ACCOUNT = $sa; BACKUP_CONTAINER = "backups"; CONFIG_CONTAINER = "config"
    ENABLED_GAMES = ($games -join ",")
    PORTAL_PASSWORD = $secrets.portalPassword; PLAYER_PASSWORD = $secrets.playerPassword
    SERVER_ADDRESS = "${ip}:8211"; SERVER_PASSWORD = $secrets.joinPassword
    ENSHROUDED_ADDRESS = "${ip}:15637"; ENSHROUDED_PASSWORD = $secrets.joinPassword
}
$tmp = New-TemporaryFile; [IO.File]::WriteAllText($tmp, ($settings | ConvertTo-Json -Compress))
az functionapp config appsettings set -g $rg -n $app --settings "@$tmp" -o none
Remove-Item $tmp
& (Join-Path $PSScriptRoot "deploy-portal.ps1") -ConfigPath $ConfigPath

Step "Saving default game settings and roles through the portal"
$portal = "https://$app.azurewebsites.net"
$hdr = @{ "x-portal-key" = $secrets.portalPassword }
function Portal([string]$Path, [string]$Method = "GET", $Body = $null) {
    for ($i = 0; $i -lt 12; $i++) {
        try {
            if ($Body) { return Invoke-RestMethod "$portal/api/$Path" -Method $Method -Headers $hdr -ContentType "application/json" -Body ($Body | ConvertTo-Json -Depth 6) -TimeoutSec 120 }
            return Invoke-RestMethod "$portal/api/$Path" -Method $Method -Headers $hdr -TimeoutSec 120
        } catch { Start-Sleep 15 }
    }
    throw "The portal did not respond to /api/$Path. Role assignments can take a few minutes; re-run provision.ps1."
}
foreach ($g in $games) {
    $s = Portal "settings?game=$g"
    $values = [ordered]@{}; foreach ($f in $s.schema) { $values[$f.key] = $s.values.($f.key) }
    Portal "settings" "POST" @{ game = $g; values = $values; applyNow = $false } | Out-Null
}
if ($games -contains "enshrouded") {
    $existing = Portal "roles"
    if (-not $existing.roles) {
        $perm = { param($kick) [ordered]@{ canKickBan = $kick; canAccessInventories = $true; canEditWorld = $true; canEditBase = $true; canExtendBase = $true } }
        $admins = [ordered]@{ name = "Admins"; password = $secrets.adminPassword; reservedSlots = 1 }
        $friends = [ordered]@{ name = "Friends"; password = $secrets.joinPassword; reservedSlots = 0 }
        foreach ($kv in (& $perm $true).GetEnumerator()) { $admins[$kv.Key] = $kv.Value }
        foreach ($kv in (& $perm $false).GetEnumerator()) { $friends[$kv.Key] = $kv.Value }
        Portal "roles" "POST" @{ roles = @($admins, $friends); public = "Friends"; applyNow = $false } | Out-Null
    }
}

if (-not $SkipBudget -and $cfg.budget.alertEmail) {
    Step "Budget alert"
    $start = (Get-Date -Day 1).ToString("yyyy-MM-01T00:00:00Z")
    $notify = @{}
    foreach ($t in @(@("actual50", 50, "Actual"), @("actual80", 80, "Actual"), @("actual100", 100, "Actual"), @("forecast100", 100, "Forecasted"))) {
        $notify[$t[0]] = @{ enabled = $true; operator = "GreaterThan"; threshold = $t[1]; thresholdType = $t[2]; contactEmails = @($cfg.budget.alertEmail) }
    }
    $budget = @{ properties = @{ category = "Cost"; amount = [double]$cfg.budget.monthlyAmount; timeGrain = "Monthly"
            timePeriod = @{ startDate = $start; endDate = (Get-Date -Day 1).AddYears(2).ToString("yyyy-MM-01T00:00:00Z") }; notifications = $notify } }
    $tmp = New-TemporaryFile; [IO.File]::WriteAllText($tmp, ($budget | ConvertTo-Json -Depth 6))
    az rest --method put --url "https://management.azure.com/subscriptions/$subId/resourceGroups/$rg/providers/Microsoft.Consumption/budgets/gameservers-budget?api-version=2023-05-01" --body "@$tmp" -o none
    Remove-Item $tmp
}

Step "Done"
Write-Host @"
Portal:            $portal
Admin password:    $($secrets.portalPassword)
Player password:   $($secrets.playerPassword)   (can only view server info and press Start)
Game join password: $($secrets.joinPassword)
Game admin password: $($secrets.adminPassword)  (Palworld /AdminPassword, Enshrouded Admins role)
Server address:    $ip  (Palworld :8211, Enshrouded :15637)
SSH:               ssh -i $key $($cfg.vm.adminUser)@$ip
Passwords are saved in $secretsPath. The VM shuts itself down about 10 minutes after boot until a game is started from the portal.
"@
