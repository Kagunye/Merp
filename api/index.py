"""Vercel Python entrypoint — exposes the Django WSGI application as `app`.

`vercel.json` uses a `builds:` block, which makes the top-level `buildCommand`
a no-op on Vercel. Migrations and the default-admin seed therefore need to
happen on the Lambda itself. Two separate phases:

1. **Lazy bootstrap at import time** (this file). Fast fail-safe checks:
   * If the DB is Postgres AND any migration is unapplied, run `migrate`.
     Each migration is atomic on Postgres, so if two cold starts race, the
     loser's step rolls back and is logged; the next start sees it applied.
     Migrations run one app at a time so no single transaction is too long.
   * If an admin would be creatable (ADMIN_EMAIL/ADMIN_PASSWORD set and no
     admin exists yet), seed one.
   On failure the bootstrap is NOT marked complete so a fresh cold start
   retries.

2. **First-request fallback**. If phase 1 failed or was skipped, a thin
   WSGI middleware runs `migrate` on the very first request. This covers
   the case where phase 1 was killed by a timeout or a concurrent race.

Opt-out: SKIP_MIGRATE=1 disables phase 1 entirely (phase 2 still runs).
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
_LAST_MIGRATE_ERROR = None


def _pending_migrations():
    """Migrations on disk that the database hasn't applied yet."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    executor = MigrationExecutor(connection)
    return executor.migration_plan(executor.loader.graph.leaf_nodes())


def _run_migrate():
    """Run migrate one app at a time so each transaction stays short."""
    from django.core.management import call_command
    pending = _pending_migrations()
    if not pending:
        return True
    apps_to_migrate = []
    seen = set()
    for migration, _backwards in pending:
        if migration.app_label not in seen:
            seen.add(migration.app_label)
            apps_to_migrate.append(migration.app_label)
    log.info("Migrate: %d pending migration(s) across %s",
             len(pending), ", ".join(apps_to_migrate))
    for app_label in apps_to_migrate:
        log.info("Migrate: running %s", app_label)
        call_command("migrate", app_label, "--noinput", verbosity=1)
    log.info("Migrate: all apps complete")
    return True


def _admin_exists():
    try:
        from django.contrib.auth import get_user_model
        return get_user_model().objects.filter(is_superuser=True, is_active=True).exists()
    except Exception:
        return False


def _bootstrap_db_once():
    global _BOOTSTRAPPED, _LAST_MIGRATE_ERROR
    if _BOOTSTRAPPED:
        return

    if os.environ.get("SKIP_MIGRATE") == "1":
        log.info("Cold-start bootstrap: SKIP_MIGRATE=1, skipping")
        _BOOTSTRAPPED = True
        return

    try:
        from django.conf import settings
        engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
    except Exception:
        engine = ""

    if "postgresql" not in engine:
        log.info("Cold-start bootstrap: DB engine %r — skipping migrate", engine)
        _BOOTSTRAPPED = True
        return

    try:
        pending = _pending_migrations()
    except Exception as exc:
        log.exception("Cold-start bootstrap: could not read migration state: %s", exc)
        _LAST_MIGRATE_ERROR = f"Could not read migration state: {exc}"
        return

    if not pending:
        log.info("Cold-start bootstrap: schema is current")
    else:
        try:
            _run_migrate()
        except Exception as exc:
            log.exception("Cold-start migrate failed: %s", exc)
            _LAST_MIGRATE_ERROR = f"{type(exc).__name__}: {exc}"
            return

    _BOOTSTRAPPED = True
    _LAST_MIGRATE_ERROR = None

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

_inner_app = get_wsgi_application()
_first_request_migrated = _BOOTSTRAPPED


def app(environ, start_response):
    """WSGI wrapper: retries migrate on the first request if bootstrap failed."""
    global _first_request_migrated, _LAST_MIGRATE_ERROR
    if not _first_request_migrated:
        _first_request_migrated = True
        try:
            _run_migrate()
            _LAST_MIGRATE_ERROR = None
            log.info("First-request migrate: success")
        except Exception as exc:
            _LAST_MIGRATE_ERROR = f"{type(exc).__name__}: {exc}"
            log.exception("First-request migrate failed: %s", exc)
    return _inner_app(environ, start_response)


application = app
