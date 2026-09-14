#!/usr/bin/env python3
"""
Web UI + JSON API for the Vinted reposter.

Start this (it is the container's default command) and open http://host:8080.
The scheduler only runs when AUTO_REPOST=true; otherwise everything is manual.
"""
import io
import os
import threading
import time
import zipfile

from flask import Flask, Response, jsonify, render_template, request, send_file

import auth
import config
import notify
import settings
import store
import vinted_api as api
import worker
from store import log

UPSTREAM_ERRORS = (api.VintedError, auth.AuthError)

app = Flask(__name__)

_cache = {"data": None, "ts": 0}
CACHE_TTL = 120


def _invalidate_cache():
    _cache.update(data=None, ts=0)


worker.on_wardrobe_changed = _invalidate_cache


# ---- auth ----------------------------------------------------------------

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.before_request
def guard():
    if request.path == "/api/health":            # Docker probes it without a login
        return None
    if request.method in MUTATING and request.path.startswith("/api/") \
            and request.headers.get("X-Requested-With") != "vinted-reposter":
        # A browser attaches cached basic-auth to any cross-site POST a page
        # tricks it into. A custom header it will not attach on its own, and a
        # page that tries is stopped by the CORS preflight instead.
        return jsonify({"error": "missing X-Requested-With: vinted-reposter"}), 403
    if not config.WEB_PASSWORD:
        return None
    auth = request.authorization
    if auth and auth.username == (config.WEB_USER or auth.username) \
            and auth.password == config.WEB_PASSWORD:
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": 'Basic realm="Vinted Reposter"'})


# ---- pages ---------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


# ---- status --------------------------------------------------------------

@app.get("/api/status")
def api_status():
    return jsonify({
        "dry_run": config.DRY_RUN,
        "auto_repost": config.AUTO_REPOST,
        "locale": config.LOCALE,
        "max_per_day": config.MAX_REPOSTS_PER_DAY,
        "reposts_today": store.reposts_today(),
        "min_age_days": config.MIN_ITEM_AGE_DAYS,
        "active_hours": config.ACTIVE_HOURS,
        "within_active_hours": worker.within_active_hours(),
        "backup_before_repost": config.BACKUP_BEFORE_REPOST,
        "manual_delay": (f"{config.MANUAL_DELAY_MIN_SECONDS}-{config.MANUAL_DELAY_MAX_SECONDS}s"
                         if config.MANUAL_DELAY_MAX_SECONDS else None),
        "notify_on": config.NOTIFY_ON,
        "token": worker.token_info(),
    })


@app.get("/api/health")
def api_health():
    """For Docker's HEALTHCHECK: no login, just whether the threads are alive."""
    names = {t.name for t in threading.enumerate()}
    alive = "vinted-worker" in names and "vinted-scheduler" in names
    return jsonify({"ok": alive}), (200 if alive else 503)


@app.get("/api/log")
def api_log():
    return jsonify({"lines": store.log_tail(int(request.args.get("n", 200)))})


@app.get("/api/jobs")
def api_jobs():
    return jsonify({"jobs": worker.jobs()})


# ---- wardrobe ------------------------------------------------------------

@app.get("/api/items")
def api_items():
    fresh = request.args.get("refresh") == "1"
    if not fresh and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
        data = _cache["data"]
        # backup badges and ages can have changed since the wardrobe was fetched
        worker.decorate(data["items"])
        return jsonify({**data, "cached_at": _cache["ts"]})
    try:
        data = worker.wardrobe()
    except UPSTREAM_ERRORS as e:
        return jsonify({"error": str(e)}), 502
    _cache.update(data=data, ts=time.time())
    return jsonify({**data, "cached_at": _cache["ts"]})


@app.post("/api/items/<int:item_id>/backup")
def api_backup(item_id):
    return jsonify(worker.enqueue("backup", item_id, request.json.get("title") if request.is_json else None))


@app.post("/api/items/<int:item_id>/repost")
def api_repost(item_id):
    body = request.get_json(silent=True) or {}
    price = body.get("price")
    if price not in (None, ""):
        try:
            price = round(float(price), 2)
        except (TypeError, ValueError):
            return jsonify({"error": f"'{price}' is not a price"}), 400
        if price <= 0:
            return jsonify({"error": "a price has to be above zero"}), 400
    else:
        price = None
    return jsonify(worker.enqueue("repost", item_id, body.get("title"), price=price))


@app.post("/api/items/<int:item_id>/exclude")
def api_exclude(item_id):
    """Add or remove this item from the scheduler's blacklist."""
    wanted = bool((request.get_json(silent=True) or {}).get("excluded", True))
    ids = set(config.EXCLUDE_ITEM_IDS)
    ids.add(item_id) if wanted else ids.discard(item_id)
    settings.update({"EXCLUDE_ITEM_IDS": sorted(ids)})
    return jsonify({"excluded": wanted, "count": len(ids)})


# ---- history ---------------------------------------------------------------

