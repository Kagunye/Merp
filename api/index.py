"""Vercel Python entrypoint — exposes the Django WSGI application as `app`.

Because `vercel.json` uses a `builds:` block, Vercel ignores the top-level
`buildCommand`, so `./build_files.sh` never actually runs on the serverless
function. Migrations and the default-admin seed therefore need to happen
elsewhere. We do them once per Lambda cold start, guarded by RUN_MIGRATIONS=1,
so the first request after a deploy brings the Supabase schema up to date
without needing a separate ops endpoint. Both `migrate` and
`create_default_admin` are idempotent — repeated runs are no-ops.
"""
import logging
import os
import sys
from pathlib import Path

# Make the project root importable (so `config.*`, `apps.*` resolve).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.vercel")

import django  # noqa: E402

django.setup()

log = logging.getLogger("api.index")


def _bootstrap_db_once():
    """Run migrate + create_default_admin on cold start if opted in."""
    if os.environ.get("RUN_MIGRATIONS") != "1":
        return
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
    except Exception as exc:
        log.exception("Cold-start admin seed failed: %s", exc)


_bootstrap_db_once()

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
application = app
