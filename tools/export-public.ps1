# Builds a public copy of this repository with all deployment-specific data removed.
#
#   powershell -ExecutionPolicy Bypass -File tools/export-public.ps1 -Destination ..\gameservermanager-public
#
# - Copies the tracked files except gameservers.config.json (your real settings).
# - Builds a deny-list from your config (IDs, names, IP, email, scrubTerms), saved passwords
#   (*.secrets.json) and, if signed in to Azure, the portal's live password settings.
# - Refuses to finish if any deny-listed value appears anywhere in the copy.
# - Starts a fresh git history under a neutral author, so nothing from the private history
#   (or your git name and email) is carried over.
param(
    [Parameter(Mandatory)][string]$Destination,
    [string]$ConfigPath,
    [string]$RemoteUrl,
    [switch]$ForcePush,
    [string]$AuthorName = "gameservermanager",
    [string]$AuthorEmail = "gameservermanager@users.noreply.github.com",
    [switch]$SkipAzureSecrets
)
$ErrorActionPreference = "Continue"
$repo = Split-Path $PSScriptRoot -Parent
if (-not $ConfigPath) { $ConfigPath = Join-Path $repo "gameservers.config.json" }
if (-not (Test-Path $ConfigPath)) { throw "Private config not found: $ConfigPath" }
$cfg = Get-Content $ConfigPath -Raw | ConvertFrom-Json
$Destination = if ([IO.Path]::IsPathRooted($Destination)) { [IO.Path]::GetFullPath($Destination) } else { [IO.Path]::GetFullPath((Join-Path (Get-Location) $Destination)) }
if ((Test-Path $Destination) -and (Get-ChildItem $Destination -Force | Select-Object -First 1)) { throw "$Destination is not empty" }

# --- Deny-list -------------------------------------------------------------------------------
$terms = New-Object System.Collections.Generic.HashSet[string]([StringComparer]::OrdinalIgnoreCase)
function Add-Term([string]$t) { if ($t -and $t.Trim().Length -ge 4) { [void]$terms.Add($t.Trim()) } }
Add-Term $cfg.subscriptionId; Add-Term $cfg.resourceGroup; Add-Term $cfg.storageAccount; Add-Term $cfg.functionApp
Add-Term $cfg.vm.name; Add-Term $cfg.vm.adminUser; Add-Term (Split-Path $cfg.vm.sshKeyPath -Leaf)
if ($cfg.vm.sshAllowedIp -ne "auto") { Add-Term $cfg.vm.sshAllowedIp }
Add-Term $cfg.budget.alertEmail
if ($cfg.budget.alertEmail) { Add-Term ($cfg.budget.alertEmail -split "@")[0] }
foreach ($t in @($cfg.scrubTerms)) { Add-Term $t }
Add-Term (git -C $repo config user.name); Add-Term (git -C $repo config user.email)
Get-ChildItem $repo -Filter "*.secrets.json" -ErrorAction SilentlyContinue | ForEach-Object {
    (Get-Content $_.FullName -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { Add-Term "$($_.Value)" }
}
if (-not $SkipAzureSecrets) {
    $settings = az functionapp config appsettings list -g $cfg.resourceGroup -n $cfg.functionApp -o json 2>$null
    if ($LASTEXITCODE -eq 0 -and $settings) {
        foreach ($s in ($settings -join "`n" | ConvertFrom-Json)) {
            if ($s.name -match "PASSWORD|ADDRESS") { Add-Term ($s.value -split ":")[0]; Add-Term $s.value }
        }
    } else {
        Write-Warning "Could not read the portal's settings from Azure (run 'az login'); live passwords are not in the deny-list."
    }
}
Write-Host "Deny-list has $($terms.Count) terms."

# --- Copy --------------------------------------------------------------------------------------
$files = git -C $repo ls-files | Where-Object { $_ -ne "gameservers.config.json" -and $_ -notmatch '\.secrets\.json$' }
foreach ($f in $files) {
    $target = Join-Path $Destination $f
    New-Item -ItemType Directory -Force (Split-Path $target) | Out-Null
    Copy-Item (Join-Path $repo $f) $target
}
# Public users keep their own config and passwords out of git.
Add-Content (Join-Path $Destination ".gitignore") "`n# Your deployment settings and generated passwords`ngameservers.config.json`n*.secrets.json"

# --- Scan --------------------------------------------------------------------------------------
$hits = @()
foreach ($file in Get-ChildItem $Destination -Recurse -File) {
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($t in $terms) {
        if ($text.IndexOf($t, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            $hits += "$($file.FullName.Substring($Destination.Length + 1)): contains a deny-listed value ('$($t.Substring(0, [Math]::Min(3, $t.Length)))...')"
        }
    }
}
if ($hits) {
    $hits | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    throw "Personal data found in the public copy. Fix the files above (the copy was left in $Destination for inspection, with no git history)."
}
Write-Host "Scan clean: no deny-listed values in $($files.Count) files." -ForegroundColor Green

# --- Fresh history -------------------------------------------------------------------------------
# A neutral identity, also set on the new repo, keeps your git name and email out of the public history.
git -C $Destination init -q -b main
git -C $Destination config user.name $AuthorName
git -C $Destination config user.email $AuthorEmail
git -C $Destination add -A
git -C $Destination commit -q -m "Initial public release"
$author = git -C $Destination log -1 --format="%an <%ae> %cn <%ce>"
foreach ($t in $terms) {
    if ($author.IndexOf($t, [StringComparison]::OrdinalIgnoreCase) -ge 0) { throw "The public commit author contains personal data; pass -AuthorName/-AuthorEmail" }
}
if ($RemoteUrl) {
    git -C $Destination remote add origin $RemoteUrl
    # Each export is a fresh single-commit history, so updating an existing public repo replaces it.
    if ($ForcePush) { git -C $Destination push -u --force origin main } else { git -C $Destination push -u origin main }
    if ($LASTEXITCODE -ne 0) { throw "Push failed. To replace an existing public repo's history, re-run with -ForcePush." }
}
Write-Host "Public copy ready in $Destination"
