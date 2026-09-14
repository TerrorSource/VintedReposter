"""
Job queue and the actual Vinted actions.

Every write to Vinted goes through ONE worker thread, so a manual repost from the
web UI can never overlap with the scheduler or with another click. Jobs are
queued and their status is polled by the UI.
"""
import os
import queue
import random
import threading
import time
from datetime import datetime, timezone

import auth
import config
import notify
import store
import vinted_api as api
from store import log

MAX_JOBS = 200                # how much history the UI keeps

# Set by app.py. A repost replaces a listing with a new id, so any cached
# wardrobe is wrong the moment one succeeds -- and acting on a stale list means
# queueing reposts for listings that no longer exist.
on_wardrobe_changed = None

_JOB_Q = queue.Queue()
_JOBS = {}                    # job_id -> job dict
_JOB_ORDER = []               # newest last
_LOCK = threading.Lock()
_next_id = 1


# ---- client --------------------------------------------------------------

def get_client():
    """Fresh client per job: the token may have rotated in the meantime."""
    token = auth.ensure_token()
    return api.VintedClient(
        locale=config.LOCALE,
        access_token=token["access_token"],
        user_agent=config.USER_AGENT,
        datadome=token.get("datadome"),
        cf_clearance=token.get("cf_clearance"),
        on_cookies=auth.remember_cookies,
    )


def token_info():
    """Status only -- never refreshes, so polling the UI cannot rotate anything."""
    token = auth.load_token()
    if not token:
        has_creds = bool(auth.load_state().get("refresh_token_web"))
        return {"ok": False, "can_refresh": has_creds,
                "error": ("no access token yet -- press Refresh now under Settings"
                          if has_creds else
                          "no access token and no credentials stored -- paste your "
                          "browser cookies under Settings")}
    expires = token.get("expires_at")
    return {
        "ok": True,
        "can_refresh": bool(auth.load_state().get("refresh_token_web")),
        "fresh": auth.token_is_fresh(token),
        "expires_at": expires,
        "expires_in": int(expires - time.time()) if expires else None,
        "refresh_expires_in": auth.refresh_token_expires_in(),
    }


# ---- jobs ----------------------------------------------------------------

def enqueue(kind, item_id=None, title=None, price=None):
    global _next_id
    with _LOCK:
        job = {
            "id": _next_id,
            "kind": kind,
            "item_id": item_id,
            "title": title,
            "price": price,          # only set when a repost changes the price
            "status": "queued",
            "message": "",
            "created": int(time.time()),
            "finished": None,
        }
        _next_id += 1
        _JOBS[job["id"]] = job
        _JOB_ORDER.append(job["id"])
        for evicted in _JOB_ORDER[:-MAX_JOBS]:      # the dict has to shrink along
            _JOBS.pop(evicted, None)
        del _JOB_ORDER[:-MAX_JOBS]
    _JOB_Q.put(job["id"])
    return job


def jobs(n=50):
    with _LOCK:
        return [_JOBS[i] for i in _JOB_ORDER[-n:]][::-1]


def _set(job, **kw):
    with _LOCK:
        job.update(kw)


def _hold_back(job):
    """
    Space out the writes you trigger by hand. Clicking Repost on five items is
    five clicks in five seconds; arriving at Vinted that way is exactly the
    pattern their bot detection looks for. The job sits in the queue with a
    visible countdown instead.

    Called just before the first write, never at the start of the job: reading
    the item and backing it up cost Vinted nothing, and a listing that turns out
    to be gone or sold should say so at once instead of after a minute of
    waiting for a write that will not happen.
    """
    if job["kind"] not in ("repost", "restore") or config.DRY_RUN:
        return
    delay = random.uniform(config.MANUAL_DELAY_MIN_SECONDS, config.MANUAL_DELAY_MAX_SECONDS)
    if delay <= 0:
        return
    _set(job, status="waiting", starts_at=int(time.time() + delay))
    log(f"Holding {job['kind']} of #{job['item_id']} for {int(delay)}s before carrying it out.")
    time.sleep(delay)


