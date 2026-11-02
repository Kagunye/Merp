"""The DEBUG-only file server for pictures shown inline on pages.

It used to be django.views.static.serve behind a regex that only checked
the path *started* with a public prefix. The URL is decoded before it is
matched, so `/media/users/avatars/%2e%2e/%2e%2e/documents/...` started with
"users/avatars/" and still walked up into the confidential uploads
(contracts, CVs, receipts) the protected download views exist to guard.

This view serves a file only when all of these hold:

* the path is under one of PUBLIC_MEDIA_PREFIXES;
* no segment of it is "." or ".." (or empty, or holds a backslash or a NUL),
  whatever encoding it arrived in;
* once resolved (symlinks included) it is still inside MEDIA_ROOT/<prefix>;
* it is a real file with a picture extension.

Anything else is a plain 404, so the route says nothing about what exists.
"""
import mimetypes
import os
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404

PUBLIC_MEDIA_PREFIXES = ("users/avatars/", "companies/logos/", "customers/logos/",
                         "products/images/")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}


def _resolve(path):
    """The file on disk `path` names, or None if it may not be served."""
    if not path or "\\" in path or "\x00" in path:
        return None
    prefix = next((p for p in PUBLIC_MEDIA_PREFIXES if path.startswith(p)), None)
    if prefix is None:
        return None
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    if os.path.splitext(parts[-1])[1].lower() not in IMAGE_EXTENSIONS:
        return None
    root = Path(settings.MEDIA_ROOT).resolve()
    allowed = (root / prefix).resolve()
    try:
        target = (root / path).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    if target != allowed and allowed not in target.parents:
        return None
    if not target.is_file():
        return None
    return target


def public_media(request, path):
    target = _resolve(path)
    if target is None:
        raise Http404("Not found")
    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    response = FileResponse(open(target, "rb"), content_type=content_type)
    response["X-Content-Type-Options"] = "nosniff"
    return response
