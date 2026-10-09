"""Vercel-friendly settings.

Vercel's Python runtime has a read-only filesystem except /tmp, no persistent
disk, and a tight bundle size. These settings make the project boot without
every optional service wired up, while still being safe for a live deployment.
"""
import os

from .base import *  # noqa: F401, F403

DEBUG = os.environ.get("DEBUG", "0") == "1"

# Allow Vercel preview/production hosts and any explicit ones from env.
_allowed = os.environ.get("ALLOWED_HOSTS", "")
ALLOWED_HOSTS = [h.strip() for h in _allowed.split(",") if h.strip()] or ["*"]

CSRF_TRUSTED_ORIGINS = [
    "https://*.vercel.app",
] + [o.strip() for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

# Database — prefer DATABASE_URL (Postgres, Neon, etc.); fall back to SQLite
# in /tmp so the app still boots on a cold Vercel preview without a DB attached.
try:
    import dj_database_url
    if os.environ.get("DATABASE_URL"):
        DATABASES = {
            "default": dj_database_url.parse(
                os.environ["DATABASE_URL"], conn_max_age=600, ssl_require=True
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

# Static — WhiteNoise serves pre-collected files from staticfiles_build/static.
STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles_build", "static")  # noqa: F405
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Media on a serverless filesystem is read-only; keep uploads in /tmp.
MEDIA_ROOT = "/tmp/media"

# TLS is terminated by Vercel. The redirect would otherwise cause a loop
# because the WSGI request itself arrives as HTTP from the Vercel proxy.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

# Logging to stdout so Vercel captures it.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "INFO"},
}
