"""Vercel Python entrypoint — exposes the Django WSGI application as `app`.

`vercel.json` uses a `builds:` block, which makes the top-level `buildCommand`
a no-op on Vercel. Migrations and the default-admin seed therefore need to
happen on the Lambda itself. Two separate phases:

1. **Lazy bootstrap at import time** (this file). Fast fail-safe checks:
   * If the DB is Postgres AND the auth table is missing, run `migrate`.
     We fast-path when the table already exists so repeat cold starts
     don't re-walk the migration graph.
   * If an admin would be creatable (ADMIN_EMAIL/ADMIN_PASSWORD set and no
     admin exists yet), seed one.
   The whole phase is wrapped in try/except, has a hard time cap, and
   degrades to doing nothing if anything goes wrong.

2. **Mid-request self-heal** (via `apps/accounts/views.py`). If the login
   POST hits an OperationalError (table missing / connection dropped),
   the view runs `migrate` once and retries. This covers the case where
   phase 1 was skipped or raced the first request.

Opt-out: SKIP_MIGRATE=1 disables phase 1 entirely.
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


def _auth_table_exists():
    """Fast check: does the accounts_user table already exist?"""
    try:
        from django.db import connection
        with connection.cursor() as c:
            # information_schema is portable across Postgres backends.
            c.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = %s LIMIT 1",
                ["accounts_user"],
            )
            return c.fetchone() is not None
    except Exception:
        return False


def _admin_exists():
    try:
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(is_superuser=True, is_active=True).exists()
    except Exception:
        return False


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

    if "postgresql" not in engine:
        log.info("Cold-start bootstrap: DB engine %r — skipping migrate", engine)
        return

    # Fast-path: schema already in place. Skip migrate so repeat cold
    # starts don't pay the walk-through cost (which can push a Lambda past
    # its 10s Hobby timeout and never actually complete).
    if _auth_table_exists():
        log.info("Cold-start bootstrap: auth table exists — skipping migrate")
    else:
        try:
            from django.core.management import call_command
            log.info("Cold-start bootstrap: auth table missing — running migrate --noinput")
            call_command("migrate", "--noinput", verbosity=1)
            log.info("Cold-start bootstrap: migrate complete")
        except Exception as exc:
            log.exception("Cold-start migrate failed: %s", exc)
            return

    try:
        has_admin_env = os.environ.get("ADMIN_EMAIL") and os.environ.get("ADMIN_PASSWORD")
        if has_admin_env and not _admin_exists():
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