@app.get("/api/history")
def api_history():
    return jsonify({"reposts": store.repost_chain(),
                    "today": store.reposts_today(),
                    "locale": config.LOCALE})


# ---- settings ------------------------------------------------------------

@app.get("/api/settings")
def api_settings():
    return jsonify(settings.as_dict())


@app.post("/api/settings")
def api_save_settings():
    try:
        changed = settings.update(request.get_json(silent=True) or {})
    except settings.SettingsError as e:
        return jsonify({"error": str(e)}), 400
    _cache.update(data=None, ts=0)                 # locale or user id may have moved
    return jsonify({"changed": changed})


@app.post("/api/settings/test-notification")
def api_test_notification():
    problem = notify.send("🔔 <b>Vinted reposter</b>\nTest message — notifications work.")
    if problem:
        return jsonify({"error": problem}), 400
    return jsonify({"ok": True})


@app.post("/api/settings/reset")
def api_reset_settings():
    settings.reset()
    _cache.update(data=None, ts=0)
    return jsonify({"ok": True})


# ---- credentials ---------------------------------------------------------

@app.get("/api/credentials")
def api_credentials():
    """Whether each credential is stored -- never the values themselves."""
    return jsonify({"credentials": auth.credentials_status(),
                    "self_refresh": config.SELF_REFRESH,
                    "state_path": config.STATE_PATH})


@app.post("/api/credentials")
def api_save_credentials():
    body = request.get_json(silent=True) or {}
    changed = auth.update_credentials(body)
    if not changed:
        return jsonify({"changed": [], "message": "Nothing to save."})
    result = {"changed": changed}
    if body.get("refresh"):
        try:
            token = auth.refresh_now()
            result["refreshed"] = True
            result["expires_in"] = int(token["expires_at"] - time.time())
        except Exception as e:
            return jsonify({**result, "refreshed": False, "error": str(e)}), 502
    return jsonify(result)


@app.post("/api/credentials/refresh")
def api_refresh_token():
    try:
        token = auth.refresh_now()
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    _cache.update(data=None, ts=0)            # a new token may see a new wardrobe
    return jsonify({"ok": True, "expires_in": int(token["expires_at"] - time.time())})


@app.post("/api/backup-all")
def api_backup_all():
    try:
        data = _cache["data"] or worker.wardrobe()
    except UPSTREAM_ERRORS as e:
        return jsonify({"error": str(e)}), 502
    # sold listings are archived too, just partially -- that is what a backup is for
    queued = [worker.enqueue("backup", it["id"], it["title"]) for it in data["items"]]
    partial = sum(1 for it in data["items"] if not it.get("can_edit", True))
    log(f"Queued {len(queued)} backup job(s), {partial} of them partial (sold/closed).")
    return jsonify({"queued": len(queued), "partial": partial})


# ---- backups -------------------------------------------------------------

@app.get("/api/backups")
def api_backups():
    return jsonify({"backups": store.list_backups()})


@app.get("/api/backups/<int:item_id>/photo/<int:n>")
def api_backup_photo(item_id, n):
    paths = store.backup_photos(item_id)
    if n < 1 or n > len(paths):
        return jsonify({"error": "no such photo"}), 404
    return send_file(paths[n - 1])


@app.get("/api/backups/<int:item_id>/download")
def api_backup_download(item_id):
    detail = store.load_backup(item_id)
    if not detail:
        return jsonify({"error": "no backup"}), 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(os.path.join(config.BACKUP_DIR, str(item_id), "item.json"),
                f"{item_id}/item.json")
        for p in store.backup_photos(item_id):
            z.write(p, f"{item_id}/photos/{os.path.basename(p)}")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"vinted-{item_id}.zip")


@app.post("/api/backups/<int:item_id>/restore")
def api_restore(item_id):
    return jsonify(worker.enqueue("restore", item_id))


@app.delete("/api/backups/<int:item_id>")
def api_backup_delete(item_id):
    """Removes the local archive only; the listing on Vinted is untouched."""
    try:
        removed = store.delete_backup(item_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not removed:
        return jsonify({"error": "no such backup"}), 404
    return jsonify({"ok": True})


def main():
    settings.load()
    log(f"Vinted reposter starting. locale={config.LOCALE} dry_run={config.DRY_RUN} "
        f"auto_repost={config.AUTO_REPOST} web={config.WEB_HOST}:{config.WEB_PORT}")
    if config.DRY_RUN:
        log("DRY_RUN is on: nothing will be uploaded, created or deleted.")
    if not config.WEB_PASSWORD:
        log("WARNING: no password set. Anyone who can reach this port can delete "
            "and re-list your items. Set WEB_USER/WEB_PASSWORD in docker-compose.")
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    worker.start()
    # A real WSGI server: Flask's built-in one is for development and says so
    # in the log on every start.
    from waitress import serve
    serve(app, host=config.WEB_HOST, port=config.WEB_PORT, threads=8, ident=None)


if __name__ == "__main__":
    main()
