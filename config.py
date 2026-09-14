"""All configuration in one place. Everything comes from the environment."""
import os


def _bool(name, default):
    return os.environ.get(name, str(default)).lower() not in ("0", "false", "no", "")


def _ids(name):
    raw = os.environ.get(name, "").replace(";", ",")
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


# --- paths ---
TOKEN_PATH = os.environ.get("TOKEN_PATH", "/state/token.json")
STATE_PATH = os.environ.get("STATE_PATH", "/state/state.json")
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/state/repost_history.json")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")

# --- account / fingerprint ---
LOCALE = os.environ.get("VINTED_LOCALE", "www.vinted.nl")
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
VINTED_USER_ID = os.environ.get("VINTED_USER_ID", "").strip()

# --- endpoints ---
# All confirmed against a live account, but overridable so a change on Vinted's
# side is a field in the UI rather than a new image.
PHOTO_UPLOAD_PATH = os.environ.get("PHOTO_UPLOAD_PATH", "/api/v2/photos")
ITEM_CREATE_PATH = os.environ.get("ITEM_CREATE_PATH", "/api/v2/item_upload/items")
ITEM_DELETE_PATH = os.environ.get("ITEM_DELETE_PATH", "/api/v2/items/{id}")

# --- token handling ---
# auto  = refresh only when token.json is missing or nearly expired (safe next to
#         the vinted-token-refresher container, which normally keeps it fresh)
# never = never refresh here; something else owns the token
SELF_REFRESH = os.environ.get("SELF_REFRESH", "auto").strip().lower()

# --- safety ---
DRY_RUN = _bool("DRY_RUN", True)
BACKUP_BEFORE_REPOST = _bool("BACKUP_BEFORE_REPOST", True)
# Back up every listing while it is still for sale, so a sold one can be
# re-listed from that copy later (Vinted hides the description of sold items).
AUTO_BACKUP = _bool("AUTO_BACKUP", True)

# --- pacing of the actions you click yourself ---
MANUAL_DELAY_MIN_SECONDS = int(os.environ.get("MANUAL_DELAY_MIN_SECONDS", "30"))
MANUAL_DELAY_MAX_SECONDS = int(os.environ.get("MANUAL_DELAY_MAX_SECONDS", "90"))

# --- housekeeping ---
BACKUP_RETENTION_DAYS = int(os.environ.get("BACKUP_RETENTION_DAYS", "30"))

# --- notifications ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NOTIFY_ON = os.environ.get("NOTIFY_ON", "off").strip().lower()

# --- automatic reposting (off by default: the web UI is the point) ---
AUTO_REPOST = _bool("AUTO_REPOST", False)
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "3600"))
MAX_REPOSTS_PER_DAY = int(os.environ.get("MAX_REPOSTS_PER_DAY", "3"))
MIN_ITEM_AGE_DAYS = float(os.environ.get("MIN_ITEM_AGE_DAYS", "30"))
MIN_DELAY_SECONDS = int(os.environ.get("MIN_DELAY_SECONDS", "120"))
MAX_DELAY_SECONDS = int(os.environ.get("MAX_DELAY_SECONDS", "600"))
ACTIVE_HOURS = os.environ.get("ACTIVE_HOURS", "09:00-22:00")
INCLUDE_ITEM_IDS = _ids("INCLUDE_ITEM_IDS")
EXCLUDE_ITEM_IDS = _ids("EXCLUDE_ITEM_IDS")

# --- web ---
WEB_ENABLED = _bool("WEB_ENABLED", True)
WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_USER = os.environ.get("WEB_USER", "").strip()
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "").strip()
