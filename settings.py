"""
Runtime settings, owned by the web UI.

Everything here lives in settings.json on the state volume and can be changed
from the Settings tab without touching the container. The environment variables
in docker-compose.yml are only the seed values used the first time the container
starts with an empty settings.json -- after that the file wins, so editing the
compose has no effect on a setting you have already changed in the UI.

Container plumbing (ports, volume paths, whether the web server runs at all)
stays with Docker: it cannot meaningfully change while the process is running.
Those are exposed read-only so the UI can show what they are.
"""
import json
import os

import config
from store import log

SETTINGS_PATH = os.environ.get("SETTINGS_PATH", "/state/settings.json")

# key, type, group, label, help, and per-type constraints. `key` matches the
# attribute on config, which is what the rest of the code reads.
SCHEMA = [
    # --- safety ---
    dict(key="DRY_RUN", type="bool", group="Safety", label="Dry run",
         help="Simulate everything: no photo upload, no listing created, nothing deleted."),
    dict(key="BACKUP_BEFORE_REPOST", type="bool", group="Safety",
         label="Back up before every repost",
         help="Always archive the item to disk before touching it on Vinted."),

    # --- pacing ---
    dict(key="MANUAL_DELAY_MIN_SECONDS", type="int", group="Pacing", min=0, max=3600,
         label="Wait before a repost you click, minimum (seconds)"),
    dict(key="MANUAL_DELAY_MAX_SECONDS", type="int", group="Pacing", min=0, max=7200,
         label="Wait before a repost you click, maximum (seconds)",
         help="A random wait in this range before the repost is actually carried out, so a "
              "burst of clicks does not arrive at Vinted as a burst. The queue shows the "
              "countdown; other reposts wait their turn behind it."),
    dict(key="BACKUP_RETENTION_DAYS", type="int", group="Pacing", min=0, max=3650,
         label="Delete backups after (days)",
         help="0 keeps them forever. Backing an item up again resets its clock."),

    # --- notifications ---
    dict(key="NOTIFY_ON", type="choice", group="Notifications", label="Send a message",
         choices=["off", "failures", "all"],
         help="failures reports failed jobs and login trouble; all adds every repost."),
    dict(key="TELEGRAM_BOT_TOKEN", type="secret", group="Notifications",
         label="Telegram bot token",
         help="Create a bot with @BotFather and paste the token it gives you."),
    dict(key="TELEGRAM_CHAT_ID", type="str", group="Notifications", label="Telegram chat id",
         help="Message your bot once, then read the id from @userinfobot, or use your own."),

    # --- automatic reposting ---
    dict(key="AUTO_REPOST", type="bool", group="Automatic reposting",
         label="Repost automatically",
         help="Off means nothing happens unless you click Repost yourself."),
    dict(key="CHECK_INTERVAL", type="int", group="Automatic reposting",
         label="Check every (seconds)", min=60, max=86400,
         help="How often the scheduler looks for items to repost."),
    dict(key="MAX_REPOSTS_PER_DAY", type="int", group="Automatic reposting",
         label="Max reposts per day", min=0, max=50,
         help="Caps the scheduler. Manual clicks count towards the counter but are never blocked."),
    dict(key="MIN_ITEM_AGE_DAYS", type="float", group="Automatic reposting",
         label="Only items older than (days)", min=0, max=365,
         help="Measured from the last repost, or the original upload if never reposted."),
    dict(key="MIN_DELAY_SECONDS", type="int", group="Automatic reposting",
         label="Pause between reposts, minimum (seconds)", min=0, max=7200),
    dict(key="MAX_DELAY_SECONDS", type="int", group="Automatic reposting",
         label="Pause between reposts, maximum (seconds)", min=0, max=14400,
         help="A random pause in this range is taken between two automatic reposts."),
    dict(key="ACTIVE_HOURS", type="time_range", group="Automatic reposting",
         label="Active hours", help="HH:MM-HH:MM in the container's timezone. May wrap past midnight."),
    dict(key="INCLUDE_ITEM_IDS", type="ids", group="Automatic reposting",
         label="Only these item ids", help="Comma separated. Empty means all items."),
    dict(key="EXCLUDE_ITEM_IDS", type="ids", group="Automatic reposting",
         label="Never these item ids", help="Comma separated."),

    # --- account ---
    dict(key="LOCALE", type="str", group="Account", label="Vinted domain",
         help="For example www.vinted.nl or www.vinted.be."),
    dict(key="VINTED_USER_ID", type="str", group="Account", label="User id",
         hidden=True, help="Read from the access token unless VINTED_USER_ID is set."),
    dict(key="SELF_REFRESH", type="choice", group="Account", label="Token refresh",
         choices=["auto", "never"],
         help="auto refreshes the access token here when it is stale; never leaves that to the refresher container."),
    dict(key="USER_AGENT", type="str", group="Account", label="User agent", hidden=True,
         help="Must match the browser your datadome cookie came from -- the Sec-Ch-Ua "
              "headers are derived from it. Set with the USER_AGENT variable."),

    # --- endpoints ---
    dict(key="PHOTO_UPLOAD_PATH", type="path", group="Advanced: Vinted endpoints", hidden=True,
         label="Photo upload",
         help="Multipart photo[file] + photo[type]=item + photo[temp_uuid]. Returns the numeric id."),
    dict(key="ITEM_CREATE_PATH", type="path", group="Advanced: Vinted endpoints", hidden=True,
         label="Create listing",
         help="If a repost starts failing, capture the request that fires when you press "
              "Publish on vinted.nl and paste its path here."),
    dict(key="ITEM_DELETE_PATH", type="path", group="Advanced: Vinted endpoints", hidden=True,
         label="Delete listing", help="Use {id} where the item id goes."),
]

