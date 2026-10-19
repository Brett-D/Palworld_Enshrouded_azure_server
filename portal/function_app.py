import hmac
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import azure.functions as func

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

SUB = os.environ["VM_SUBSCRIPTION_ID"]
RG = os.environ["VM_RESOURCE_GROUP"]
VM = os.environ["VM_NAME"]
PASSWORD = os.environ["PORTAL_PASSWORD"]
# Optional second password that can only view server info and start games.
PLAYER_PASSWORD = os.environ.get("PLAYER_PASSWORD", "")
STORAGE_ACCOUNT = os.environ["STORAGE_ACCOUNT"]
BACKUP_CONTAINER = os.environ.get("BACKUP_CONTAINER", "backups")
CONFIG_CONTAINER = os.environ.get("CONFIG_CONTAINER", "config")
BACKUP_NAME_RE = re.compile(r"^(palworld|enshrouded)-\d{8}-\d{6}-[a-z-]+\.tar\.gz$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.!-]{0,39}$")
PLAYER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ROLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,23}$")
ROLE_PASSWORD_RE = re.compile(r"^[A-Za-z0-9._!-]{1,32}$")
ROLE_PERMISSIONS = ["canKickBan", "canAccessInventories", "canEditWorld", "canEditBase", "canExtendBase"]
MAX_ROLES = 8
DOWNLOAD_LINK_MINUTES = 10
# One usage sample a minute for up to 8 hours (the default max uptime), so peaks cover a whole session.
HISTORY_SAMPLES = 480
# The VM publishes its status every minute; older than this means its manager isn't reporting.
STATUS_MAX_AGE_SECONDS = 180

VM_URL = (
    f"https://management.azure.com/subscriptions/{SUB}/resourceGroups/{RG}"
    f"/providers/Microsoft.Compute/virtualMachines/{VM}"
)
API = "api-version=2024-03-01"

GAMES = {
    "palworld": {
        "label": "Palworld",
        "address": os.environ.get("SERVER_ADDRESS", ""),
        "password": os.environ.get("SERVER_PASSWORD", ""),
    },
    "enshrouded": {
        "label": "Enshrouded",
        "address": os.environ.get("ENSHROUDED_ADDRESS", ""),
        "password": os.environ.get("ENSHROUDED_PASSWORD", ""),
    },
}
# Comma-separated list of the games deployed on this server.
_ENABLED = [g.strip() for g in os.environ.get("ENABLED_GAMES", "palworld,enshrouded").split(",") if g.strip()]
GAMES = {g: info for g, info in GAMES.items() if g in _ENABLED}


# ---- Settings schema -------------------------------------------------------------------------
# Every key is an environment variable understood by that game's Docker image. Values are validated
# against this table and stored in an env-ready form; the VM only copies them into settings.env.

def _text(key, label, group, default):
    return {"key": key, "label": label, "group": group, "type": "text", "default": default}


def _enum(key, label, group, options, default):
    return {"key": key, "label": label, "group": group, "type": "enum", "options": options, "default": default}


def _int(key, label, group, lo, hi, default):
    return {"key": key, "label": label, "group": group, "type": "int", "min": lo, "max": hi, "default": default}


def _flt(key, label, group, lo, hi, default=1.0, fmt="palworld"):
    return {"key": key, "label": label, "group": group, "type": "float", "min": lo, "max": hi,
            "default": default, "fmt": fmt}


def _bool(key, label, group, default, lower=False):
    return {"key": key, "label": label, "group": group, "type": "bool", "default": default,
            "true": "true" if lower else "True", "false": "false" if lower else "False"}


def _minutes(key, label, group, lo, hi, default):
    return {"key": key, "label": label, "group": group, "type": "minutes", "min": lo, "max": hi, "default": default}


PALWORLD_SETTINGS = [
    _text("SERVER_NAME", "Server name", "Server", "Palworld Server"),
    _enum("DEATH_PENALTY", "Death penalty", "Game mode", ["None", "Item", "ItemAndEquipment", "All"], "Item"),
    _bool("IS_PVP", "PvP mode", "Game mode", False),
    _bool("ENABLE_PLAYER_TO_PLAYER_DAMAGE", "Player vs player damage", "Game mode", False),
    _bool("ENABLE_FRIENDLY_FIRE", "Friendly fire", "Game mode", False),
    _bool("ENABLE_INVADER_ENEMY", "Base raids", "Game mode", True),
    _bool("ENABLE_FAST_TRAVEL", "Fast travel", "Game mode", True),
    _flt("EXP_RATE", "XP rate", "Progression and loot", 0.1, 20),
    _flt("PAL_CAPTURE_RATE", "Pal capture rate", "Progression and loot", 0.5, 2),
    _flt("PAL_SPAWN_NUM_RATE", "Pal spawn rate", "Progression and loot", 0.5, 3),
    _flt("COLLECTION_DROP_RATE", "Gathering drop rate", "Progression and loot", 0.1, 10),
    _flt("ENEMY_DROP_ITEM_RATE", "Enemy drop rate", "Progression and loot", 0.1, 10),
    _flt("WORK_SPEED_RATE", "Pal work speed", "Progression and loot", 0.1, 10),
    _flt("ITEM_WEIGHT_RATE", "Item weight", "Progression and loot", 0.1, 10),
    _flt("PAL_EGG_DEFAULT_HATCHING_TIME", "Egg hatch time (hours)", "Progression and loot", 0, 240),
    _flt("PLAYER_DAMAGE_RATE_ATTACK", "Player damage dealt", "Combat", 0.1, 5),
    _flt("PLAYER_DAMAGE_RATE_DEFENSE", "Player damage taken", "Combat", 0.1, 5),
    _flt("PAL_DAMAGE_RATE_ATTACK", "Pal damage dealt", "Combat", 0.1, 5),
    _flt("PAL_DAMAGE_RATE_DEFENSE", "Pal damage taken", "Combat", 0.1, 5),
    _flt("DAYTIME_SPEEDRATE", "Day speed", "Survival", 0.1, 5),
    _flt("NIGHTTIME_SPEEDRATE", "Night speed", "Survival", 0.1, 5),
    _flt("PLAYER_STOMACH_DECREASE_RATE", "Player hunger rate", "Survival", 0.1, 5),
    _flt("PAL_STOMACH_DECREASE_RATE", "Pal hunger rate", "Survival", 0.1, 5),
    _int("BASE_CAMP_MAX_NUM", "Max bases (server)", "Bases", 1, 128, 128),
    _int("BASE_CAMP_WORKER_MAX_NUM", "Pals working per base", "Bases", 1, 50, 15),
]

CUSTOM = "Custom difficulty"
ENSHROUDED_SETTINGS = [
    _text("SERVER_NAME", "Server name", "Server", "Enshrouded Server"),
    _int("SERVER_SLOT_COUNT", "Max players", "Server", 1, 16, 8),
    _bool("SERVER_ENABLE_TEXT_CHAT", "Text chat", "Server", True, lower=True),
    _bool("SERVER_ENABLE_VOICE_CHAT", "Voice chat", "Server", False, lower=True),
    _enum("SERVER_GS_PRESET", "Difficulty preset", "Difficulty",
          ["Default", "Relaxed", "Hard", "Survival", "Custom"], "Default"),
    _flt("SERVER_GS_PLAYER_HEALTH_FACTOR", "Player health", CUSTOM, 0.25, 4, fmt="plain"),
    _flt("SERVER_GS_PLAYER_STAMINA_FACTOR", "Player stamina", CUSTOM, 0.25, 4, fmt="plain"),
    _bool("SERVER_GS_ENABLE_DURABILITY", "Weapon durability", CUSTOM, True, lower=True),
    _bool("SERVER_GS_ENABLE_STARVING_DEBUFF", "Starvation", CUSTOM, False, lower=True),
    _flt("SERVER_GS_SHROUD_TIME_FACTOR", "Time allowed in the Shroud", CUSTOM, 0.5, 2, fmt="plain"),
    _enum("SERVER_GS_TOMBSTONE_MODE", "On death, lose", CUSTOM,
          ["AddBackpackMaterials", "Everything", "NoTombstone"], "AddBackpackMaterials"),
    _enum("SERVER_GS_CURSE_MODIFIER", "Curse chance", CUSTOM, ["Easy", "Normal", "Hard"], "Normal"),
    _enum("SERVER_GS_WEATHER_FREQUENCY", "Weather", CUSTOM, ["Disabled", "Rare", "Normal", "Often"], "Normal"),
    _flt("SERVER_GS_EXPERIENCE_COMBAT_FACTOR", "Combat XP", CUSTOM, 0.25, 2, fmt="plain"),
    _flt("SERVER_GS_EXPERIENCE_MINING_FACTOR", "Mining XP", CUSTOM, 0, 2, fmt="plain"),
    _flt("SERVER_GS_EXPERIENCE_EXPLORATION_QUESTS_FACTOR", "Exploration and quest XP", CUSTOM, 0.25, 2, fmt="plain"),
    _flt("SERVER_GS_MINING_DAMAGE_FACTOR", "Mining yield", CUSTOM, 0.5, 2, fmt="plain"),
    _flt("SERVER_GS_RESOURCE_DROP_STACK_AMOUNT_FACTOR", "Loot stack size", CUSTOM, 0.25, 2, fmt="plain"),
    _flt("SERVER_GS_PLANT_GROWTH_SPEED_FACTOR", "Plant growth speed", CUSTOM, 0.25, 2, fmt="plain"),
    _flt("SERVER_GS_FACTORY_PRODUCTION_SPEED_FACTOR", "Crafting station time", CUSTOM, 0.25, 2, fmt="plain"),
    _enum("SERVER_GS_RANDOM_SPAWNER_AMOUNT", "Enemy amount", CUSTOM,
          ["Few", "Normal", "Automatic", "Many", "Extreme"], "Normal"),
    _flt("SERVER_GS_ENEMY_DAMAGE_FACTOR", "Enemy damage", CUSTOM, 0.25, 5, fmt="plain"),
    _flt("SERVER_GS_ENEMY_HEALTH_FACTOR", "Enemy health", CUSTOM, 0.25, 4, fmt="plain"),
    _flt("SERVER_GS_BOSS_DAMAGE_FACTOR", "Boss damage", CUSTOM, 0.2, 5, fmt="plain"),
    _flt("SERVER_GS_BOSS_HEALTH_FACTOR", "Boss health", CUSTOM, 0.2, 5, fmt="plain"),
    _bool("SERVER_GS_PACIFY_ALL_ENEMIES", "Enemies only fight back", CUSTOM, False, lower=True),
    _minutes("SERVER_GS_DAY_TIME_DURATION", "Day length (minutes)", CUSTOM, 2, 60, 30),
    _minutes("SERVER_GS_NIGHT_TIME_DURATION", "Night length (minutes)", CUSTOM, 2, 60, 12),
]

