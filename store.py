"""Persistence: the activity log, the repost history and the on-disk backups."""
import json
import os
import shutil
import time
from collections import deque

import config
import vinted_api as api

# In-memory tail of the activity log; the web UI polls it, Docker gets stdout.
LOG = deque(maxlen=500)


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


def log_tail(n=200):
    return list(LOG)[-n:]


def _atomic_write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---- repost history ------------------------------------------------------

def load_history():
    if not os.path.exists(config.HISTORY_PATH):
        return {"reposts": {}, "daily": {}}
    try:
        with open(config.HISTORY_PATH) as f:
            h = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log(f"WARN: history unreadable ({e}); starting a fresh one.")
        return {"reposts": {}, "daily": {}}
    h.setdefault("reposts", {})
    h.setdefault("daily", {})
    return h


def save_history(h):
    keep = set(sorted(h["daily"])[-30:])          # don't grow forever
    h["daily"] = {k: v for k, v in h["daily"].items() if k in keep}
    _atomic_write(config.HISTORY_PATH, h)


def today_key():
    return time.strftime("%Y-%m-%d")


def reposts_today(h=None):
    h = h or load_history()
    return h["daily"].get(today_key(), 0)


def record_repost(old_id, new_id, title=None):
    h = load_history()
    now = int(time.time())
    h["reposts"][str(old_id)] = {"ts": now, "new_id": new_id, "title": title}
    h["reposts"][str(new_id)] = {"ts": now, "from_id": old_id, "title": title}
    h["daily"][today_key()] = h["daily"].get(today_key(), 0) + 1
    save_history(h)
    # keep the backup discoverable under the new id as well
    meta = read_meta(old_id)
    if meta:
        meta["reposted_to"] = new_id
        meta["reposted_at"] = now
        _atomic_write(os.path.join(_dir(old_id), "meta.json"), meta)


def repost_chain():
    """
    Every repost that happened, newest first: which listing replaced which, when.
    Only the entries keyed on the original are real events; the mirror entry
    under the new id exists so the age of the new listing is known too.
    """
    out = []
    for item_id, entry in load_history()["reposts"].items():
        if not entry.get("new_id"):
            continue
        title = entry.get("title") or (read_meta(int(item_id)) or {}).get("title")
        out.append({
            "ts": entry.get("ts"),
            "from_id": int(item_id),
            "new_id": entry["new_id"],
            "title": title or f"#{item_id}",
            "url": f"https://{config.LOCALE}/items/{entry['new_id']}",
            "has_backup": has_backup(int(item_id)),
        })
    out.sort(key=lambda e: e["ts"] or 0, reverse=True)
    return out


def prune_backups(max_age_days):
    """
    Drop archives older than the retention window. Backing an item up again
    refreshes its timestamp, so anything you still hold in your wardrobe keeps
    itself alive as long as you keep backing it up.
    """
    if not max_age_days:
        return []
    cutoff = time.time() - max_age_days * 86400
    removed = []
    for meta in list_backups():
        if (meta.get("backed_up_at") or 0) < cutoff:
            try:
                delete_backup(meta["item_id"])
                removed.append(meta["item_id"])
            except (ValueError, OSError) as e:
                log(f"WARN: could not prune the backup of #{meta['item_id']}: {e}")
    if removed:
        log(f"Pruned {len(removed)} backup(s) older than {max_age_days} days.")
    return removed


def last_repost_ts(item_id):
    entry = load_history()["reposts"].get(str(item_id))
    return entry.get("ts") if entry else None


# ---- backups -------------------------------------------------------------

def _dir(item_id):
    return os.path.join(config.BACKUP_DIR, str(item_id))


def has_backup(item_id):
    return os.path.exists(os.path.join(_dir(item_id), "item.json"))


