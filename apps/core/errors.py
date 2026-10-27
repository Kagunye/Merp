"""The 500 page: a plain apology for everyone, the actual reason for admins.

Production runs with DEBUG off, so a crash only says "Server Error (500)" and
the reason sits in the host's logs. Platform administrators get the exception,
the frames in this project's code that raised it, and any migrations the
database has not applied, so a broken deploy can be diagnosed from the page.
"""
import html
import sys
import traceback

from django.http import HttpResponseServerError

PLAIN = ("<!doctype html><title>Server error</title>"
         "<h1>Server Error (500)</h1><p>Something went wrong. Please try again.</p>")


def _is_platform_admin(request):
    try:
        user = request.user
        return bool(user.is_authenticated and (user.is_superuser or user.is_system_admin))
    except Exception:
        return False


def _pending_migrations():
    try:
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        return [f"{m.app_label}.{m.name}" for m, _ in plan]
    except Exception as exc:  # the database itself may be what is broken
        return [f"(could not read migration state: {type(exc).__name__}: {exc})"]


def server_error(request):
    exc_type, exc, tb = sys.exc_info()
    if exc is None or not _is_platform_admin(request):
        return HttpResponseServerError(PLAIN)
    frames = [f for f in traceback.extract_tb(tb) if "/apps/" in f.filename or "/config/" in f.filename]
    where = "\n".join(f"{f.filename.split('/apps/')[-1]}:{f.lineno} in {f.name}\n    {f.line}"
                      for f in frames[-8:]) or "(no frames in project code)"
    pending = _pending_migrations()
    body = (
        "<!doctype html><title>Server error</title>"
        "<h1>Server Error (500)</h1>"
        "<p>Shown only to platform administrators.</p>"
        f"<h2>{html.escape(exc_type.__name__)}</h2>"
        f"<pre style='white-space:pre-wrap'>{html.escape(str(exc)[:3000])}</pre>"
        f"<h2>Where</h2><pre style='white-space:pre-wrap'>{html.escape(where)}</pre>"
        f"<h2>Unapplied migrations ({len(pending)})</h2>"
        f"<pre style='white-space:pre-wrap'>{html.escape(chr(10).join(pending) or 'none')}</pre>"
    )
    return HttpResponseServerError(body)
