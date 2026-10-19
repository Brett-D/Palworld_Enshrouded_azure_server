# Game Server Manager

Run **Palworld** and/or **Enshrouded** dedicated servers on a single Azure VM that is only on (and billed)
while people are playing, managed from a small web portal.

- One click to start a game; the VM boots, the game starts, and it shuts itself down again after
  20 idle minutes. When no game is running the VM is deallocated, so you only pay for the disk and IP.
- Both games can run at the same time on one VM and one static IP.
- Automatic world backups (on every shutdown, every 3 hours, or on demand) with 14-day retention,
  one-click restore and download.
- Game settings, server names, Enshrouded roles and Palworld kick/ban from the browser.
- Live CPU and memory charts per game, with 5-second peaks and overload warnings, so you can tell when the
  VM is too small.
- Two portal passwords: an admin password for everything and a player password that can see server info,
  who is online and server usage, and press Start.

## How it works

```
 Browser ──► Portal (Azure Function, Python) ──► Azure Storage "config" container
                 │  start VM / run commands          desired-<game>, settings, roles, status.json
                 ▼                                              ▲
           Azure VM (Ubuntu 26.04, Docker) ─ games-reconcile.sh ┘  (every minute)
             ├─ palworld   (UDP 8211)                 └─► "backups" container (14-day retention)
             └─ enshrouded (UDP 15637)
```

- **Start**: the portal writes `config/desired-<game>` and starts the VM if it is off. On the VM, a sampler
  notices start and stop requests within about 10 seconds; the manager also runs 5 seconds after boot.
- **Start progress**: while a game starts, `vm/scripts/game-start-watch.sh` reads its log every 3 seconds
  and publishes the current step (preparing, checking for updates or downloading with a percentage,
  starting, loading the world) to `config/progress-<game>.json`, which the portal's Start button shows.
  "Save and restart now" uses the same file: `game-apply-settings.sh` publishes its own steps (warning
  Palworld players, backing up, stopping), then hands over to the watcher. A restart that never comes back
  is caught by the same start watchdog.
- **Manager** (`vm/scripts/games-reconcile.sh`, every minute): starts wanted games, saves, backs up and stops
  unwanted or idle games, publishes `status.json` for the portal, and deallocates the VM when no game is
  running. A maximum uptime and a nightly Azure auto-shutdown are backstops; an optional budget sends
  email alerts.
- **Start watchdog**: a game that has not answered within 15 minutes of being started is stopped, so the VM
  does not stay on for nothing, and the portal shows "The last start failed" (admins also see the server's
  last log lines) until the next start.
- **Updates**: the games update themselves on every start. About once a week, the start also refreshes the
  Docker image that runs each game, so a game patch that needs a newer image still starts.