# Enshrouded only uses the individual difficulty values when the preset is Custom, so the page shows them
# disabled otherwise. They are still saved, so switching presets never loses them.
for _spec in ENSHROUDED_SETTINGS:
    if _spec["group"] == CUSTOM:
        _spec["enabledWhen"] = {"key": "SERVER_GS_PRESET", "value": "Custom",
                                "hint": "Set Difficulty preset to Custom to change these."}

SETTINGS = {"palworld": PALWORLD_SETTINGS, "enshrouded": ENSHROUDED_SETTINGS}

PRESETS = {
    "palworld": {
        "Normal": {},
        "Casual": {
            "EXP_RATE": 2, "PAL_CAPTURE_RATE": 1.5, "DEATH_PENALTY": "None",
            "PLAYER_STOMACH_DECREASE_RATE": 0.5, "PAL_STOMACH_DECREASE_RATE": 0.5,
            "ENEMY_DROP_ITEM_RATE": 1.5, "COLLECTION_DROP_RATE": 1.5,
            "ITEM_WEIGHT_RATE": 0.5, "ENABLE_INVADER_ENEMY": False,
        },
        "Hard": {
            "DEATH_PENALTY": "ItemAndEquipment", "PLAYER_DAMAGE_RATE_DEFENSE": 1.5,
            "PAL_DAMAGE_RATE_DEFENSE": 1.5, "PAL_CAPTURE_RATE": 0.75,
            "PLAYER_STOMACH_DECREASE_RATE": 1.5, "PAL_STOMACH_DECREASE_RATE": 1.5,
        },
        "PvP": {"IS_PVP": True, "ENABLE_PLAYER_TO_PLAYER_DAMAGE": True},
    },
    # Enshrouded has its own built-in difficulty presets, chosen with "Difficulty preset".
    "enshrouded": {},
}


def _fmt_float(spec, number):
    if spec["fmt"] == "palworld":
        return f"{number:.6f}"
    return f"{number:.4f}".rstrip("0").rstrip(".") or "0"


def _to_env(spec, raw):
    """Validates one UI value and returns its env-ready string. Raises ValueError."""
    kind = spec["type"]
    if kind == "bool":
        text = str(raw).lower()
        if text not in ("true", "false"):
            raise ValueError
        return spec["true"] if text == "true" else spec["false"]
    if kind == "enum":
        if raw not in spec["options"]:
            raise ValueError
        return raw
    if kind == "text":
        if not isinstance(raw, str) or not SERVER_NAME_RE.fullmatch(raw.strip()):
            raise ValueError
        return raw.strip()
    if kind == "int":
        number = int(str(raw))
        if not spec["min"] <= number <= spec["max"]:
            raise ValueError
        return str(number)
    number = float(raw)
    if not (math.isfinite(number) and spec["min"] <= number <= spec["max"]):
        raise ValueError
    if kind == "minutes":
        return str(int(round(number * 60_000_000_000)))
    return _fmt_float(spec, number)


def _from_env(spec, value):
    if spec["type"] == "minutes":
        return round(int(value) / 60_000_000_000, 2)
    return value


def _validate_settings(game, values):
    specs = SETTINGS[game]
    known = {s["key"] for s in specs}
    for key in values:
        if key not in known:
            raise ValueError(f"Unknown setting: {key}")
    clean = {}
    for spec in specs:
        raw = values.get(spec["key"], spec["default"])
        try:
            clean[spec["key"]] = _to_env(spec, raw)
        except (ValueError, TypeError):
            raise ValueError(f"Invalid value for {spec['label']}")
    return clean


# ---- Azure helpers -----------------------------------------------------------------------------

_failures = {}


_tokens = {}


def _token(resource="https://management.azure.com/"):
    # Managed identity tokens last hours; reuse them instead of fetching one per Azure call.
    cached = _tokens.get(resource)
    if cached and cached[1] - time.time() > 300:
        return cached[0]
    req = urllib.request.Request(
        f"{os.environ['IDENTITY_ENDPOINT']}?api-version=2019-08-01&resource={resource}",
        headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    _tokens[resource] = (data["access_token"], float(data.get("expires_on", time.time() + 600)))
    return data["access_token"]


def _arm(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _token())
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ARM {e.code}: {e.read().decode()[:300]}")


class BusyError(Exception):
    """The VM is already running another portal command."""


def _run_on_vm(command):
    # Only ever called with fixed commands, or arguments validated against GAMES or BACKUP_NAME_RE.
    try:
        _arm("POST", f"{VM_URL}/runCommand?{API}", {"commandId": "RunShellScript", "script": [command]})
    except RuntimeError as e:
        if "ARM 409" in str(e):
            raise BusyError() from e
        raise


def _storage(method, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _token("https://storage.azure.com/"))
    req.add_header("x-ms-version", "2023-11-03")
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _blob(container, name):
    return f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{container}/{name}"


def _put_blob(container, name, data, content_type):
    code, _ = _storage("PUT", _blob(container, name), data,
                       {"x-ms-blob-type": "BlockBlob", "Content-Type": content_type})
    return code == 201


def _list_backups():
    code, body = _storage("GET", f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/{BACKUP_CONTAINER}"
                                 "?restype=container&comp=list")
    if code != 200:
        raise RuntimeError(f"Storage {code} listing backups")
    out = []
    for b in ET.fromstring(body).iter("Blob"):
        name = b.findtext("Name") or ""
        if not BACKUP_NAME_RE.fullmatch(name):
            continue
        props = b.find("Properties")
        out.append({
            "name": name,
            "size": int(props.findtext("Content-Length")),
            "modified": parsedate_to_datetime(props.findtext("Last-Modified")).isoformat(),
        })
    out.sort(key=lambda b: b["modified"], reverse=True)
    return out


def _load_settings(game):
    """Returns env-ready values: stored ones where valid, defaults otherwise."""
    stored = {}
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, f"settings-{game}.json"))
    if code == 200:
        stored = json.loads(body)
    elif code != 404:
        raise RuntimeError(f"Storage {code} reading settings")
    values = {}
    for spec in SETTINGS[game]:
        value = stored.get(spec["key"])
        values[spec["key"]] = str(value) if value is not None else _to_env(spec, spec["default"])
    return values


def _state():
    view = _arm("GET", f"{VM_URL}/instanceView?{API}")
    codes = [s["code"] for s in view.get("statuses", []) if s["code"].startswith("PowerState/")]
    code = codes[0].split("/")[1] if codes else "unknown"
    return {"running": "running", "starting": "starting", "deallocated": "stopped",
            "deallocating": "stopping", "stopped": "stopped", "stopping": "stopping"}.get(code, "unknown")


def _requested_at(game):
    """When the portal last asked for this game to run (the desired blob's time), or None."""
    req = urllib.request.Request(_blob(CONFIG_CONTAINER, f"desired-{game}"), method="HEAD")
    req.add_header("Authorization", "Bearer " + _token("https://storage.azure.com/"))
    req.add_header("x-ms-version", "2023-11-03")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return parsedate_to_datetime(r.headers["Last-Modified"]).timestamp()
    except (urllib.error.HTTPError, KeyError, TypeError, ValueError):
        return None


def _wanted(game):
    return _requested_at(game) is not None


def _start_progress(game, requested_at):
    """The VM's startup stage for a game being started, if it is current."""
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, f"progress-{game}.json"))
    if code != 200:
        return None
    try:
        p = json.loads(body)
    except ValueError:
        return None
    if not isinstance(p, dict):
        return None
    # Before the container goes down a restart publishes steps without heartbeats (a backup or an
    # Enshrouded shutdown can take a minute or two), so those steps are allowed to be older.
    restart = p.get("kind") == "restart"
    max_age = 180 if restart and p.get("stage") in ("warn", "backup", "stop") else 60
    if time.time() - p.get("updated", 0) > max_age:
        return None
    # Ignore a leftover from an earlier start (the VM's clock and Azure's can differ slightly).
    if requested_at and p.get("started", 0) < requested_at - 120:
        return None
    out = {k: p.get(k) for k in ("stage", "pct", "label", "started", "stageSince")}
    out["restart"] = restart
    return out


def _vm_report():
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, "status.json"))
    if code != 200:
        return None
    try:
        report = json.loads(body)
    except ValueError:
        return None
    if time.time() - report.get("updated", 0) > STATUS_MAX_AGE_SECONDS:
        return None
    return report


def _game_states(vm, admin=False, with_report=False):
    report = _vm_report() if vm == "running" else None
    games = {}
    for g, info in GAMES.items():
        requested = _requested_at(g) if vm in ("running", "starting") else None
        wanted = requested is not None
        players = None
        r = {}
        if vm == "running":
            r = (report or {}).get("games", {}).get(g, {})
            running, ready = r.get("running", False), r.get("ready", False)
            players = r.get("players")
            if wanted:
                state = "running" if running and ready else "starting"
            else:
                state = "stopping" if running else "stopped"
        elif vm == "starting":
            state = "starting" if wanted else "stopped"
        else:
            state = "stopped"
        out = {"state": state}
        if state == "starting" and requested:
            out["requestedAt"] = int(requested)
        if state in ("running", "starting"):
            out["name"] = _load_settings(g)["SERVER_NAME"]
            if info["address"]:
                out["address"] = info["address"]
            password = _public_password(g)
            if password:
                out["password"] = password
            out["players"] = players
        if state == "running":
            # Everyone sees who is online and how busy the game is; IDs and bans are admin-only.
            plist = r.get("list", [])
            out["playerList"] = plist if admin else [{k: p[k] for k in ("name", "level") if k in p} for p in plist]
            out["usage"] = r.get("metrics")
            if admin and g == "palworld":
                out["bans"] = r.get("bans", [])
        games[g] = out
    return (games, report) if with_report else games


