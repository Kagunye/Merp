"""What an uploaded file may be.

Every upload in the app -- a document in the library, a CV, an expense
receipt, a logo, a photo -- goes through `validate_upload`. Three checks:

* the extension must be on a short whitelist of office, image and text
  formats (`ALLOWED_UPLOAD_EXTENSIONS`);
* the content must not look like HTML, SVG or script (the whole file for
  .txt/.csv, the first few kilobytes of anything else), whatever
  the extension says. A file called `invoice.pdf` that is really an HTML
  page is a stored cross-site-scripting attack the moment a browser renders
  it inline: it runs as this site, with the viewer's session;
* the file must be no bigger than `settings.MAX_UPLOAD_SIZE` (10 MB unless
  configured otherwise), so one upload can't fill the disk or the bucket.

Files are also only ever served as downloads, through the protected views in
`apps.documents.views`, so even a file that slipped past these checks would
be saved by the browser rather than run.
"""
import os
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible

ALLOWED_UPLOAD_EXTENSIONS = (
    "pdf", "png", "jpg", "jpeg", "gif", "webp", "txt", "csv",
    "xlsx", "xls", "docx", "doc", "odt", "ods",
)

DEFAULT_MAX_UPLOAD_SIZE = 10 * 1024 * 1024

# How much of the file is read for the content sniff. Browsers decide what a
# response "really" is from its first 512 bytes; reading more than that also
# catches markup pushed further down behind a little padding.
SNIFF_BYTES = 8192

# Markers that make a browser treat bytes as a page or a script. Lower-case;
# the head of the file is lower-cased before it is searched.
# Kept to tags and attributes that are markup beyond doubt: very short ones
# such as "<a " turn up by chance inside compressed image and PDF streams.
MARKUP_MARKERS = (
    b"<html", b"<!doctype html", b"<head>", b"<body", b"<script", b"<svg",
    b"<iframe", b"<object", b"<embed", b"<?xml", b"<!entity",
    b"javascript:", b"onload=", b"onerror=", b"onmouseover=",
)


# Plain-text uploads are read in full (up to the size cap), not just their
# head: a .txt or .csv has no structure a browser must find up front, so an
# attacker could otherwise pad 8 KB of harmless text and put the <script>
# after it. Binary formats (PDF, images, office files) keep the head sniff;
# their compressed bodies are full of chance byte runs.
TEXT_EXTENSIONS = ("txt", "csv")

# Tolerant patterns for text uploads. Browsers accept "< script", "onerror =",
# tabs and newlines inside a tag, and many more tags than the fixed byte list
# above names: <img onerror>, <details open ontoggle>, <meta http-equiv=
# refresh> all run or redirect with no <script> at all. An event handler
# (on...=) is looked for inside a tag only, so a line of text like
# "one = 1" in a CSV is not mistaken for one.
MARKUP_TAG_RE = re.compile(
    rb"<\s*/?\s*(script|iframe|object|embed|svg|meta|details|img|link|style|html|body|form|input)\b")
EVENT_HANDLER_RE = re.compile(rb"<[^<>]*\bon\w+\s*=")


def max_upload_size():
    return int(getattr(settings, "MAX_UPLOAD_SIZE", DEFAULT_MAX_UPLOAD_SIZE))


def _human(size):
    return f"{size / (1024 * 1024):.0f} MB" if size >= 1024 * 1024 else f"{size // 1024} KB"


def _head(value, limit=SNIFF_BYTES):
    """The first `limit` bytes of an upload, leaving its read position alone."""
    f = getattr(value, "file", value)
    if f is None or not hasattr(f, "read"):
        return b""
    try:
        pos = f.tell()
    except Exception:
        pos = None
    try:
        if hasattr(f, "seek"):
            f.seek(0)
        head = f.read(limit) or b""
    finally:
        if pos is not None and hasattr(f, "seek"):
            f.seek(pos)
    if isinstance(head, str):
        head = head.encode("utf-8", "ignore")
    return head


def looks_like_markup(head: bytes) -> bool:
    """Whether these bytes would be rendered by a browser as a page or script.

    UTF-16 text is folded to plain bytes first (dropping the NULs), so an
    HTML page saved as UTF-16 is still recognised.
    """
    sample = _fold(head)
    return any(marker in sample for marker in MARKUP_MARKERS)


def _fold(data: bytes) -> bytes:
    """Lower-case, with a leading BOM/whitespace and every NUL (UTF-16) removed."""
    sample = data.lstrip(b"\xef\xbb\xbf\xff\xfe\x00\t\r\n ")
    return sample.replace(b"\x00", b"").lower()


def text_looks_like_markup(data: bytes) -> bool:
    """The stricter check for plain-text uploads: the whole file, tolerant patterns."""
    sample = _fold(data)
    return (any(marker in sample for marker in MARKUP_MARKERS)
            or MARKUP_TAG_RE.search(sample) is not None
            or EVENT_HANDLER_RE.search(sample) is not None)


@deconstructible
class UploadValidator:
    """Callable validator for FileField / ImageField values (see module doc)."""

    def __call__(self, value):
        if not value:
            return
        # A file already stored (an edit that didn't replace it) was checked
        # when it was uploaded; don't re-read it from storage on every save.
        if getattr(value, "_committed", False):
            return
        name = os.path.basename(getattr(value, "name", "") or "")
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise ValidationError(
                f"Files of type '.{ext or '?'}' can't be uploaded. Allowed types: "
                + ", ".join(ALLOWED_UPLOAD_EXTENSIONS) + ". Save it as a PDF or "
                "an image and try again.", code="file_type")
        limit = max_upload_size()
        size = getattr(value, "size", None)
        if size is not None and size > limit:
            raise ValidationError(
                f"'{name}' is {_human(size)}; the largest file you can upload is "
                f"{_human(limit)}. Compress it or split it and try again.", code="file_size")
        if ext in TEXT_EXTENSIONS:
            suspicious = text_looks_like_markup(_head(value, limit + 1))
        else:
            suspicious = looks_like_markup(_head(value))
        if suspicious:
            raise ValidationError(
                f"'{name}' contains web-page or script content (HTML, SVG or "
                "JavaScript), which can't be stored here whatever its extension. "
                "Upload the original PDF, image or office file instead.", code="file_content")

    def __eq__(self, other):
        return isinstance(other, UploadValidator)


validate_upload = UploadValidator()


def attach_upload_validator():
    """Put `validate_upload` on every FileField and ImageField in the project.

    Called once from DocumentsConfig.ready(). The validator is appended to the
    field's runtime validator list rather than declared on each model, so the
    rule covers every app's uploads (receipts, photos, product images) from
    one place, a new upload field is covered the day it is added, and no app
    needs a migration just to carry it. Model validation (and so every
    ModelForm) runs it.
    """
    from django.apps import apps
    from django.db import models

    for model in apps.get_models():
        for field in model._meta.get_fields():
            if isinstance(field, models.FileField):
                current = field.validators  # cached list Django validates with
                if not any(isinstance(v, UploadValidator) for v in current):
                    current.append(validate_upload)
