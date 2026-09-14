#!/usr/bin/env python3
"""
Container entrypoint.

WEB_ENABLED=true (default) serves the web UI on WEB_PORT and only reposts what
you click, unless AUTO_REPOST=true also turns the scheduler on.
WEB_ENABLED=false runs headless: scheduler only, no HTTP.
"""
import time

import config
import settings
import worker
from store import log


def main():
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
