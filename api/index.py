"""Vercel Python entrypoint — exposes the Django WSGI application as `app`.

`vercel.json` uses a `builds:` block, which makes the top-level `buildCommand`
a no-op on Vercel. The migrate / default-admin seed from `./build_files.sh`
therefore never actually runs on the serverless function. We bootstrap once
per cold start:

* `migrate --noinput` whenever the active DB is Postgres (SQLite in /tmp is
  the local-only fallback and doesn't need this). The call is idempotent, so
  concurrent cold starts and repeat invocations converge rather than fight.
* `create_default_admin` when ADMIN_EMAIL / ADMIN_PASSWORD are set in the
  environment. Also idempotent.

Each step is wrapped in try/except — a transient failure during bootstrap
should never take the whole function down. Set SKIP_MIGRATE=1 to opt out
entirely (e.g. in a managed-migration environment).
"""
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.vercel")

import django  # noqa: E402

django.setup()

log = logging.getLogger("api.index")

_BOOTSTRAPPED = False


def _bootstrap_db_once():
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED:
        return
    _BOOTSTRAPPED = True

    if os.environ.get("SKIP_MIGRATE") == "1":
        log.info("Cold-start bootstrap: SKIP_MIGRATE=1, skipping")
        return

    try:
        from django.conf import settings
        engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    except Exception:
        engine = ""

    # Only run migrate for a real DB. The /tmp SQLite fallback is per-Lambda
    # scratch and gets re-created at app start by Django on first write.
    if "postgresql" not in engine:
        log.info("Cold-start bootstrap: DB engine %r — skipping migrate", engine)
    else:
        try:
            from django.core.management import call_command
            log.info("Cold-start bootstrap: running migrate --noinput")
            call_command("migrate", "--noinput", verbosity=1)
            log.info("Cold-start bootstrap: migrate complete")
        except Exception as exc:
            log.exception("Cold-start migrate failed: %s", exc)

    try:
        if os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD"):
            from django.core.management import call_command
            log.info("Cold-start bootstrap: ensuring default admin exists")
            call_command("create_default_admin")
            log.info("Cold-start bootstrap: admin ensured")
        else:
            log.info("Cold-start bootstrap: ADMIN_EMAIL/ADMIN_PASSWORD unset — skipping admin seed")
    except Exception as exc:
        log.exception("Cold-start admin seed failed: %s", exc)


_bootstrap_db_once()

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
application = app
