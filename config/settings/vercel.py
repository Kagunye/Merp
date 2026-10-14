"""Vercel-friendly settings.

Vercel's Python runtime has a read-only filesystem except /tmp, no persistent
disk, and a tight bundle size. These settings make the project boot without
every optional service wired up, while still being safe for a live deployment.
"""
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .base import *  # noqa: F401, F403

DEBUG = os.environ.get("DEBUG", "0") == "1"

# Allow Vercel preview/production hosts and any explicit ones from env.
_allowed = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()] or ["*"]

CSRF_TRUSTED_ORIGINS = [
    "https://*.vercel.app",
] + [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Database — support DATABASE_URL and the standard Vercel/Supabase Postgres
# variables. Fall back to SQLite in /tmp if no database is configured.
#
# Prefer a NON-transaction-pooler URL. Supabase's :6543 "pooler" is a PgBouncer
# *transaction* pooler, which breaks Django's connection handling (prepared
# statements / session state) and shows up as intermittent 500s on auth and
# writes. The session pooler (:5432, POSTGRES_URL_NON_POOLING) or a direct
# connection is the correct target for Django.
def _is_txn_pooler(url):
    try:
        return urlsplit(url).port == 6543
    except ValueError:
        return False

_candidates = [
    os.environ.get("DATABASE_URL"),
    os.environ.get("POSTGRES_URL_NON_POOLING"),
    os.environ.get("POSTGRES_URL"),
    os.environ.get("POSTGRES_PRISMA_URL"),
]
_candidates = [c for c in _candidates if c]
# Use the first candidate that is NOT the transaction pooler; only fall back to
# a :6543 URL if that is all we have.
_database_url = next((c for c in _candidates if not _is_txn_pooler(c)), None) \
    or (_candidates[0] if _candidates else None)
try:
    import dj_database_url
    if _database_url:
        parsed_url = urlsplit(_database_url)
        query = urlencode(
            [
                (key, value)
                for key, value in parse_qsl(
                    parsed_url.query, keep_blank_values=True
                )
                if key not in {"pgbouncer", "supa"}
            ]
        )
        _database_url = urlunsplit(parsed_url._replace(query=query))
        # conn_max_age=0 — do not hold connections open across Lambda
        # freeze/resume; a pooled connection left in-transaction breaks the
        # next request that reuses the same warm instance.
        DATABASES = {
            "default": dj_database_url.parse(
                _database_url, conn_max_age=0, ssl_require=True
            )
        }
    else:
        DATABASES = {
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": "/tmp/db.sqlite3",
            }
        }
except ImportError:  # pragma: no cover
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": "/tmp/db.sqlite3",
        }
    }

# Static — WhiteNoise serves directly from STATICFILES_DIRS (the source
# `static/` dir), skipping collectstatic entirely. On Vercel the explicit
# `builds` block in vercel.json makes `buildCommand` a no-op, so a
# collectstatic-at-build-time pipeline silently never runs; serving from
# finders instead is reliable and lets a single commit of the asset ship
# straight into the Lambda bundle via vercel.json's `includeFiles`.
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles_build", "static")  # noqa: F405
STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
WHITENOISE_USE_FINDERS = True
WHITENOISE_MANIFEST_STRICT = False
WHITENOISE_AUTOREFRESH = True

# Media on a serverless filesystem is read-only; keep uploads in /tmp.
MEDIA_ROOT = "/tmp/media"

# TLS is terminated by Vercel. The redirect would otherwise cause a loop
# because the WSGI request itself arrives as HTTP from the Vercel proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Sessions — store them in a signed cookie instead of the DB. On Vercel the
# `django_session` table may not exist yet on the first cold-start request
# (migrate races the traffic), which makes `login()` blow up with "Could not
# start a session". Signed cookies have no server-side table dependency, so
# sign-in works as soon as the app boots.
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

# Logging to stdout so Vercel captures it. Django's request-exception logger
# is set to ERROR explicitly so unhandled 500s print a full traceback in the
# Vercel Runtime Logs instead of a bare "Server Error (500)".
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