def _run_job(job):
    kind = job["kind"]
    _set(job, status="running")
    hold = lambda: _hold_back(job)          # noqa: E731 -- called just before the first write
    try:
        client = get_client()
        if kind == "backup":
            meta = do_backup(client, job["item_id"])
            _set(job, message=f"{len(meta['photos'])} photos backed up")
        elif kind in ("repost", "auto_repost"):
            new_id = do_repost(client, job["item_id"], job.get("price"), hold=hold)
            _set(job, message=f"new listing #{new_id}" if new_id else "dry run only")
        elif kind == "restore":
            new_id = do_restore(client, job["item_id"], hold=hold)
            _set(job, message=f"restored as #{new_id}" if new_id else "dry run only")
        else:
            raise api.VintedError(f"unknown job kind {kind}")
        _set(job, status="done", finished=int(time.time()))
    except Exception as e:
        log(f"JOB {kind} #{job['item_id']} FAILED: {e}")
        _set(job, status="error", message=str(e), finished=int(time.time()))
        if isinstance(e, auth.AuthError) or "401" in str(e) or "CAPTCHA" in str(e):
            notify.token_problem(str(e))
        else:
            notify.job_failed(kind, job["item_id"], job["title"], str(e))


def _worker():
    while True:
        job_id = _JOB_Q.get()
        job = _JOBS.get(job_id)
        if job:
            _run_job(job)
            # pace automatic reposts; manual clicks stay responsive
            if job["kind"] == "auto_repost" and not config.DRY_RUN:
                delay = random.uniform(config.MIN_DELAY_SECONDS, config.MAX_DELAY_SECONDS)
                log(f"Pausing {delay / 60:.1f} min before the next automatic repost.")
                time.sleep(delay)
        _JOB_Q.task_done()


# ---- actions -------------------------------------------------------------

def _user_id(client):
    return int(config.VINTED_USER_ID) if config.VINTED_USER_ID else client.current_user_id()


def item_for_backup(client, item_id):
    """
    Returns (item, partial). The editor endpoint 404s for listings Vinted will
    not let you edit any more -- sold or otherwise closed. Those can still be
    archived from the wardrobe data: photos, title, price, size, brand name.
    What is missing is the description and the numeric ids, so such a backup
    cannot be used to re-create the listing; it is flagged partial.
    """
    try:
        return client.get_item(item_id), False
    except api.VintedError as e:
        if "404" not in str(e):
            raise
    listing = next((l for l in client.list_wardrobe(_user_id(client))
                    if l.get("id") == item_id), None)
    if not listing:
        raise api.VintedError(
            f"#{item_id} is not in your wardrobe -- already deleted?")
    return listing, True


def do_backup(client, item_id):
    detail, partial = item_for_backup(client, item_id)
    meta = store.save_backup(client, detail, partial=partial)
    note = " (partial: sold or closed, photos and basics only)" if partial else ""
    log(f"Backed up #{item_id} '{meta['title']}' ({len(meta['photos'])} photos){note}.")
    return meta


def _photo_blobs(client, detail):
    """
    Use the backed-up files, but only while they still are the item's photos.
    A backup taken before the photos were changed on Vinted would otherwise put
    the old pictures back up.
    """
    paths = store.backup_photos(detail["id"])
    meta = store.read_meta(detail["id"]) or {}
    if paths and meta.get("photo_keys") == api.photo_keys(detail):
        for p in paths:
            with open(p, "rb") as f:
                yield f.read()
        return
    for url in api.photo_urls(detail):
        yield client.download_photo(url)


def price_change(detail, wanted):
    """
    The price this repost should carry, and a note for the log. `wanted` is what
    was asked for when the repost was started -- reposting is per item, so the
    price is decided there and not in the settings.
    """
    if wanted is None:
        return None, ""
    amount, _ = api.price_amount(detail)
    try:
        current, wanted = float(amount), float(wanted)
    except (TypeError, ValueError):
        return None, ""
    if wanted <= 0 or abs(wanted - current) < 0.005:
        return None, ""
    return round(wanted, 2), f" (price {current:.2f} -> {wanted:.2f})"


