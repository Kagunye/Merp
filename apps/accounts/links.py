"""Absolute links that go out in emails (password reset, invitations).

A link in an email is clicked later, somewhere else, so it has to be built
from an address the operator configured -- never from the incoming request.
`request.build_absolute_uri()` uses the request's Host header, which the
person asking for the email controls: with ALLOWED_HOSTS open, anybody could
ask for a reset of someone else's account while sending `Host: evil.example`,
and the victim would receive a genuine email whose link hands the secret
token to evil.example.

`settings.SITE_URL` (the SITE_URL environment variable, e.g.
https://erp.example.co.ke) is the only source in production. Only with
DEBUG on, when nothing is configured, does the request stand in for it, so a
laptop install still works.
"""
import logging

from django.conf import settings

log = logging.getLogger(__name__)


def site_url(request=None):
    """The site's base URL without a trailing slash, or "" if unknown."""
    configured = (getattr(settings, "SITE_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    if settings.DEBUG and request is not None:
        return request.build_absolute_uri("/").rstrip("/")
    return ""


def absolute_link(path, request=None):
    """`path` on this site as a full URL, or None when SITE_URL is not set.

    Callers treat None as "can't email a link": the email is not sent and the
    problem is logged for the operator, instead of mailing a link built from
    whatever host the request claimed.
    """
    base = site_url(request)
    if not base:
        log.error("SITE_URL is not configured; refusing to build an emailed link for %s", path)
        return None
    return f"{base}/{path.lstrip('/')}"
