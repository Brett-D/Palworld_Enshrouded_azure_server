# Installs the manager scripts, systemd units and /etc/gameservers.conf on the VM.
# The VM must be running (start any game from the portal first).
# Compose files are not touched here because the live copies contain passwords (see provision.ps1).
param([string]$ConfigPath, [string]$VmHost)
. (Join-Path $PSScriptRoot "common.ps1")

$cfg = if ($ConfigPath) { Get-GsConfig $ConfigPath } else { Get-GsConfig }
Use-GsSubscription $cfg
if (-not $VmHost) { $VmHost = Get-GsVmIp $cfg }

$vm = Join-Path $RepoRoot "vm"
$files = @(Get-ChildItem "$vm\scripts", "$vm\systemd" -File | ForEach-Object FullName)
$target = "$($cfg.vm.adminUser)@$VmHost"

ssh -i $cfg.vm.sshKeyPath -o StrictHostKeyChecking=accept-new $target "rm -rf /tmp/gsm && mkdir -p /tmp/gsm"
scp -i $cfg.vm.sshKeyPath @files "${target}:/tmp/gsm/"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

$confB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes((Get-GsVmConf $cfg) + "`n"))
Invoke-GsSsh $cfg $VmHost @"
set -e
cd /tmp/gsm
sed -i 's/\r`$//' *
for f in *.sh; do bash -n "`$f"; done
python3 -m py_compile a2s-players.py enshrouded-players.py
# The config goes in first so the new scripts can find it.
echo '$confB64' | base64 -d | sudo tee /etc/gameservers.conf >/dev/null
sudo install -d /usr/local/lib
sudo install -m 0644 games-common.sh /usr/local/lib/
sudo install -m 0755 game-backup.sh game-restore.sh games-reconcile.sh game-apply-settings.sh game-admin.sh games-sampler.sh game-start-watch.sh /usr/local/bin/
sudo install -m 0755 a2s-players.py /usr/local/bin/a2s-players
sudo install -m 0755 enshrouded-players.py /usr/local/bin/enshrouded-players
sudo install -m 0644 *.service *.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now games-reconcile.timer games-backup.timer
sudo systemctl enable games-sampler.service
sudo systemctl restart games-sampler.service
rm -rf /tmp/gsm
echo "VM scripts installed on $VmHost"
"@