def _start_failures():
    """The VM's record of games that did not start: {game: {time, reason, log}}."""
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, "failures.json"))
    if code != 200:
        return {}
    try:
        data = json.loads(body)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _status_payload(vm=None, admin=False):
    vm = vm or _state()
    games, report = _game_states(vm, admin, with_report=True)
    for g, failure in _start_failures().items():
        if g in games and isinstance(failure, dict):
            # Log lines can include player names and IDs, so only admins see them.
            entry = {"time": failure.get("time"), "reason": failure.get("reason", "")}
            if admin:
                entry["log"] = [str(line) for line in failure.get("log", [])][:20]
            games[g]["lastFailure"] = entry
    if vm == "running":
        for g, s in games.items():
            if s["state"] == "starting":
                progress = _start_progress(g, s.get("requestedAt"))
                if progress:
                    s["progress"] = progress
                    if progress["restart"]:
                        s["restarting"] = True
                        s["requestedAt"] = progress["started"]
            elif s["state"] == "running":
                # "Save and restart now" in progress: show it on the Start button like a start.
                progress = _start_progress(g, None)
                if progress and progress["restart"] and progress["stage"] != "ready":
                    s.update(state="starting", restarting=True, requestedAt=progress["started"], progress=progress)
    payload = {"vm": vm, "games": games}
    if report:
        payload["resources"] = report.get("vm")
        payload["vmUptime"] = int(report.get("uptime", 0) + max(0, time.time() - report.get("updated", time.time())))
    return payload


# ---- Enshrouded roles ----------------------------------------------------------------------------

def _load_roles():
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, "roles-enshrouded.json"))
    if code == 404:
        return {"public": "", "roles": []}
    if code != 200:
        raise RuntimeError(f"Storage {code} reading roles")
    return json.loads(body)


def _public_password(game):
    if game != "enshrouded":
        return GAMES[game]["password"]
    try:
        data = _load_roles()
    except (RuntimeError, ValueError):
        data = {}
    if not data.get("roles"):
        return GAMES[game]["password"]
    for role in data["roles"]:
        if role.get("name") == data.get("public"):
            return role.get("password", "")
    # Roles exist but none is marked as shown on the portal: show no password.
    return ""


def _validate_roles(body):
    roles = body.get("roles")
    public = body.get("public", "")
    if not isinstance(roles, list) or not 1 <= len(roles) <= MAX_ROLES:
        raise ValueError(f"Add between 1 and {MAX_ROLES} roles")
    clean, names, passwords = [], set(), set()
    for i, role in enumerate(roles, 1):
        if not isinstance(role, dict):
            raise ValueError(f"Role {i} is invalid")
        name = role.get("name")
        if not isinstance(name, str) or not ROLE_NAME_RE.fullmatch(name.strip()):
            raise ValueError(f"Role {i}: name must be 1-24 letters, numbers, spaces, _ or -")
        name = name.strip()
        if name.lower() in names:
            raise ValueError(f"Role names must be unique ({name})")
        password = role.get("password")
        if not isinstance(password, str) or not ROLE_PASSWORD_RE.fullmatch(password):
            raise ValueError(f"{name}: password must be 1-32 letters, numbers, or . _ ! -")
        # The game assigns a role by the password a player types, so passwords must differ.
        if password in passwords:
            raise ValueError(f"{name}: each role needs a different password")
        slots = role.get("reservedSlots", 0)
        try:
            slots = int(str(slots))
        except ValueError:
            raise ValueError(f"{name}: reserved slots must be a whole number")
        if not 0 <= slots <= 16:
            raise ValueError(f"{name}: reserved slots must be 0-16")
        entry = {"name": name, "password": password, "reservedSlots": slots}
        for perm in ROLE_PERMISSIONS:
            value = role.get(perm, False)
            if not isinstance(value, bool):
                raise ValueError(f"{name}: invalid permission value")
            entry[perm] = value
        names.add(name.lower())
        passwords.add(password)
        clean.append(entry)
    if public and public not in {r["name"] for r in clean}:
        raise ValueError("The role shown to players must be one of the roles")
    return {"public": public, "roles": clean}


# ---- Save downloads ------------------------------------------------------------------------------

def _download_url(name):
    # Imported here so the rest of the portal doesn't pay the import cost on every cold start.
    from datetime import datetime, timedelta, timezone

    from azure.identity import ManagedIdentityCredential
    from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

    account_url = f"https://{STORAGE_ACCOUNT}.blob.core.windows.net"
    service = BlobServiceClient(account_url, credential=ManagedIdentityCredential())
    start = datetime.now(timezone.utc) - timedelta(minutes=5)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=DOWNLOAD_LINK_MINUTES)
    key = service.get_user_delegation_key(start, expiry)
    sas = generate_blob_sas(
        account_name=STORAGE_ACCOUNT, container_name=BACKUP_CONTAINER, blob_name=name,
        user_delegation_key=key, permission=BlobSasPermissions(read=True),
        start=start, expiry=expiry, protocol="https",
        content_disposition=f'attachment; filename="{name}"',
    )
    return f"{account_url}/{BACKUP_CONTAINER}/{name}?{sas}"


# ---- Request helpers ---------------------------------------------------------------------------

def _json(obj, status=200):
    return func.HttpResponse(json.dumps(obj), status_code=status, mimetype="application/json")


def _role_for(supplied):
    supplied = supplied.encode()
    if hmac.compare_digest(supplied, PASSWORD.encode()):
        return "admin"
    if PLAYER_PASSWORD and hmac.compare_digest(supplied, PLAYER_PASSWORD.encode()):
        return "player"
    return None


def _authorized(req, admin=True):
    ip = req.headers.get("x-forwarded-for", "unknown").split(",")[0].strip()
    now = time.time()
    recent = [t for t in _failures.get(ip, []) if now - t < 300]
    if len(recent) >= 5:
        return False, _json({"error": "rate limited"}, 429)
    role = _role_for(req.headers.get("x-portal-key", ""))
    if role is None:
        recent.append(now)
        _failures[ip] = recent
        time.sleep(2)
        return False, _json({"error": "unauthorized"}, 401)
    if admin and role != "admin":
        return False, _json({"error": "This needs the admin password"}, 403)
    return True, None


def _body(req):
    try:
        body = req.get_json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _valid_game(game):
    return game if isinstance(game, str) and game in GAMES else None


BUSY = {"error": "The server is busy with another task, try again in a minute"}


# ---- Routes ------------------------------------------------------------------------------------

@app.route(route="{ignored:maxlength(0)?}", methods=["GET"])
def home(req: func.HttpRequest) -> func.HttpResponse:
    return func.HttpResponse(PAGE, mimetype="text/html")


@app.route(route="api/status", methods=["GET"])
def status(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req, admin=False)
    if not ok:
        return resp
    role = _role_for(req.headers.get("x-portal-key", ""))
    payload = _status_payload(admin=role == "admin")
    payload["role"] = role
    return _json(payload)


@app.route(route="api/history", methods=["GET"])
def history(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req, admin=False)
    if not ok:
        return resp
    code, body = _storage("GET", _blob(CONFIG_CONTAINER, "history.json"))
    samples = []
    if code == 200:
        try:
            samples = json.loads(body)
        except ValueError:
            samples = []
    if not isinstance(samples, list):
        samples = []
    return _json({"samples": samples[-HISTORY_SAMPLES:]})


@app.route(route="api/start", methods=["POST"])
def start(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req, admin=False)
    if not ok:
        return resp
    game = _valid_game(_body(req).get("game"))
    if not game:
        return _json({"error": "Unknown game"}, 400)
    vm = _state()
    if vm == "stopping":
        return _json({"error": "The VM is shutting down, try again in a minute"}, 409)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()).encode()
    if not _put_blob(CONFIG_CONTAINER, f"desired-{game}", stamp, "text/plain"):
        return _json({"error": "Could not record the start request"}, 500)
    if vm == "stopped":
        _arm("POST", f"{VM_URL}/start?{API}")
        vm = "starting"
    return _json(_status_payload(vm))