# Settings marked hidden keep working and keep their stored value, but stay out
# of the form: they are set once, if ever, and only clutter it. The web login is
# deliberately not here at all -- WEB_USER/WEB_PASSWORD come from the
# environment, so the password never lands in a settings file.

BY_KEY = {s["key"]: s for s in SCHEMA}

# Set by Docker, shown for reference, not editable while running.
ENVIRONMENT_KEYS = ["WEB_ENABLED", "WEB_HOST", "WEB_PORT", "WEB_USER",
                    "STATE_PATH", "TOKEN_PATH", "HISTORY_PATH", "BACKUP_DIR"]


class SettingsError(ValueError):
    pass


# ---- coercion ------------------------------------------------------------

def _coerce(spec, value):
    kind, key = spec["type"], spec["key"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")

    if kind in ("int", "float"):
        try:
            number = int(value) if kind == "int" else float(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{spec['label']}: '{value}' is not a number.")
        if "min" in spec and number < spec["min"]:
            raise SettingsError(f"{spec['label']}: must be at least {spec['min']}.")
        if "max" in spec and number > spec["max"]:
            raise SettingsError(f"{spec['label']}: must be at most {spec['max']}.")
        return number

    if kind == "choice":
        text = str(value).strip().lower()
        if text not in spec["choices"]:
            raise SettingsError(f"{spec['label']}: pick one of {', '.join(spec['choices'])}.")
        return text

    if kind == "time_range":
        text = str(value).strip()
        try:
            start, end = text.split("-")
            for part in (start, end):
                hours, minutes = (int(x) for x in part.split(":"))
                if not (0 <= hours < 24 and 0 <= minutes < 60):
                    raise ValueError
        except ValueError:
            raise SettingsError(f"{spec['label']}: use HH:MM-HH:MM, for example 09:00-22:00.")
        return text

    if kind == "ids":
        if isinstance(value, (list, set, tuple)):
            parts = [str(v) for v in value]
        else:
            parts = str(value).replace(";", ",").split(",")
        ids = []
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise SettingsError(f"{spec['label']}: '{part}' is not an item id.")
            ids.append(int(part))
        return sorted(set(ids))

    if kind == "path":
        text = str(value).strip()
        if text.startswith("http"):                   # accept a pasted full URL
            text = "/" + text.split("/", 3)[-1] if text.count("/") > 2 else "/"
        if not text.startswith("/"):
            raise SettingsError(f"{spec['label']}: a path starts with /, e.g. /api/v2/items.")
        if key == "ITEM_DELETE_PATH" and "{id}" not in text:
            raise SettingsError("Delete listing: the path must contain {id}.")
        return text.split("?")[0]                     # query strings are not ours to keep

    if key == "LOCALE":
        text = str(value).strip().strip("/")
        if not text or "/" in text or " " in text:
            raise SettingsError("Vinted domain: use a bare hostname, e.g. www.vinted.nl.")
        return text

    return str(value).strip()


def _to_config(spec, value):
    """settings.json stores ids as a list; config wants the set the code uses."""
    return set(value) if spec["type"] == "ids" else value


# ---- storage -------------------------------------------------------------

def _snapshot():
    out = {}
    for spec in SCHEMA:
        value = getattr(config, spec["key"])
        out[spec["key"]] = sorted(value) if spec["type"] == "ids" else value
    return out


# Taken once, at import, while config still holds exactly what the environment
# said. apply() overwrites those attributes afterwards, so this is the only
# place the docker-compose values survive -- Reset needs them.
ENV_DEFAULTS = _snapshot()


def _defaults():
    return dict(ENV_DEFAULTS)


def _read_file():
    if not os.path.exists(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            stored = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARN: {SETTINGS_PATH} unreadable ({e}); falling back to the environment.")
        return {}
    return {k: v for k, v in stored.items() if k in BY_KEY}


def _write_file(values):
    """
    Store only what differs from the environment.

    Writing the full set would silently freeze every setting at its first-boot
    value: change something in docker-compose afterwards and it would never take
    effect, not even for settings you never touched here. With only the
    deviations on disk, the compose keeps governing the rest -- and a setting put
    back to its compose value simply disappears from the file again.
    """
    delta = {k: v for k, v in values.items() if v != ENV_DEFAULTS.get(k)}
    if not delta:
        if os.path.exists(SETTINGS_PATH):
            os.remove(SETTINGS_PATH)
        return
    os.makedirs(os.path.dirname(SETTINGS_PATH) or ".", exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(delta, f, indent=2, sort_keys=True)
    os.replace(tmp, SETTINGS_PATH)
    os.chmod(SETTINGS_PATH, 0o600)


def current():
    """Effective settings: the stored file on top of the environment defaults."""
    values = _defaults()
    for key, raw in _read_file().items():
        try:
            values[key] = _coerce(BY_KEY[key], raw)
        except SettingsError as e:
            log(f"WARN: ignoring stored setting {key}: {e}")
    return values


def apply(values=None):
    """Push the effective settings onto config, where the rest of the code reads them."""
    values = values if values is not None else current()
    for key, value in values.items():
        setattr(config, key, _to_config(BY_KEY[key], value))
    return values


def load():
    values = apply()
    stored = _read_file()
    if any(_coerce(BY_KEY[k], v) == ENV_DEFAULTS.get(k) for k, v in stored.items()):
        _write_file(values)          # drop entries that match the environment again
        stored = _read_file()
    if stored:
        log(f"Settings loaded: {len(stored)} deviating from docker-compose "
            f"({', '.join(sorted(stored))}).")
    else:
        log("No stored settings; everything follows docker-compose.")
    return values


def update(incoming):
    """
    Validate and store the given subset of settings. Nothing is written unless
    every value passes, so a typo cannot leave you half-configured.
    """
    values = current()
    proposed = dict(values)
    changed = []
    for key, raw in (incoming or {}).items():
        spec = BY_KEY.get(key)
        if not spec:
            continue
        if spec["type"] == "secret" and not str(raw).strip():
            continue                      # empty means "keep what is stored"
        value = _coerce(spec, raw)
        if value != proposed[key]:
            proposed[key] = value
            changed.append(key)

    if proposed["MAX_DELAY_SECONDS"] < proposed["MIN_DELAY_SECONDS"]:
        raise SettingsError("The maximum pause cannot be shorter than the minimum pause.")
    if proposed["MANUAL_DELAY_MAX_SECONDS"] < proposed["MANUAL_DELAY_MIN_SECONDS"]:
        raise SettingsError("The maximum wait cannot be shorter than the minimum wait.")
    if proposed["NOTIFY_ON"] != "off" and not (proposed["TELEGRAM_BOT_TOKEN"]
                                               and proposed["TELEGRAM_CHAT_ID"]):
        raise SettingsError("Set both a Telegram bot token and a chat id, or put "
                            "notifications on 'off'.")

    if changed:
        _write_file(proposed)
        apply(proposed)
        log(f"Settings changed: {', '.join(changed)}.")
    return changed


def reset():
    """Throw the file away and fall back to the environment."""
    if os.path.exists(SETTINGS_PATH):
        os.remove(SETTINGS_PATH)
    log("Settings reset to the values from docker-compose.")
    return apply()


# ---- for the UI ----------------------------------------------------------

def as_dict():
    values = current()
    fields = []
    for spec in SCHEMA:
        if spec.get("hidden"):
            continue
        field = {k: v for k, v in spec.items() if k != "key"}
        field["key"] = spec["key"]
        if spec["type"] == "secret":
            field["value"] = ""                     # never hand a secret back
            field["is_set"] = bool(values[spec["key"]])
        elif spec["type"] == "ids":
            field["value"] = ", ".join(str(i) for i in values[spec["key"]])
        else:
            field["value"] = values[spec["key"]]
        fields.append(field)

    # everything the UI does not offer to edit, so it is at least visible
    reference = {k: getattr(config, k) for k in ENVIRONMENT_KEYS}
    reference.update({s["key"]: values[s["key"]] for s in SCHEMA if s.get("hidden")})
    reference["WEB_PASSWORD"] = "set" if config.WEB_PASSWORD else "not set"

    return {
        "fields": fields,
        "groups": list(dict.fromkeys(f["group"] for f in fields)),
        "environment": reference,
        "settings_path": SETTINGS_PATH,
        "stored": os.path.exists(SETTINGS_PATH),
    }
