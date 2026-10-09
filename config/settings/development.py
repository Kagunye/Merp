"""Development settings."""
from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]

# Safely remove optional third-party apps not installed in dev
_optional_apps = {
    "debug_toolbar", "django_extensions",
    "django_celery_beat", "django_celery_results",
    "crispy_forms", "crispy_tailwind",
    "whitenoise.runserver_nostatic",
}

def _safe_import(module):
    try:
        __import__(module.split(".")[0].replace("-", "_"))
        return True
    except ImportError:
        return False

INSTALLED_APPS = [a for a in INSTALLED_APPS if a not in _optional_apps or _safe_import(a)]  # noqa: F405

# Remove middleware for missing apps
MIDDLEWARE = [m for m in MIDDLEWARE if not m.startswith("debug_toolbar")]  # noqa: F405

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",  # noqa: F405
    }
}

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

STATICFILES_STORAGE = "django.contrib.staticfiles.storage.StaticFilesStorage"