def do_repost(client, item_id, wanted_price=None, hold=None):
    try:
        detail = client.get_item(item_id)
    except api.VintedError as e:
        if "404" not in str(e):
            raise
        # 404 covers two very different situations; say which one it is
        in_wardrobe = any(l.get("id") == item_id
                          for l in client.list_wardrobe(_user_id(client)))
        raise api.VintedError(
            f"#{item_id} can no longer be edited on Vinted -- sold or closed "
            f"listings cannot be reposted."
            if in_wardrobe else
            f"#{item_id} is not in your wardrobe any more -- it was probably "
            f"reposted already. Reload the list to see the current ids.")
    title = detail.get("title", "?")

    if config.BACKUP_BEFORE_REPOST:
        store.save_backup(client, detail)

    if not api.photo_urls(detail) and not store.backup_photos(item_id):
        raise api.VintedError("no photos found for this item")

    new_price, price_note = price_change(detail, wanted_price)

    if config.DRY_RUN:
        log(f"DRY RUN: would repost #{item_id} '{title}'{price_note} "
            f"and delete the original.")
        return None

    if hold:
        hold()                       # everything above was read-only; from here we write

    temp_uuid = api.new_temp_uuid()
    photo_ids = []
    for i, blob in enumerate(_photo_blobs(client, detail), 1):
        photo_ids.append(client.upload_photo(blob, temp_uuid, filename=f"photo{i}.jpg"))
        time.sleep(random.uniform(1.0, 3.0))
    log(f"#{item_id}: {len(photo_ids)} photos re-uploaded.")

    new_id = client.create_item(detail, photo_ids, temp_uuid, price=new_price)
    log(f"#{item_id} '{title}'{price_note} -> new listing #{new_id}.")

    client.delete_item(item_id)                 # only after the new one exists
    store.record_repost(item_id, new_id, title=title)
    log(f"#{item_id} deleted. Repost complete.")
    notify.repost_done(item_id, title, new_id, price_note)
    if on_wardrobe_changed:
        on_wardrobe_changed()
    return new_id


def do_restore(client, item_id, hold=None):
    """Re-create a listing from a backup. Deletes nothing."""
    detail = store.load_backup(item_id)
    if not detail:
        raise api.VintedError(f"no backup found for #{item_id}")
    if (store.read_meta(item_id) or {}).get("partial"):
        raise api.VintedError(
            f"the backup of #{item_id} is partial -- it was taken from a sold or "
            f"closed listing, so it has no description or category and cannot be "
            f"re-listed. The photos are in the download.")
    paths = store.backup_photos(item_id)
    if not paths:
        raise api.VintedError(f"backup of #{item_id} has no photos")

    if config.DRY_RUN:
        log(f"DRY RUN: would restore #{item_id} '{detail.get('title')}' from backup.")
        return None

    if hold:
        hold()

    temp_uuid = api.new_temp_uuid()
    photo_ids = []
    for i, p in enumerate(paths, 1):
        with open(p, "rb") as f:
            photo_ids.append(client.upload_photo(f.read(), temp_uuid, filename=f"photo{i}.jpg"))
        time.sleep(random.uniform(1.0, 3.0))

    new_id = client.create_item(detail, photo_ids, temp_uuid)
    log(f"Restored backup of #{item_id} as new listing #{new_id}.")
    return new_id


# ---- wardrobe view -------------------------------------------------------