- Game servers run in Docker using
  [thijsvanloef/palworld-server-docker](https://github.com/thijsvanloef/palworld-server-docker) and
  [mornedhels/enshrouded-server](https://github.com/mornedhels/enshrouded-server).

## Requirements

- An Azure subscription (pay-as-you-go or credits) where you can create resources and role assignments
  (Owner, or Contributor + User Access Administrator).
- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli), OpenSSH (built into Windows 10/11)
  and Windows PowerShell 5.1 or PowerShell 7.
- Quota for 4 vCPUs of the VM size in your region (the provisioning script tells you if it fails).

## Quick start

1. Copy `gameservers.config.example.json` to `gameservers.config.json` and fill it in:

   | Setting | Notes |
   |---|---|
   | `subscriptionId`, `location`, `resourceGroup` | Where everything is created |
   | `storageAccount` | Globally unique, 3-24 lowercase letters and numbers |
   | `functionApp` | Globally unique; the portal is `https://<functionApp>.azurewebsites.net` |
   | `timezone` | e.g. `UTC`, `America/New_York` |
   | `games` | `["palworld"]`, `["enshrouded"]` or both |
   | `vm.size` | `Standard_D4as_v7` (4 vCPU, 16 GB) works for small groups; resize later if needed |
   | `vm.sshAllowedIp` | Your public IP for SSH, or `auto` to detect it |
   | `power.*` | Idle minutes before a game stops, max uptime, nightly shutdown time (UTC, `HHMM`) |
   | `budget` | Monthly amount and alert email; leave the email empty to skip |
   | `palworld` | Max players and server description |

2. Sign in and provision:

   ```powershell
   az login
   powershell -ExecutionPolicy Bypass -File .\azure\provision.ps1
   ```

   This takes about 15 minutes. It prints the portal URL and the generated passwords and saves them to
   `gameservers.secrets.json` (git-ignored; keep it private). Re-running it is safe: existing resources and
   saved passwords are reused.

3. Open the portal, enter the admin password, and press **Start** on a game. The first start downloads the
   game server (a few minutes).

## Passwords

| Password | Used for |
|---|---|
| Portal admin | Everything in the portal |
| Portal player | Seeing server info, who is online and server usage, and pressing Start (share this with friends) |
| Game join | Joining Palworld; the Enshrouded "Friends" role |
| Game admin | Palworld `/AdminPassword` in chat; the Enshrouded "Admins" role |

Change portal passwords in the Function App settings (`PORTAL_PASSWORD`, `PLAYER_PASSWORD`). Enshrouded role
passwords are edited in the portal. Palworld's join/admin passwords live in
`/opt/palworld/docker-compose.yml` on the VM (`SERVER_PASSWORD` / `ADMIN_PASSWORD`; also update the
`SERVER_PASSWORD` Function App setting so the portal shows the right one).

## Portal

Each game has a tab with:

- **Server**: status, address, join password, current CPU and memory use, Start/Stop. While a game starts,
  the Start button fills as a progress bar with the elapsed time, and the line above it names the current
  step ("Step 3 of 5: Downloading game update 42%"). It reaches 100% only when the server answers.
  "Save and restart now" shows the same way ("Restarting… 0:40").
- **Players**: who is online (admins also see player IDs). Enshrouded's Steam query only gives the count,
  so its names come from the server log. Palworld admins get Kick/Ban/Unban; Enshrouded has
  no remote admin API, so moderation happens in-game by players whose role allows kick/ban.
- **Roles** (Enshrouded, admin): role names, join passwords, permissions and reserved slots.
- **Backups** (admin): back up now, restore (the current world is saved first as `pre-restore`), and
  download through a 10-minute read-only link.
- **Settings** (admin): server name and gameplay settings, applied on the next start or immediately with
  "Save and restart now".

While the VM is on, a **Server usage** panel above the tabs shows:

- CPU and memory over the current session as line charts (whole server and each game). Values are recorded
  every minute; a sampler also records the highest 5-second CPU and memory in each minute, so short spikes
  show up as a dotted line and in the "peak" figures instead of being averaged away.
- **Time spent waiting**: the share of the last minute that programs were stalled waiting for CPU or memory
  (Linux pressure stall information). This is the clearest sign of an overloaded server. Under 10% is normal;
  sustained values above about 25% mean players will notice lag and the VM should be resized.
- Swap and disk usage, plus warnings for admins (memory nearly full, using swap, CPU maxed, waiting time).

History is kept only for the current session (in memory on the VM, up to 8 hours) and is deleted when the VM
shuts down.

## Updating a deployment

```powershell
powershell -ExecutionPolicy Bypass -File .\azure\deploy-portal.ps1   # portal changes
powershell -ExecutionPolicy Bypass -File .\azure\deploy-vm.ps1       # VM script changes (start a game first so the VM is on)
```

## Performance

The VM runs **Ubuntu 26.04 LTS Minimal** (kernel 7.0) with a few tunings applied at first boot:

- **ntsync**: Enshrouded's server is a Windows program run through Proton/Wine. On kernel 6.14+ the kernel's
  ntsync driver handles Wine's thread synchronization, which on a 4-core VM cut Enshrouded's idle CPU from
  about 1.5 cores to about 0.3 and the time programs spend waiting for CPU from about 12% to under 3%.
  `game-apply-settings.sh` passes `/dev/ntsync` into the container only when the kernel provides it.
- **Accelerated networking** (Azure's MANA adapter): lower network latency and jitter.
- **Larger UDP buffers** (`/etc/sysctl.d/99-zz-gameservers.conf`) so traffic bursts are not dropped.
- **CPU priority**: game containers get a higher CPU weight than background work; backups run at low priority.

To move an existing VM to a larger size, stop the games, then in Azure resize it (for example to
`Standard_D8as_v7`); disks, IP and data are kept.

### Start-up time

Measured on a 4-core VM: with the VM already on, a game is ready about 20 seconds after pressing Start
(Palworld; Enshrouded about 35 seconds). From a stopped VM add Azure's VM start (typically 30-90 seconds)
and an 11-second Ubuntu boot. Docker images (refreshed weekly), game files and worlds all stay on the
VM's disk, so a start only downloads a game patch when one has been released.

## Security

- The portal and the VM use managed identities with least-privilege roles: each can control only the VM
  and only the storage containers it needs. No keys or connection strings are stored anywhere.
- Only the game ports and SSH (from your IP) are open on the VM. The game admin APIs listen on localhost.
- Backups are private; downloads use short-lived user-delegation links. Storage soft delete keeps deleted or
  overwritten backups, settings and roles recoverable for 7 days.
- All portal input is validated against fixed allow-lists before it reaches the VM.

## Using a backup locally

Download a backup from the portal and extract it (`tar -xzf <file>.tar.gz`).

- **Enshrouded**: the world is `savegame/world0.ksd`. Local saves live in
  `%USERPROFILE%\Saved Games\enshrouded` (or Steam `userdata\<id>\1203620\remote` with cloud saves). Create a
  local world, close the game, back up its files, then replace them with the server's, renamed to match.
- **Palworld**: the world folder is `Saved/SaveGames/0/<world-id>/`; copy it into
  `%LOCALAPPDATA%\Pal\Saved\SaveGames\<steam-id>\`. Moving a Linux dedicated-server save to a local game
  usually makes players create new characters; the community `palworld-host-save-fix` tool can migrate them
  but is experimental, so work on a copy.

## Costs (approximate, US regions)

- VM `Standard_D4as_v7`: about $0.18/hour, only while running.
- Always on: 64 GB Standard SSD about $5, static IP about $3.65, portal and storage about $1 per month.

## Repository layout

| Path | What it is |
|---|---|
| `portal/` | Azure Function: API and single-page UI |
| `vm/scripts/` | Manager, backup, restore, settings/roles and moderation scripts installed on the VM |
| `vm/systemd/` | Timers for the manager (every minute) and periodic backups (every 3 hours), and the 5-second usage sampler |
| `vm/palworld/`, `vm/enshrouded/` | Compose file templates (placeholders are filled in by provisioning) |
| `vm/cloud-init.yaml` | First-boot setup for the VM (Docker, ntsync, network tuning, swap) |
| `azure/` | Provisioning and deploy scripts, backup lifecycle policy |
| `tools/export-public.ps1` | Builds a scrubbed public copy of a private fork (see below) |

## Keeping a private fork

If you keep your real `gameservers.config.json` in a private repository, `tools/export-public.ps1` creates a
public copy without it: it deny-lists every value from your config, your saved passwords, your portal's live
passwords and any extra `scrubTerms`, refuses to finish if any of them appear in the copy, and starts a fresh
git history under a neutral author. To update an existing public repo, export to an empty folder again with
`-RemoteUrl <url> -ForcePush`.

## License

MIT
