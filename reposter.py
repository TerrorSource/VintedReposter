#!/usr/bin/env python3
"""
Container entrypoint.

WEB_ENABLED=true (default) serves the web UI on WEB_PORT and only reposts what
you click, unless AUTO_REPOST=true also turns the scheduler on.
WEB_ENABLED=false runs headless: scheduler only, no HTTP.
"""
import os
import time

import config
import settings
import worker
from store import log


def drop_privileges(*folders):
    """
    PUID/PGID, the way the linuxserver.io images do it: hand our own folders to
    that user, then run as that user instead of root, so the files this
    container writes belong to you on the NAS. Nothing outside those folders is
    touched. Without PUID the container keeps running as root, as before.

    The token refresher shares the state folder and honours the same two
    variables -- give both containers the same PUID or they lock each other out.
    """
    puid = os.environ.get("PUID", "").strip()
    if not puid:
        return
    if os.geteuid() != 0:
        log(f"PUID is set, but the container is not root: staying uid {os.geteuid()}.")
        return
    uid = int(puid)
    gid = int(os.environ.get("PGID", "").strip() or puid)
    for root in folders:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            for path in [dirpath] + [os.path.join(dirpath, n) for n in filenames]:
                st = os.lstat(path)
                if (st.st_uid, st.st_gid) != (uid, gid):
                    os.lchown(path, uid, gid)
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
    os.environ["HOME"] = "/tmp"           # /root is off limits now
    log(f"Running as uid {uid} gid {gid} (PUID/PGID); "
        f"{', '.join(f for f in folders if f)} handed over.")


def main():
    drop_privileges(os.path.dirname(config.STATE_PATH), config.BACKUP_DIR)
    if config.WEB_ENABLED:
        import app
        app.main()
        return

    settings.load()
    log(f"Vinted reposter starting headless. locale={config.LOCALE} "
        f"dry_run={config.DRY_RUN} auto_repost={config.AUTO_REPOST} "
        f"max/day={config.MAX_REPOSTS_PER_DAY} min_age={config.MIN_ITEM_AGE_DAYS}d "
        f"active={config.ACTIVE_HOURS}")
    if config.DRY_RUN:
        log("DRY_RUN is on: nothing will be uploaded, created or deleted.")
    if not config.AUTO_REPOST:
        log("Headless with automatic reposting off: nothing will happen. Turn it on "
            "in the web UI, or set AUTO_REPOST=true.")
    worker.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