def _listing_ts(listing):
    """
    Vinted's wardrobe response carries no creation date, so the upload time of
    the first photo stands in for it -- it is set when the listing is created.
    """
    ts = api.photo_timestamp(listing)
    if ts:
        return ts
    raw = listing.get("created_at_ts") or listing.get("created_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    if isinstance(raw, (int, float)):
        return raw
    return None


def decorate(items):
    """
    Refresh the fields that change without Vinted being involved, so a cached
    wardrobe never shows a stale backup badge or a stale age.
    """
    for it in items:
        it["has_backup"] = store.has_backup(it["id"])
        it["excluded"] = it["id"] in config.EXCLUDE_ITEM_IDS
        ts = store.last_repost_ts(it["id"]) or it.get("ts")
        it["age_days"] = round((time.time() - ts) / 86400, 1) if ts else None
    return items


def is_sellable(listing):
    if listing.get("is_closed") or listing.get("is_hidden") or listing.get("is_reserved"):
        return False
    if listing.get("is_draft") or listing.get("item_closing_action"):
        return False
    return listing.get("is_visible", 1) != 0


def wardrobe(client=None):
    """Normalised wardrobe for the UI. One API call per 100 items, no details."""
    client = client or get_client()
    user_id = int(config.VINTED_USER_ID) if config.VINTED_USER_ID else client.current_user_id()
    out = []
    for listing in client.list_wardrobe(user_id):
        item_id = listing.get("id")
        if not item_id:
            continue
        out.append({
            "id": item_id,
            "title": listing.get("title") or "(no title)",
            "price": store._price_str(listing),
            "price_num": store._price_number(listing),
            "thumb": api.thumbnail_url(listing),
            "url": listing.get("url") or f"https://{config.LOCALE}/items/{item_id}",
            "ts": _listing_ts(listing),
            "sellable": is_sellable(listing),
            "can_edit": bool(listing.get("can_edit", True)),
            "excluded": item_id in config.EXCLUDE_ITEM_IDS,
            "closing_action": listing.get("item_closing_action"),
            "favourites": listing.get("favourite_count"),
            "views": listing.get("view_count"),
        })
    return {"user_id": user_id, "items": decorate(out)}


# ---- scheduler -----------------------------------------------------------

def within_active_hours():
    try:
        start, end = config.ACTIVE_HOURS.split("-")
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except ValueError:
        log(f"WARN: ACTIVE_HOURS '{config.ACTIVE_HOURS}' is malformed; treating as always active.")
        return True
    now = datetime.now()
    mins = now.hour * 60 + now.minute
    lo, hi = sh * 60 + sm, eh * 60 + em
    return lo <= mins < hi if lo <= hi else (mins >= lo or mins < hi)


def auto_candidates(client=None):
    """Items the scheduler would repost, oldest first."""
    data = wardrobe(client)
    picked = []
    for it in data["items"]:
        if not it["sellable"] or it["age_days"] is None:
            continue
        if it["id"] in config.EXCLUDE_ITEM_IDS:
            continue
        if config.INCLUDE_ITEM_IDS and it["id"] not in config.INCLUDE_ITEM_IDS:
            continue
        if it["age_days"] < config.MIN_ITEM_AGE_DAYS:
            continue
        picked.append(it)
    picked.sort(key=lambda i: i["age_days"], reverse=True)
    return picked


SCHEDULER_TICK = 15          # short, so a change in Settings takes effect promptly


def _scan():
    budget = config.MAX_REPOSTS_PER_DAY - store.reposts_today()
    if budget <= 0:
        log(f"Daily limit reached ({config.MAX_REPOSTS_PER_DAY}).")
        return
    picked = auto_candidates()[:budget]
    if not picked:
        log(f"No items older than {config.MIN_ITEM_AGE_DAYS} days to repost.")
        return
    for it in picked:
        enqueue("auto_repost", it["id"], it["title"])
        log(f"Queued automatic repost of #{it['id']} '{it['title']}' ({it['age_days']}d old).")


def _scheduler():
    """
    Always running, but idle unless automatic reposting is switched on. It wakes
    every few seconds so a change in Settings takes effect right away instead of
    at the end of a long sleep, and only scans once per CHECK_INTERVAL.
    """
    last_scan = 0.0
    last_prune = 0.0
    last_state = None
    while True:
        try:
            if time.time() - last_prune >= 86400:      # housekeeping, once a day
                last_prune = time.time()
                store.prune_backups(config.BACKUP_RETENTION_DAYS)

            state = (config.AUTO_REPOST, within_active_hours())
            if state != last_state:
                if not state[0]:
                    log("Automatic reposting is off; reposts only happen when you click.")
                elif state[1]:
                    log(f"Automatic reposting on: max {config.MAX_REPOSTS_PER_DAY}/day, "
                        f"items older than {config.MIN_ITEM_AGE_DAYS}d, active {config.ACTIVE_HOURS}.")
                else:
                    log(f"Automatic reposting on, but outside active hours ({config.ACTIVE_HOURS}).")
                last_state = state
            if all(state) and time.time() - last_scan >= config.CHECK_INTERVAL:
                last_scan = time.time()
                _scan()
        except Exception as e:
            log(f"Scheduler error: {e}")
        time.sleep(SCHEDULER_TICK)


def start():
    threading.Thread(target=_worker, daemon=True, name="vinted-worker").start()
    threading.Thread(target=_scheduler, daemon=True, name="vinted-scheduler").start()
