# Shared helpers for the deploy and provisioning scripts. Dot-source this file.
# Windows PowerShell treats native stderr as errors under "Stop", so az/ssh failures are detected
# through exit codes instead (Invoke-AzCli, Invoke-GsSsh).
$ErrorActionPreference = "Continue"

$script:RepoRoot = Split-Path $PSScriptRoot -Parent
$script:SupportedGames = @("palworld", "enshrouded")
$script:GamePorts = @{ palworld = 8211; enshrouded = 15637 }

function Invoke-AzCli {
    az @args
    if ($LASTEXITCODE -ne 0) { throw "az $($args[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
}

function Expand-HomePath([string]$Path) {
    if ($Path.StartsWith("~")) { return Join-Path $HOME $Path.Substring(1).TrimStart('\', '/') }
    return $Path
}

function Get-GsConfig([string]$Path = (Join-Path $script:RepoRoot "gameservers.config.json")) {
    if (-not (Test-Path $Path)) {
        throw "Config not found: $Path. Copy gameservers.config.example.json to gameservers.config.json and fill it in."
    }
    $cfg = Get-Content $Path -Raw | ConvertFrom-Json
    foreach ($field in "subscriptionId", "location", "resourceGroup", "storageAccount", "functionApp", "timezone") {
        if (-not $cfg.$field) { throw "Config is missing '$field'" }
    }
    if ($cfg.subscriptionId -eq "00000000-0000-0000-0000-000000000000") { throw "Set subscriptionId in $Path" }
    if ($cfg.storageAccount -notmatch '^[a-z0-9]{3,24}$') { throw "storageAccount must be 3-24 lowercase letters and numbers" }
    if ($cfg.functionApp -notmatch '^[a-zA-Z0-9][a-zA-Z0-9-]{1,58}[a-zA-Z0-9]$') { throw "functionApp must be 3-60 letters, numbers or hyphens" }
    $games = @($cfg.games)
    if ($games.Count -eq 0) { throw "Config 'games' must list at least one game" }
    foreach ($g in $games) { if ($script:SupportedGames -notcontains $g) { throw "Unsupported game '$g' (supported: $($script:SupportedGames -join ', '))" } }
    $cfg.vm.sshKeyPath = Expand-HomePath $cfg.vm.sshKeyPath
    if (-not $cfg.vm.image) { $cfg.vm | Add-Member -NotePropertyName image -NotePropertyValue "Canonical:ubuntu-26_04-lts:minimal:latest" }
    return $cfg
}

function Use-GsSubscription($Config) {
    az account set --subscription $Config.subscriptionId
    if ($LASTEXITCODE -ne 0) { throw "Could not select subscription $($Config.subscriptionId). Run 'az login' first." }
}

function Get-GsVmIp($Config) {
    $ip = az vm show -d -g $Config.resourceGroup -n $Config.vm.name --query publicIps -o tsv
    if (-not $ip) { throw "Could not find the VM's public IP" }
    return $ip
}

# Contents of /etc/gameservers.conf on the VM. Values can still be overridden by the environment.
function Get-GsVmConf($Config) {
    @(
        "STORAGE_ACCOUNT=$($Config.storageAccount)"
        "GAMES=`"$(@($Config.games) -join ' ')`""
        "IDLE_MINUTES=`${IDLE_MINUTES:-$([int]$Config.power.idleMinutes)}"
        "MAX_UPTIME_HOURS=`${MAX_UPTIME_HOURS:-$([int]$Config.power.maxUptimeHours)}"
    ) -join "`n"
}

function New-GsPassword([int]$Length = 16) {
    $chars = [char[]]"abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    $bytes = New-Object byte[] $Length
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Invoke-GsSsh($Config, [string]$VmHost, [string]$Script) {
    ($Script -replace "`r", "") | ssh -i $Config.vm.sshKeyPath -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 "$($Config.vm.adminUser)@$VmHost" "bash -s"
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed on $VmHost" }
}