def read_meta(item_id):
    path = os.path.join(_dir(item_id), "meta.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def save_backup(client, detail, partial=False):
    """
    Store the item's full JSON plus its original photos on disk.

    partial=True means the item came from the wardrobe listing instead of the
    editor, because Vinted no longer exposes it for editing (sold/closed). The
    photos are complete; the description and the numeric ids are not, so such a
    backup can be kept and downloaded but never re-listed.

    Photos are only re-fetched when they actually changed. The check is on
    Vinted's own photo identities, not on the file names: edit the photos of a
    listing and the numbering stays 001, 002, ... while the content is different,
    so comparing names alone would happily keep serving the old pictures to the
    next repost.
    """
    item_id = detail["id"]
    d = _dir(item_id)
    photo_dir = os.path.join(d, "photos")
    os.makedirs(photo_dir, exist_ok=True)

    urls = api.photo_urls(detail)
    keys = api.photo_keys(detail)
    if (read_meta(item_id) or {}).get("photo_keys") != keys:
        # the photo set moved: start clean rather than mix old files with new
        for name in os.listdir(photo_dir):
            os.remove(os.path.join(photo_dir, name))

    saved = []
    for i, url in enumerate(urls, 1):
        name = f"{i:03d}.jpg"
        path = os.path.join(photo_dir, name)
        if not os.path.exists(path):
            with open(path + ".tmp", "wb") as f:
                f.write(client.download_photo(url))
            os.replace(path + ".tmp", path)
        saved.append(name)

    _atomic_write(os.path.join(d, "item.json"), detail)
    meta = {
        "item_id": item_id,
        "title": detail.get("title"),
        "price": _price_str(detail),
        "photos": saved,
        "photo_keys": keys,
        "backed_up_at": int(time.time()),
        "url": detail.get("url"),
        "partial": bool(partial),
        # Vinted's own thumbnail, so the overview does not pull megabytes of
        # full-size photos off disk just to draw 64-pixel squares.
        "thumb": api.thumbnail_url(detail),
    }
    old = read_meta(item_id) or {}
    for k in ("reposted_to", "reposted_at"):          # never lose the repost link
        if k in old:
            meta[k] = old[k]
    _atomic_write(os.path.join(d, "meta.json"), meta)
    return meta


def load_backup(item_id):
    path = os.path.join(_dir(item_id), "item.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def backup_photos(item_id):
    """Absolute paths of the backed-up photos, in order."""
    photo_dir = os.path.join(_dir(item_id), "photos")
    if not os.path.isdir(photo_dir):
        return []
    return [os.path.join(photo_dir, n) for n in sorted(os.listdir(photo_dir))
            if n.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]


def delete_backup(item_id):
    """Remove one backup folder. Touches nothing on Vinted."""
    target = os.path.realpath(_dir(item_id))
    root = os.path.realpath(config.BACKUP_DIR)
    if os.path.dirname(target) != root or not os.path.basename(target).isdigit():
        raise ValueError(f"refusing to delete {target}: not a backup folder")
    if not os.path.isdir(target):
        return False
    shutil.rmtree(target)
    log(f"Deleted the backup of #{item_id}.")
    return True


def list_backups():
    if not os.path.isdir(config.BACKUP_DIR):
        return []
    out = []
    for name in os.listdir(config.BACKUP_DIR):
        if not name.isdigit():
            continue
        meta = read_meta(int(name))
        if meta:
            meta.setdefault("partial", False)     # backups written before the flag existed
            out.append(meta)
    out.sort(key=lambda m: m.get("backed_up_at", 0), reverse=True)
    return out


def _price_str(detail):
    price = detail.get("price")
    if isinstance(price, dict):
        return f"{price.get('amount')} {price.get('currency_code', '')}".strip()
    return str(price) if price is not None else ""


def _price_number(detail):
    """The bare amount, so the UI can sort on it."""
    price = detail.get("price")
    if isinstance(price, dict):
        price = price.get("amount")
    try:
        return float(price)
    except (TypeError, ValueError):
        return None
