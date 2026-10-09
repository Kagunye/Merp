"""Vercel Python entrypoint — exposes the Django WSGI application as `app`."""
import os
import sys
from pathlib import Path

# Make the project root importable (so `config.*`, `apps.*` resolve).
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.vercel")

from django.core.wsgi import get_wsgi_application  # noqa: E402

app = get_wsgi_application()
application = app