@app.route(route="api/stop", methods=["POST"])
def stop(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    game = _valid_game(_body(req).get("game"))
    if not game:
        return _json({"error": "Unknown game"}, 400)
    # The VM's manager notices within a minute, saves, backs up and stops the game,
    # then deallocates the VM if no other game is running.
    code, _ = _storage("DELETE", _blob(CONFIG_CONTAINER, f"desired-{game}"))
    if code not in (202, 404):
        return _json({"error": "Could not record the stop request"}, 500)
    return _json(_status_payload())


@app.route(route="api/backups", methods=["GET"])
def backups(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    return _json({"backups": _list_backups()}) if ok else resp


@app.route(route="api/backup", methods=["POST"])
def backup_now(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    game = _valid_game(_body(req).get("game"))
    if not game:
        return _json({"error": "Unknown game"}, 400)
    if _game_states(_state())[game]["state"] != "running":
        return _json({"error": "The game must be running to back up"}, 409)
    try:
        _run_on_vm(f"/usr/local/bin/game-backup.sh {game} manual")
    except BusyError:
        return _json(BUSY, 409)
    return _json({"started": True})


@app.route(route="api/restore", methods=["POST"])
def restore(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    name = _body(req).get("name", "")
    if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
        return _json({"error": "Invalid backup name"}, 400)
    if name not in {b["name"] for b in _list_backups()}:
        return _json({"error": "Backup not found"}, 404)
    if _state() != "running":
        return _json({"error": "Start a game first so the VM is on, then restore"}, 409)
    try:
        _run_on_vm(f"/usr/local/bin/game-restore.sh {name}")
    except BusyError:
        return _json(BUSY, 409)
    return _json({"started": True})


@app.route(route="api/settings", methods=["GET"])
def settings_get(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    game = _valid_game(req.params.get("game"))
    if not game:
        return _json({"error": "Unknown game"}, 400)
    env_values = _load_settings(game)
    values = {s["key"]: _from_env(s, env_values[s["key"]]) for s in SETTINGS[game]}
    return _json({"schema": SETTINGS[game], "values": values, "presets": PRESETS[game]})


@app.route(route="api/settings", methods=["POST"])
def settings_save(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    body = _body(req)
    game = _valid_game(body.get("game"))
    if not game:
        return _json({"error": "Unknown game"}, 400)
    values = body.get("values")
    if not isinstance(values, dict):
        return _json({"error": "Invalid request"}, 400)
    try:
        clean = _validate_settings(game, values)
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    if not _put_blob(CONFIG_CONTAINER, f"settings-{game}.json", json.dumps(clean).encode(), "application/json"):
        return _json({"error": "Could not save settings"}, 500)
    vm = _state()
    applied = bool(body.get("applyNow")) and vm == "running" and _game_states(vm)[game]["state"] == "running"
    if applied:
        try:
            _run_on_vm(f"/usr/local/bin/game-apply-settings.sh {game} now")
        except BusyError:
            return _json({"error": "Saved, but the server is busy. Settings apply on the next start."}, 409)
    return _json({"saved": True, "applied": applied})


@app.route(route="api/download", methods=["POST"])
def download(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    name = _body(req).get("name", "")
    if not isinstance(name, str) or not BACKUP_NAME_RE.fullmatch(name):
        return _json({"error": "Invalid backup name"}, 400)
    if name not in {b["name"] for b in _list_backups()}:
        return _json({"error": "Backup not found"}, 404)
    return _json({"url": _download_url(name), "expiresInMinutes": DOWNLOAD_LINK_MINUTES})


@app.route(route="api/player", methods=["POST"])
def player_action(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    body = _body(req)
    if body.get("game") != "palworld" or "palworld" not in GAMES:
        return _json({"error": "Only Palworld supports kick and ban from the portal"}, 400)
    action, player_id = body.get("action"), body.get("id")
    if action not in ("kick", "ban", "unban"):
        return _json({"error": "Unknown action"}, 400)
    if not isinstance(player_id, str) or not PLAYER_ID_RE.fullmatch(player_id):
        return _json({"error": "Invalid player id"}, 400)
    if _game_states(_state())["palworld"]["state"] != "running":
        return _json({"error": "Palworld must be running"}, 409)
    try:
        _run_on_vm(f"/usr/local/bin/game-admin.sh palworld {action} {player_id}")
    except BusyError:
        return _json(BUSY, 409)
    return _json({"started": True})


@app.route(route="api/roles", methods=["GET"])
def roles_get(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    if "enshrouded" not in GAMES:
        return _json({"error": "Enshrouded is not enabled"}, 404)
    data = _load_roles()
    return _json({"public": data.get("public", ""), "roles": data.get("roles", []),
                  "permissions": ROLE_PERMISSIONS, "maxRoles": MAX_ROLES})


@app.route(route="api/roles", methods=["POST"])
def roles_save(req: func.HttpRequest) -> func.HttpResponse:
    ok, resp = _authorized(req)
    if not ok:
        return resp
    body = _body(req)
    if "enshrouded" not in GAMES:
        return _json({"error": "Enshrouded is not enabled"}, 404)
    try:
        clean = _validate_roles(body)
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    if not _put_blob(CONFIG_CONTAINER, "roles-enshrouded.json", json.dumps(clean).encode(), "application/json"):
        return _json({"error": "Could not save roles"}, 500)
    vm = _state()
    applied = bool(body.get("applyNow")) and vm == "running" and _game_states(vm)["enshrouded"]["state"] == "running"
    if applied:
        try:
            _run_on_vm("/usr/local/bin/game-apply-settings.sh enshrouded now")
        except BusyError:
            return _json({"error": "Saved, but the server is busy. Roles apply on the next start."}, 409)
    return _json({"saved": True, "applied": applied})


# ---- Page --------------------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Game Servers</title>
<style>
body{font-family:system-ui,sans-serif;background:#10161c;color:#e8eef3;margin:0;display:flex;justify-content:center}
main{background:#1a232c;padding:1.5rem;border-radius:12px;width:min(94vw,440px);margin:2rem 0}
h1{margin:0 0 .8rem;font-size:1.4rem}
h2{font-size:1.05rem;margin:0 0 .5rem}
h3{font-size:.8rem;text-transform:uppercase;letter-spacing:.05em;color:#8fa3b3;margin:1rem 0 .3rem}
input,button,select{box-sizing:border-box;border-radius:8px;border:0;font-size:1rem}
#pw,.wide{width:100%;padding:.7rem;margin:.3rem 0}
button{cursor:pointer;font-weight:600;padding:.7rem;color:#fff}
button:disabled{opacity:.45;cursor:not-allowed}
#vm{color:#8fa3b3;margin:.5rem 0 1rem;font-size:.95rem}
.tabs{display:flex;gap:.4rem;margin-bottom:-1px}
.tab{flex:1;background:#141c23;color:#8fa3b3;border-radius:10px 10px 0 0;padding:.7rem}
.tab.active{background:#222e39;color:#fff}
.tab .dot{display:inline-block;width:.6rem;height:.6rem;border-radius:50%;margin-right:.4rem;background:#55606a}
.dot.running{background:#2e9e5b}.dot.starting,.dot.stopping{background:#d08a1e}
.panel{background:#222e39;border-radius:0 0 10px 10px;padding:1rem}
.section{background:#1a232c;border-radius:8px;padding:.9rem;margin-bottom:.8rem}
.state{font-weight:600;margin-bottom:.4rem}
.info{font-size:.9rem;line-height:1.6;word-break:break-all;margin-bottom:.5rem}
.btns{display:flex;gap:.5rem}.btns button{flex:1}
.start{background:#2e9e5b}.stop{background:#c0463d}.blue{background:#3b7dd8}.orange{background:#d08a1e}
.start{position:relative;overflow:hidden}
.start .pfill{position:absolute;left:0;top:0;bottom:0;width:0;background:#3fbf73;transition:width .9s ease-out}
.start .pfill::after{content:"";position:absolute;top:0;left:0;right:0;bottom:0;transform:translateX(-100%);
  background:linear-gradient(90deg,transparent,rgba(255,255,255,.22),transparent)}
.start .plabel{position:relative;white-space:nowrap;font-variant-numeric:tabular-nums}
.start.busy{background:#1d4430}
.start.busy:disabled{opacity:1;cursor:progress}
.start.busy .pfill::after{animation:shimmer 1.6s linear infinite}
.start.done .pfill{background:#3fbf73}
@keyframes shimmer{to{transform:translateX(100%)}}
@media (prefers-reduced-motion:reduce){.start .pfill{transition:none}.start.busy .pfill::after{animation:none}}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.msg{margin-top:.5rem;font-size:.88rem;line-height:1.4;color:#c9d6e0}
.presets{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:.4rem}
.presets button{flex:1;padding:.45rem;background:#2c3a46;font-size:.85rem}
.row{display:flex;justify-content:space-between;align-items:center;gap:.6rem;margin:.25rem 0}
.row label{flex:1;font-size:.9rem}
.row input,.row select{width:150px;padding:.4rem}
.row input[type=checkbox]{width:auto;transform:scale(1.3)}
.row.off label{color:#6b7a87}
.row.off input,.row.off select{opacity:.45;cursor:not-allowed}
.dephint{margin-top:-.1rem}
.note{font-size:.8rem;color:#8fa3b3;margin:.2rem 0 .4rem}
.plist{font-size:.9rem;margin:.2rem 0 .5rem}
.prow{display:flex;align-items:center;gap:.4rem;padding:.35rem 0;border-bottom:1px solid #2c3a46}
.prow span{flex:1;word-break:break-all}
.prow button{padding:.35rem .6rem;font-size:.8rem}
.role{background:#222e39;border-radius:8px;padding:.6rem;margin:.5rem 0}
.role .row input[type=text]{width:170px}
.ghost{background:#2c3a46}
.pubsel{width:150px;padding:.4rem}
#usage{background:#141c23;border-radius:10px;padding:.8rem 1rem;margin-bottom:.8rem;font-size:.88rem}
#usage h2{font-size:.95rem;margin:0 0 .4rem}
.meter{display:flex;align-items:center;gap:.5rem;margin:.3rem 0}
.meter span:first-child{width:3.6rem;color:#8fa3b3}
.meter span:last-child{flex:1.4;text-align:right;white-space:nowrap}
.bar{flex:1;height:.5rem;background:#2c3a46;border-radius:4px;overflow:hidden}
.bar div{height:100%;background:#2e9e5b}
.bar div.warn{background:#d08a1e}.bar div.bad{background:#c0463d}
.warnings{color:#e0a64a;margin-top:.4rem;line-height:1.4}
.chartbox{margin:.3rem 0 .6rem}
.ctitle{color:#8fa3b3;font-size:.8rem;margin-bottom:.1rem}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:#2c3a46;stroke-width:1}
.chart .axis{fill:#8fa3b3;font-size:10px}
.chart .peak{stroke:#e8eef3;stroke-width:1;stroke-dasharray:3 3;opacity:.45}
.chart .spike{stroke-width:1;stroke-dasharray:1 2;opacity:.55}
.pressure{font-size:.82rem;color:#c9d6e0;margin:.2rem 0 .5rem}
.failbox{background:#3a1f22;border-radius:8px;padding:.6rem;margin:.4rem 0 .6rem;font-size:.85rem}
.failtitle{color:#f0a0a0;font-weight:600;margin-bottom:.2rem}
.faillog{max-height:12rem;overflow:auto;white-space:pre-wrap;word-break:break-all;font-size:.72rem;background:#1a1012;padding:.4rem;border-radius:6px;margin:.3rem 0 0}
.legend{display:flex;gap:.9rem;flex-wrap:wrap;font-size:.8rem;color:#c9d6e0}
.legend .sw{display:inline-block;width:.8rem;height:.25rem;border-radius:2px;margin-right:.3rem;vertical-align:middle}
</style></head><body><main>
<h1>Game Servers</h1>
<input id="pw" type="password" placeholder="Portal password" autocomplete="current-password">
<div id="vm">Enter the password to see the servers</div>
<div id="app" hidden>
  <div id="usage" hidden></div>
  <div class="tabs" id="tabs"></div>
  <div class="panel" id="panel"></div>
</div>
</main>
<script>
const GAMES=__GAMES_JSON__;
const LABEL=Object.fromEntries(GAMES);
const STATE={running:"Running",starting:"Starting (can take up to 10 min before you can join)",
  stopped:"Stopped",stopping:"Stopping (saving and backing up)"};
const VMSTATE={running:"VM is on",starting:"VM is starting",stopped:"VM is off (no compute cost)",
  stopping:"VM is shutting down",unknown:"VM state unknown"};
const $=id=>document.getElementById(id);
// Surface script errors on the page; phones have no easy console to check.
window.addEventListener("error",e=>{$("vm").textContent="Page error: "+(e.message||"unknown")+" - please reload"});
window.addEventListener("unhandledrejection",e=>{$("vm").textContent="Page error: "+((e.reason&&e.reason.message)||"request failed")+" - please reload"});
const el=(tag,props={},...kids)=>{const e=Object.assign(document.createElement(tag),props);e.append(...kids);return e};
$("pw").value=sessionStorage.getItem("pw")||"";
let status=null,role=null,backups=[],backupKey=null,active=sessionStorage.getItem("tab")||"palworld";
const panels={},settingsState={},rolesState={},startUI={};

async function call(path,method,body,msgEl){
  const headers={"x-portal-key":$("pw").value};
  if(body)headers["content-type"]="application/json";
  let r;
  // The portal scales to zero, so the first request after a quiet period can briefly return 503.
  for(let attempt=0;attempt<4;attempt++){
    try{r=await fetch("/api/"+path,{method,headers,body:body?JSON.stringify(body):undefined})}
    catch(_){r=null}
    if(r&&r.status!==503)break;
    if(attempt<3){(msgEl||$("vm")).textContent="Waking up the portal...";await new Promise(res=>setTimeout(res,4000))}
  }
  if(!r){(msgEl||$("vm")).textContent="Network error, try again";return null}
  if(r.status===401){$("vm").textContent="Wrong password";$("app").hidden=true;return null}
  if(r.status===429){$("vm").textContent="Too many attempts, wait a few minutes";return null}
  if(!r.ok){
    let e={};try{e=await r.json()}catch(_){}
    (msgEl||$("vm")).textContent=e.error||"Error, try again";
    return null;
  }
  sessionStorage.setItem("pw",$("pw").value);
  $("app").hidden=false;
  return r.json();
}

function buildPanel(g){
  const p={};
  p.state=el("div",{className:"state"});
  p.info=el("div",{className:"info"});
  p.fail=el("div",{className:"failbox",hidden:true});
  p.sfill=el("span",{className:"pfill"});
  p.slabel=el("span",{className:"plabel",textContent:"Start"});
  p.start=el("button",{className:"start"},p.sfill,p.slabel);
  // Announces startup steps to screen readers (once per step, not every second).
  p.live=el("div",{className:"sr-only"});p.live.setAttribute("role","status");
  p.stop=el("button",{className:"stop",textContent:"Stop"});
  p.smsg=el("div",{className:"msg"});
  p.start.onclick=async()=>{
    startUI[g]={key:null,t0:Date.now()/1000,max:0,pendingUntil:Date.now()/1000+30};
    paintStart(g);
    const r=await call("start","POST",{game:g},p.smsg);
    if(!r){delete startUI[g];paintStart(g);return}
    r.role=role;render(r);refresh();
  };
  p.stop.onclick=async()=>{
    if(!confirm("Save, back up and stop "+LABEL[g]+"?"))return;
    p.stop.disabled=true;
    const r=await call("stop","POST",{game:g},p.smsg);
    if(r){r.role=role;render(r)}
    refresh();
  };
  const server=el("div",{className:"section"},el("h2",{textContent:"Server"}),p.state,p.info,p.fail,
    el("div",{className:"btns"},p.start,p.stop),p.smsg,p.live);

  p.bsel=el("select",{className:"wide",size:6},el("option",{disabled:true,textContent:"Loading backups..."}));
  p.bnow=el("button",{className:"blue",textContent:"Back up now"});
  p.brestore=el("button",{className:"orange",textContent:"Restore selected"});
  p.bmsg=el("div",{className:"msg"});
  p.bnow.onclick=async()=>{
    p.bnow.disabled=true;p.bmsg.textContent="Backing up...";
    if(await call("backup","POST",{game:g},p.bmsg))p.bmsg.textContent="Backup started, it will appear within a minute.";
    setTimeout(refresh,45000);
  };
  p.brestore.onclick=async()=>{
    const name=p.bsel.value;
    if(!name){p.bmsg.textContent="Select a backup first";return}
    const when=p.bsel.selectedOptions[0].textContent;
    if(!confirm("Restore the "+LABEL[g]+" world to:\n"+when+"\n\n"+LABEL[g]+" players will be disconnected. The current world is backed up first as 'pre-restore', so this can be undone."))return;
    p.brestore.disabled=true;p.bmsg.textContent="Restoring...";
    if(await call("restore","POST",{name},p.bmsg))
      p.bmsg.textContent="Restore started. If "+LABEL[g]+" was running it restarts in about a minute. A 'pre-restore' backup will appear in the list.";
    setTimeout(refresh,90000);
  };
  p.bdownload=el("button",{className:"ghost",textContent:"Download selected"});
  p.bdownload.onclick=async()=>{
    const name=p.bsel.value;
    if(!name){p.bmsg.textContent="Select a backup first";return}
    p.bmsg.textContent="Preparing download...";
    const d=await call("download","POST",{name},p.bmsg);
    if(!d)return;
    window.location.href=d.url;
    p.bmsg.textContent="Download started ("+name+"). The link expires in "+d.expiresInMinutes+" minutes.";
  };
  const backupsSec=el("div",{className:"section"},el("h2",{textContent:"World backups (kept 14 days)"}),
    p.bsel,el("div",{className:"btns"},p.bnow,p.brestore),el("div",{className:"btns"},p.bdownload),p.bmsg);

  p.plist=el("div",{className:"plist"});
  p.pmsg=el("div",{className:"msg"});
  const playersSec=el("div",{className:"section"},el("h2",{textContent:"Players"}),
    el("div",{className:"note",textContent:g==="palworld"
      ?"Updates about once a minute while the game is running."
      :"Updates about once a minute. Enshrouded has no remote kick or ban; players with a role that allows kick/ban can do it in-game from the Social tab."}),
    p.plist,p.pmsg);

  p.presets=el("div",{className:"presets"});
  p.form=el("div",{},el("div",{className:"note",textContent:"Loading settings..."}));
  p.save=el("button",{className:"blue",textContent:"Save"});
  p.apply=el("button",{className:"orange",textContent:"Save and restart now"});
  p.setmsg=el("div",{className:"msg"});
  p.save.onclick=()=>saveSettings(g,false);
  p.apply.onclick=()=>{
    if(confirm("Save and restart "+LABEL[g]+" now? "+LABEL[g]+" players will be disconnected for a few minutes."))saveSettings(g,true);
  };
  const settingsSec=el("div",{className:"section"},el("h2",{textContent:"Settings"}),
    el("div",{className:"note",textContent:"Save applies the next time the game starts."}),
    p.presets,p.form,el("div",{className:"btns"},p.save,p.apply),p.setmsg);

  p.adminOnly=[backupsSec,settingsSec];
  const sections=[server,playersSec,backupsSec,settingsSec];
  if(g==="enshrouded"){
    p.roles=el("div",{},el("div",{className:"note",textContent:"Loading roles..."}));
    p.rpublic=el("select",{className:"pubsel"});
    p.radd=el("button",{className:"ghost",textContent:"Add role"});
    p.rsave=el("button",{className:"blue",textContent:"Save roles"});
    p.rapply=el("button",{className:"orange",textContent:"Save and restart now"});
    p.rmsg=el("div",{className:"msg"});
    p.radd.onclick=()=>{
      if(!rolesState.enshrouded){p.rmsg.textContent="Roles are still loading";return}
      if(roleCards(p).length>=8){p.rmsg.textContent="Up to 8 roles";return}
      p.roles.append(roleCard(p,{name:"",password:"",reservedSlots:0}));refreshPublicChoices(p);
    };
    p.rsave.onclick=()=>saveRoles(false);
    p.rapply.onclick=()=>{if(confirm("Save roles and restart Enshrouded now? Players will be disconnected for a few minutes."))saveRoles(true)};
    const rolesSec=el("div",{className:"section"},el("h2",{textContent:"Roles (join passwords and permissions)"}),
      el("div",{className:"note",textContent:"Players type a role's password when joining and get that role's permissions. Each role needs a different password. Changes apply the next time Enshrouded starts."}),
      p.roles,el("div",{className:"row"},el("label",{textContent:"Password shown on the portal"}),p.rpublic),
      el("div",{className:"btns"},p.radd),el("div",{className:"btns"},p.rsave,p.rapply),p.rmsg);
    p.adminOnly.push(rolesSec);
    sections.splice(2,0,rolesSec);
  }
  p.root=el("div",{},...sections);
  return p;
}

const PERM_LABELS={canKickBan:"Kick and ban",canAccessInventories:"Access inventories",canEditWorld:"Edit world",
  canEditBase:"Edit bases",canExtendBase:"Extend bases"};
function roleCard(p,r){
  const card=el("div",{className:"role"});
  const field=(label,input)=>el("div",{className:"row"},el("label",{textContent:label}),input);
  card.nameEl=el("input",{type:"text",maxLength:24,value:r.name||""});
  card.pwEl=el("input",{type:"text",maxLength:32,value:r.password||"",autocomplete:"off"});
  card.slotsEl=el("input",{type:"number",min:0,max:16,step:1,value:r.reservedSlots||0});
  card.perms={};
  card.append(field("Role name",card.nameEl),field("Password",card.pwEl));
  for(const [k,label] of Object.entries(PERM_LABELS)){
    card.perms[k]=el("input",{type:"checkbox",checked:!!r[k]});card.append(field(label,card.perms[k]));
  }
  const remove=el("button",{className:"stop",textContent:"Remove role"});
  remove.onclick=()=>{
    if(roleCards(p).length<=1){p.rmsg.textContent="Keep at least one role";return}
    card.remove();refreshPublicChoices(p);
  };
  card.nameEl.oninput=()=>refreshPublicChoices(p);
  card.append(field("Reserved slots",card.slotsEl),el("div",{className:"btns"},remove));
  return card;
}
function refreshPublicChoices(p,selected){
  const cur=selected!==undefined?selected:p.rpublic.value;
  const names=roleCards(p).map(c=>c.nameEl.value.trim()).filter(Boolean);
  p.rpublic.replaceChildren(el("option",{value:"",textContent:"(none)"}),...names.map(n=>el("option",{value:n,textContent:n})));
  p.rpublic.value=names.includes(cur)?cur:"";
}
let loadingRoles=false;
async function loadRoles(){
  if(loadingRoles)return;
  loadingRoles=true;
  try{
    const p=panels.enshrouded;
    const d=await call("roles","GET",null,p.rmsg);
    if(!d)return;
    rolesState.enshrouded=d;
    p.roles.replaceChildren(...d.roles.map(r=>roleCard(p,r)));
    refreshPublicChoices(p,d.public||"");
  }finally{loadingRoles=false}
}
const roleCards=p=>[...p.roles.children].filter(c=>c.nameEl);
async function saveRoles(applyNow){
  const p=panels.enshrouded;
  if(!rolesState.enshrouded){p.rmsg.textContent="Roles are still loading";return}
  const roles=roleCards(p).map(c=>{
    const r={name:c.nameEl.value.trim(),password:c.pwEl.value,reservedSlots:parseInt(c.slotsEl.value||"0",10)};
    for(const k of Object.keys(PERM_LABELS))r[k]=c.perms[k].checked;
    return r;
  });
  p.rmsg.textContent="Saving...";
  const r=await call("roles","POST",{roles,public:p.rpublic.value,applyNow},p.rmsg);
  if(!r)return;
  p.rmsg.textContent=r.applied?"Saved. Enshrouded is restarting with the new roles (a 'pre-settings' backup is made first); progress shows on the Start button."
    :"Saved. The new roles apply the next time Enshrouded starts.";
  if(r.applied)showRestart("enshrouded");
}
// Shows a "Save and restart now" on the game's Start button until the VM reports its own steps.
function showRestart(g){
  const now=Date.now()/1000;
  startUI[g]={key:null,t0:now,max:0,restart:true,pendingUntil:now+120};
  paintStart(g);
  panels[g].start.scrollIntoView({behavior:"smooth",block:"center"});
  refresh();
}

function renderPlayers(g,s){
  const p=panels[g];
  p.plist.textContent="";
  if(s.state!=="running"){p.plist.textContent="Start the game to see who is online.";return}
  const list=s.playerList||[];
  if(!list.length)p.plist.append(el("div",{className:"note",textContent:"Nobody is online."}));
  for(const pl of list){
    const row=el("div",{className:"prow"},el("span",{textContent:(pl.name||"(unnamed)")+(pl.level?" (level "+pl.level+")":"")}));
    if(role==="admin"&&g==="palworld"&&pl.id){
      for(const [action,label,cls] of [["kick","Kick","orange"],["ban","Ban","stop"]]){
        const b=el("button",{className:cls,textContent:label});
        b.onclick=()=>playerAction(g,action,pl.id,pl.name);
        row.append(b);
      }
    }
    p.plist.append(row);
  }
  if(g==="palworld"&&role==="admin"){
    const bans=s.bans||[];
    p.plist.append(el("h3",{textContent:"Banned ("+bans.length+")"}));
    for(const id of bans){
      const b=el("button",{className:"ghost",textContent:"Unban"});
      b.onclick=()=>playerAction(g,"unban",id,id);
      p.plist.append(el("div",{className:"prow"},el("span",{textContent:id}),b));
    }
  }
}
async function playerAction(g,action,id,name){
  const p=panels[g];
  const verb={kick:"Kick",ban:"Ban",unban:"Unban"}[action];
  if(!confirm(verb+" "+name+"?"))return;
  p.pmsg.textContent=verb+" in progress...";
  if(await call("player","POST",{game:g,action,id},p.pmsg))
    p.pmsg.textContent=verb+" sent. The list updates within a minute.";
  setTimeout(refresh,30000);
}

function showTab(g){
  active=g;sessionStorage.setItem("tab",g);
  for(const b of $("tabs").children)b.classList.toggle("active",b.dataset.game===g);
  $("panel").replaceChildren(panels[g].root);
  if(role==="admin"&&!settingsState[g])loadSettings(g);
  if(role==="admin"&&g==="enshrouded"&&!rolesState.enshrouded)loadRoles();
}

function initTabs(){
  if($("tabs").children.length)return;
  for(const [g,label] of GAMES){
    panels[g]=buildPanel(g);
    const b=el("button",{className:"tab"},el("span",{className:"dot"}),label);
    b.dataset.game=g;b.onclick=()=>showTab(g);
    $("tabs").append(b);
  }
  showTab(LABEL[active]?active:GAMES[0][0]);
}

function line(parent,label,value){parent.append(label+": ",el("b",{textContent:value}),el("br"))}
const gb=kb=>(kb/1048576).toFixed(1)+" GB";
function meter(label,pct,text){
  const fill=el("div");fill.style.width=Math.max(0,Math.min(100,pct))+"%";
  if(pct>=90)fill.className="bad";else if(pct>=75)fill.className="warn";
  return el("div",{className:"meter"},el("span",{textContent:label}),el("div",{className:"bar"},fill),el("span",{textContent:text}));
}
function renderUsage(d){
  const box=$("usage"),r=d.resources,h=historySamples,hasHist=h.length>=2;
  // Usage is only shown while the VM is running; nothing is kept between sessions.
  box.hidden=!r;
  if(box.hidden)return;
  const kids=[el("h2",{textContent:"Server usage (updates every minute)"})];
  if(hasHist){
    const gv=(s,k,f)=>s.g&&s.g[k]&&s.g[k][f]!=null?s.g[k][f]:null;
    const toGb=v=>v==null?null:v/1048576;
    kids.push(lineChart("CPU (% of all cores; dotted line = 5-second peaks)",h,
      (s,k)=>k==="vm"?s.cpu:gv(s,k,"cpu"),100,v=>v+"%",v=>Math.round(v)+"%",
      (s,k)=>k==="vm"?s.cpuMax:gv(s,k,"cpuMax")));
    const memMax=Math.ceil(((h[h.length-1].memTotal)||(r&&r.memTotalKb)||16777216)/1048576);
    kids.push(lineChart("Memory (GB)",h,(s,k)=>toGb(k==="vm"?s.mem:gv(s,k,"mem")),
      memMax,v=>Math.round(v)+"G",v=>v.toFixed(1)+" GB",(s,k)=>toGb(k==="vm"?s.memMax:gv(s,k,"memMax"))));
    const pk=f=>h.reduce((m,s)=>s[f]!=null&&s[f]>m?s[f]:m,0);
    if(r&&(r.cpuPressure!=null||r.memPressure!=null)){
      kids.push(el("div",{className:"pressure",textContent:"Time spent waiting (overload): CPU "+(r.cpuPressure==null?0:r.cpuPressure)+"%, memory "+
        (r.memPressure==null?0:r.memPressure)+"% in the last minute; session peak CPU "+pk("pCpu")+"%, memory "+pk("pMem")+"%"}));
    }
  }else if(r){kids.push(el("div",{className:"note",textContent:"Collecting data for the charts..."}))}
  if(r){
    const diskUsedPct=100*(r.diskTotalKb-r.diskFreeKb)/r.diskTotalKb;
    kids.push(meter("Swap",r.swapTotalKb?100*r.swapUsedKb/r.swapTotalKb:0,gb(r.swapUsedKb)+" of "+gb(r.swapTotalKb)),
      meter("Disk",diskUsedPct,gb(r.diskFreeKb)+" free"));
  }
  box.replaceChildren(...kids);
  if(!r)return;
  const memPct=100*(r.memTotalKb-r.memAvailableKb)/r.memTotalKb;
  const w=[];
  if(memPct>=90)w.push("Memory is nearly full. Players may see lag or a game may crash; consider a larger VM.");
  if(r.swapUsedKb>262144)w.push("The server is using swap (disk as memory), which slows games down.");
  if(r.cpu!=null&&r.cpu>=90)w.push("CPU is maxed out. Expect lag; consider a larger VM.");
  else if(r.cpuMax!=null&&r.cpuMax>=95)w.push("CPU briefly hit "+r.cpuMax+"% in the last minute. Short spikes are normal; frequent ones mean the VM is undersized.");
  if(r.cpuPressure!=null&&r.cpuPressure>=25)w.push("Programs waited for CPU "+r.cpuPressure+"% of the last minute. Players are likely seeing lag; consider a VM with more cores.");
  else if(r.cpuPressure!=null&&r.cpuPressure>=10)w.push("Programs waited for CPU "+r.cpuPressure+"% of the last minute. Some lag is possible.");
  if(r.memPressure!=null&&r.memPressure>=15)w.push("Programs waited for memory "+r.memPressure+"% of the last minute. Players are likely seeing lag; consider a VM with more memory.");
  else if(r.memPressure!=null&&r.memPressure>=5)w.push("Programs waited for memory "+r.memPressure+"% of the last minute. Some lag is possible.");
  if(r.diskFreeKb<5242880)w.push("Less than 5 GB of disk is free. Game updates may fail.");
  if(w.length&&role==="admin")box.append(el("div",{className:"warnings"},...w.map(t=>el("div",{textContent:"\u26a0 "+t}))));
}

// Lightweight SVG line charts (no library). Gaps longer than 3 minutes (VM off) break the line.
const SVGNS="http://www.w3.org/2000/svg";
function svgEl(tag,attrs,text){
  const e=document.createElementNS(SVGNS,tag);
  for(const k in attrs)e.setAttribute(k,attrs[k]);
  if(text!=null)e.textContent=text;
  return e;
}
const SERIES_COLORS={vm:"#e8eef3",palworld:"#5b9be8",enshrouded:"#e0a03a"};
function hhmm(t){return new Date(t*1000).toLocaleTimeString([],{hour:"numeric",minute:"2-digit"})}
function lineChart(title,samples,pick,maxY,fmtAxis,fmtLatest,pickPeak){
  // pick: the per-minute value drawn as the line; pickPeak: the 5-second peak in that minute,
  // drawn as a faint dotted line for the server and used for the "peak" in the legend.
  pickPeak=pickPeak||pick;
  const W=400,H=120,L=36,R=6,T=8,B=18,pw=W-L-R,ph=H-T-B;
  const t0=samples[0].t,t1=samples[samples.length-1].t,span=Math.max(60,t1-t0);
  const x=t=>L+pw*(t-t0)/span,y=v=>T+ph*(1-Math.max(0,Math.min(v,maxY))/maxY);
  const chart=svgEl("svg",{viewBox:"0 0 "+W+" "+H,class:"chart",role:"img","aria-label":title});
  for(const f of [0,0.5,1]){
    const yy=T+ph*(1-f);
    chart.append(svgEl("line",{x1:L,x2:W-R,y1:yy,y2:yy,class:"grid"}));
    chart.append(svgEl("text",{x:L-4,y:yy+3,"text-anchor":"end",class:"axis"},fmtAxis(Math.round(maxY*f))));
  }
  const live=Date.now()/1000-t1<180;
  chart.append(svgEl("text",{x:L,y:H-4,class:"axis"},hhmm(t0)));
  chart.append(svgEl("text",{x:W-R,y:H-4,"text-anchor":"end",class:"axis"},live?"now":hhmm(t1)));
  const legend=el("div",{className:"legend"});
  for(const key of ["vm"].concat(GAMES.map(g=>g[0]))){
    let path="",spikes="",prevT=null,last=null,peak=null;
    for(const s of samples){
      const v=pick(s,key);
      if(v==null){prevT=null;continue}
      const pv=pickPeak(s,key),hi=pv==null?v:Math.max(v,pv);
      const cmd=prevT==null||s.t-prevT>180?"M":"L";
      path+=cmd+x(s.t).toFixed(1)+" "+y(v).toFixed(1)+" ";
      spikes+=cmd+x(s.t).toFixed(1)+" "+y(hi).toFixed(1)+" ";
      prevT=s.t;last=v;if(peak==null||hi>peak)peak=hi;
    }
    if(!path)continue;
    if(key==="vm"){
      chart.append(svgEl("line",{x1:L,x2:W-R,y1:y(peak),y2:y(peak),class:"peak"}));
      if(pickPeak!==pick)chart.append(svgEl("path",{d:spikes,fill:"none",stroke:SERIES_COLORS[key],class:"spike"}));
    }
    chart.append(svgEl("path",{d:path,fill:"none",stroke:SERIES_COLORS[key],"stroke-width":key==="vm"?2:1.5,"stroke-linejoin":"round"}));
    const sw=el("i",{className:"sw"});sw.style.background=SERIES_COLORS[key];
    legend.append(el("span",{},sw,(key==="vm"?"Server":LABEL[key])+" "+(live?fmtLatest(last)+", ":"")+"peak "+fmtLatest(peak)));
  }
  return el("div",{className:"chartbox"},el("div",{className:"ctitle",textContent:title}),chart,legend);
}

let historySamples=[],lastHistoryFetch=0;
async function loadHistory(){
  lastHistoryFetch=Date.now();
  // Errors here are not worth interrupting the page for; the next refresh tries again.
  const d=await call("history","GET",null,el("div"));
  if(!d||!Array.isArray(d.samples))return;
  historySamples=d.samples.filter(s=>s&&typeof s.t==="number").sort((a,b)=>a.t-b.t);
  if(status)renderUsage(status);
}

// ---- Start progress ----------------------------------------------------------------------------
// Steps come from the VM (it reads the game's log during a start). Within a step the bar eases toward
// the next step over that step's typical duration, uses real percentages when a download reports
// them, never moves backwards, and only reaches 100% when the server answers.
const STEP_LABEL={vm:"Waking up the server machine",launch:"Preparing the game server",
  update:"Checking for game updates",server:"Starting the game server",world:"Loading the world",
  warn:"Warning players (30 seconds)",backup:"Backing up the world",stop:"Stopping the server"};
const STEP_WEIGHT={vm:40,launch:10,update:20,server:15,world:15,warn:20,backup:8,stop:7};
const STEP_SECONDS={vm:45,launch:12,update:12,server:10,world:12,warn:30,backup:8,stop:8};
// "Save and restart now": Palworld warns players for 30 s first; both back up and stop before starting.
function restartSteps(g){return (g==="palworld"?["warn"]:[]).concat(["backup","stop","launch","update","server","world"])}
function fmtElapsed(sec){sec=Math.max(0,Math.floor(sec));return Math.floor(sec/60)+":"+String(sec%60).padStart(2,"0")}
function startInfo(s,d,ui,g){
  const now=Date.now()/1000,t0=s.requestedAt||ui.t0,elapsed=Math.max(0,now-t0);
  // Keep the last step seen if an update arrives without one, so the text never steps backwards.
  if(s.progress)ui.lastPr=s.progress;
  const pr=s.progress||(d.vm==="running"?ui.lastPr:null);
  // The VM step only counts if this start had to boot it.
  const boot=!ui.restart&&(d.vm!=="running"||(d.vmUptime!=null&&d.vmUptime<elapsed+20)||ui.sawBoot);
  if(d.vm!=="running")ui.sawBoot=true;
  const order=ui.restart?restartSteps(g):boot?["vm","launch","update","server","world"]:["launch","update","server","world"];
  if(pr&&pr.stage==="ready")return {done:true,pct:100,label:"Ready",step:order.length,steps:order.length,elapsed};
  let stage,since,label,frac=null;
  if(d.vm!=="running"){stage="vm";since=t0}
  else if(pr&&order.indexOf(pr.stage)>=0){stage=pr.stage;since=pr.stageSince||now;label=pr.label;if(pr.pct!=null)frac=pr.pct/100}
  else{stage="launch";if(!ui.launchSeen)ui.launchSeen=now;since=ui.launchSeen}
  let before=0,total=0;
  for(const st of order){total+=STEP_WEIGHT[st];if(order.indexOf(st)<order.indexOf(stage))before+=STEP_WEIGHT[st]}
  const f=frac!=null?Math.min(1,frac):0.92*(1-Math.exp(-Math.max(0,now-Math.min(since,now))/STEP_SECONDS[stage]));
  label=(label||STEP_LABEL[stage])+(frac!=null?" "+Math.round(frac*100)+"%":"");
  return {pct:Math.min(99,100*(before+STEP_WEIGHT[stage]*f)/total),label,step:order.indexOf(stage)+1,steps:order.length,elapsed};
}
function paintStart(g){
  const p=panels[g],d=status;
  if(!p||!d)return;
  const s=d.games[g]||{state:"stopped"},now=Date.now()/1000;
  let ui=startUI[g];
  let info=null;
  if(s.state==="starting"){
    if(!ui||(ui.key&&s.requestedAt&&ui.key!==s.requestedAt))ui=startUI[g]={t0:now,max:0};
    ui.key=s.requestedAt||ui.key;ui.pendingUntil=0;ui.doneAt=ui.doneAt||null;ui.started=true;
    if(s.restarting)ui.restart=true;
    info=startInfo(s,d,ui,g);
    if(info.done&&!ui.doneAt)ui.doneAt=now;
  }else if(ui&&ui.pendingUntil>now&&s.state==="stopped"){
    // Clicked Start; the first status update has not arrived yet.
    info={pct:3,label:"Sending the start request",step:0,steps:0,elapsed:now-ui.t0};
  }else if(ui&&ui.restart&&!ui.started&&ui.pendingUntil>now&&s.state==="running"){
    // Clicked "Save and restart now"; the VM has not reported the first restart step yet.
    info={pct:2,label:"Sending the restart request",step:0,steps:0,elapsed:now-ui.t0};
  }else if(ui&&s.state==="running"&&((ui.restart?ui.started:ui.max>0)||ui.doneAt)){
    if(!ui.doneAt)ui.doneAt=now;
    if(now-ui.doneAt<2.5)info={done:true,pct:100,label:"Ready",step:0,steps:0,elapsed:0};
  }
  if(!info){
    if(ui&&!(ui.pendingUntil>now))delete startUI[g];
    p.start.classList.remove("busy","done");
    p.sfill.style.width="0";
    p.slabel.textContent="Start";
    p.start.removeAttribute("aria-label");
    p.start.disabled=s.state!=="stopped"||d.vm==="stopping";
    p.state.textContent=STATE[s.state]||s.state;
    return;
  }
  ui.max=Math.max(ui.max||0,info.pct);
  p.start.classList.add("busy");
  p.start.classList.toggle("done",!!info.done);
  p.start.disabled=true;
  p.sfill.style.width=(info.done?100:ui.max).toFixed(1)+"%";
  const verb=ui.restart?"Restarting":"Starting";
  p.slabel.textContent=info.done?"Ready \u2713":verb+"\u2026 "+fmtElapsed(info.elapsed);
  const stepText=info.done?"Ready":(info.steps?"Step "+info.step+" of "+info.steps+": ":"")+info.label;
  p.state.textContent=stepText;
  p.start.setAttribute("aria-label",info.done?LABEL[g]+" is ready":verb+" "+LABEL[g]+". "+stepText);
  if(ui.announced!==stepText){ui.announced=stepText;p.live.textContent=LABEL[g]+": "+stepText}
}
function anyBusy(){
  if(!status)return false;
  const now=Date.now()/1000;
  if(status.vm==="starting"||status.vm==="stopping")return true;
  for(const k in status.games){const st=status.games[k].state;if(st==="starting"||st==="stopping")return true}
  for(const k in startUI)if(startUI[k].pendingUntil>now)return true;
  return false;
}

function render(d){
  if(!d)return;
  if(d.role===undefined)d.role=role;
  const roleChanged=d.role!==role;
  status=d;role=d.role;initTabs();
  $("vm").textContent=(VMSTATE[d.vm]||d.vm)+(role==="player"?" - player access":"");
  renderUsage(d);
  for(const [g] of GAMES){
    const s=d.games[g]||{state:"stopped"},p=panels[g];
    for(const sec of p.adminOnly)sec.hidden=role!=="admin";
    p.stop.hidden=role!=="admin";
    for(const b of $("tabs").children)if(b.dataset.game===g)b.querySelector(".dot").className="dot "+s.state;
    p.state.textContent=STATE[s.state]||s.state;
    p.info.textContent="";
    if(s.name)line(p.info,"Server name",s.name);
    if(s.address)line(p.info,"Address",s.address);
    if(s.password)line(p.info,"Password",s.password);
    if(s.state==="running"&&s.players!=null)line(p.info,"Players online",String(s.players));
    if(s.usage)line(p.info,"Usage",(s.usage.cpu==null?"measuring CPU":s.usage.cpu+"% CPU")+", "+gb(s.usage.memKb)+" memory");
    // A failed start stays visible until the next attempt begins.
    p.fail.textContent="";
    p.fail.hidden=!s.lastFailure||s.state==="starting"||s.state==="running";
    if(!p.fail.hidden){
      const f=s.lastFailure,when=f.time?new Date(f.time*1000).toLocaleString():"";
      p.fail.append(el("div",{className:"failtitle",textContent:"\u26a0 The last start failed"+(when?" ("+when+")":"")+". "+(f.reason||"")}),
        el("div",{className:"note",textContent:role==="admin"?"Press Start to try again. Last lines from the server's log:":"Press Start to try again; if it keeps failing, let the admin know."}));
      if(role==="admin"&&f.log&&f.log.length)p.fail.append(el("pre",{className:"faillog",textContent:f.log.join("\n")}));
    }
    p.stop.disabled=!["running","starting"].includes(s.state);
    paintStart(g);
    p.bnow.disabled=s.state!=="running";
    p.brestore.disabled=d.vm!=="running";
    p.apply.disabled=s.state!=="running";
    if(p.rapply)p.rapply.disabled=s.state!=="running";
    renderPlayers(g,s);
  }
  if(roleChanged&&role==="admin"&&!settingsState[active])loadSettings(active);
}

function labelOf(name){return name.replace(/^[a-z]+-\d{8}-\d{6}-/,"").replace(/\.tar\.gz$/,"")}
function renderBackups(){
  for(const [g] of GAMES){
    const sel=panels[g].bsel,prev=sel.value;sel.textContent="";
    const list=backups.filter(b=>b.name.startsWith(g+"-"));
    if(!list.length)sel.append(el("option",{disabled:true,textContent:"No backups yet"}));
    for(const b of list){
      sel.append(el("option",{value:b.name,
        textContent:new Date(b.modified).toLocaleString()+"  -  "+labelOf(b.name)+"  ("+(b.size/1048576).toFixed(1)+" MB)"}));
    }
    if(prev)sel.value=prev;
  }
}
async function loadBackups(){
  const d=await call("backups","GET");
  if(!d)return;
  const key=d.backups.map(b=>b.name).join("|");
  if(key===backupKey)return;
  backupKey=key;backups=d.backups;renderBackups();
}

function makeField(s,val){
  let f;
  if(s.type==="bool")f=el("input",{type:"checkbox"});
  else if(s.type==="enum")f=el("select",{},...s.options.map(o=>el("option",{value:o,textContent:o})));
  else if(s.type==="text")f=el("input",{type:"text",maxLength:40});
  else f=el("input",{type:"number",step:s.type==="int"?"1":(s.type==="minutes"?"1":"0.05"),min:s.min,max:s.max});
  f.dataset.key=s.key;setField(f,s,val);return f;
}
function setField(f,s,val){
  if(s.type==="bool")f.checked=String(val).toLowerCase()==="true";
  else if(s.type==="float"||s.type==="minutes"||s.type==="int")f.value=parseFloat(val);
  else f.value=val;
}
function applyDependencies(p,schema){
  let anyDisabled=false;
  for(const s of schema){
    if(!s.enabledWhen)continue;
    const ctl=p.form.querySelector('[data-key="'+s.enabledWhen.key+'"]');
    const on=!ctl||ctl.value===s.enabledWhen.value;
    const f=p.form.querySelector('[data-key="'+s.key+'"]');
    if(!f)continue;
    // Disabled controls still keep their value and are still saved.
    f.disabled=!on;
    const row=f.closest(".row");if(row)row.classList.toggle("off",!on);
    if(!on)anyDisabled=true;
  }
  for(const h of p.form.querySelectorAll(".dephint"))h.hidden=!anyDisabled;
}
const loadingSettings={};
async function loadSettings(g){
  if(loadingSettings[g])return;
  loadingSettings[g]=true;
  try{await loadSettingsNow(g)}finally{loadingSettings[g]=false}
}
async function loadSettingsNow(g){
  const p=panels[g];
  const d=await call("settings?game="+g,"GET",null,p.setmsg);
  if(!d)return;
  settingsState[g]=d;
  p.form.textContent="";
  let group="";
  for(const s of d.schema){
    if(s.group!==group){
      group=s.group;p.form.append(el("h3",{textContent:group}));
      if(s.enabledWhen)p.form.append(el("div",{className:"note dephint",textContent:s.enabledWhen.hint}));
    }
    const row=el("div",{className:"row"},el("label",{textContent:s.label}),makeField(s,d.values[s.key]));
    if(s.enabledWhen)row.dataset.dep=s.enabledWhen.key;
    p.form.append(row);
  }
  // Fields that only matter for a certain value of another field are disabled otherwise, live.
  const deps=new Set(d.schema.filter(s=>s.enabledWhen).map(s=>s.enabledWhen.key));
  for(const key of deps){
    const ctl=p.form.querySelector('[data-key="'+key+'"]');
    if(ctl)ctl.addEventListener("change",()=>applyDependencies(p,d.schema));
  }
  applyDependencies(p,d.schema);
  p.presets.textContent="";
  for(const [name,vals] of Object.entries(d.presets)){
    const b=el("button",{textContent:name});
    b.onclick=()=>{
      for(const s of d.schema){
        if(s.key==="SERVER_NAME")continue;
        const f=p.form.querySelector('[data-key="'+s.key+'"]');
        setField(f,s,s.key in vals?vals[s.key]:s.default);
      }
      applyDependencies(p,d.schema);
      p.setmsg.textContent=name+" preset loaded. Review it, then save.";
    };
    p.presets.append(b);
  }
}
async function saveSettings(g,applyNow){
  const p=panels[g],d=settingsState[g];
  if(!d)return;
  const values={};
  for(const s of d.schema){
    const f=p.form.querySelector('[data-key="'+s.key+'"]');
    values[s.key]=s.type==="bool"?f.checked:f.value;
  }
  p.setmsg.textContent="Saving...";
  const r=await call("settings","POST",{game:g,values,applyNow},p.setmsg);
  if(!r)return;
  p.setmsg.textContent=r.applied
    ?"Saved. "+LABEL[g]+" is restarting with the new settings (a 'pre-settings' backup is made first); progress shows on the Start button."
    :"Saved. The new settings apply the next time "+LABEL[g]+" starts.";
  if(r.applied)showRestart(g);
}

async function refresh(){
  if(!$("pw").value)return;
  const d=await call("status","GET");
  if(!d)return;
  render(d);
  // History only exists while the VM is on; refresh it about once a minute.
  if(d.resources&&Date.now()-lastHistoryFetch>55000)loadHistory();
  if(!d.resources)historySamples=[];
  if(role==="admin"){
    // Load everything the admin view needs at once rather than one request after another,
    // so switching tabs is instant too.
    loadBackups();
    for(const [g] of GAMES)if(!settingsState[g])loadSettings(g);
    if(LABEL.enshrouded&&!rolesState.enshrouded)loadRoles();
  }
}
function showLoading(p){
  p.form.replaceChildren(el("div",{className:"note",textContent:"Loading settings..."}));
  p.presets.textContent="";p.setmsg.textContent="";p.plist.textContent="";
  if(p.roles){p.roles.replaceChildren(el("div",{className:"note",textContent:"Loading roles..."}));p.rmsg.textContent=""}
}
$("pw").addEventListener("change",()=>{
  role=null;historySamples=[];lastHistoryFetch=0;backupKey=null;
  for(const k in settingsState)delete settingsState[k];
  delete rolesState.enshrouded;
  // Drop forms loaded under the previous password so stale controls can't be edited.
  for(const p of Object.values(panels)){
    p.form.textContent="";p.presets.textContent="";p.setmsg.textContent="";p.plist.textContent="";
    if(p.roles){p.roles.textContent="";p.rmsg.textContent=""}
    showLoading(p);
  }
  refresh();
});
// Poll every 3 s while something is starting or stopping, otherwise every 15 s. Between polls the
// start progress keeps ticking (elapsed time and easing) without extra requests.
async function pollLoop(){
  try{await refresh()}catch(_){}
  setTimeout(pollLoop,anyBusy()?3000:15000);
}
pollLoop();
setInterval(()=>{if(status)for(const [g] of GAMES)paintStart(g)},1000);
</script></body></html>"""

PAGE = PAGE.replace("__GAMES_JSON__", json.dumps([[g, info["label"]] for g, info in GAMES.items()]))
