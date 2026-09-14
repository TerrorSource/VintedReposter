"""
Docker HEALTHCHECK. Exit 0 = healthy.

With the web UI on, ask /api/health whether the worker and scheduler threads
are alive. Headless, there is nothing to probe over HTTP, so report healthy and
let the log speak.
"""
import os
import sys
import urllib.request

if os.environ.get("WEB_ENABLED", "true").strip().lower() in ("0", "false", "no"):
    sys.exit(0)

port = os.environ.get("WEB_PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
