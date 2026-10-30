"""Notification models."""
import re
import uuid
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import models

# Browsers drop tabs, newlines and other control characters inside a URL
# before reading its scheme, so "java\tscript:" is still javascript:.
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]")


def safe_action_url(value):
    """`value` if it is safe to put in an href, otherwise "".

    A notification's link is followed by whoever clicks it, inside this site
    and with their session. Only two kinds are allowed:

    * a path on this site ("/sales/invoices/…", or a relative "invoices/…"),
      but not a protocol-relative "//elsewhere" or "/\\elsewhere";
    * a full http:// or https:// address.

    Anything with another scheme -- javascript:, data:, vbscript:, file: --
    is dropped, so the notice still shows but links nowhere.
    """
    url = (value or "").strip()
    if not url:
        return ""
    probe = _URL_NOISE.sub("", url).replace("\\", "/")
    try:
        parts = urlsplit(probe)
    except ValueError:
        return ""
    if parts.scheme:
        return url if parts.scheme.lower() in ("http", "https") and parts.netloc else ""
    if parts.netloc or probe.startswith("//"):
        return ""
    # No scheme was parsed, but a colon before the first "/" is something
    # urlsplit declined to read as one; a browser might not be so shy.
    head = probe.split("/", 1)[0]
    if ":" in head:
        return ""
    return url


class Notification(models.Model):
    TYPE_CHOICES = [
        ("info", "Info"),
        ("success", "Success"),
        ("warning", "Warning"),
        ("error", "Error"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True, default="")
    notification_type = models.CharField(max_length=10, choices=TYPE_CHOICES, default="info")
    is_read = models.BooleanField(default=False, db_index=True)
    action_url = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} — {self.title}"

    @classmethod
    def from_db(cls, db, field_names, values):
        # Rows written before links were checked are cleaned as they are
        # read, so every template that renders `action_url` gets a safe one.
        instance = super().from_db(db, field_names, values)
        if "action_url" in instance.__dict__:
            instance.action_url = safe_action_url(instance.action_url)
        return instance

    def clean(self):
        if self.action_url and not safe_action_url(self.action_url):
            raise ValidationError({"action_url": (
                "A notification link must be a page on this site (starting with /) "
                "or an http(s):// address.")})

    def save(self, *args, **kwargs):
        # Code paths that never call clean() (Notification.send, signals) still
        # can't store a javascript: or data: link.
        self.action_url = safe_action_url(self.action_url)
        super().save(*args, **kwargs)

    def mark_read(self):
        from django.utils import timezone
        if not self.is_read:
            self.is_read = True
            self.read_at = timezone.now()
            self.save(update_fields=["is_read", "read_at"])

    @classmethod
    def send(cls, user, title, message="", notification_type="info", action_url=""):
        return cls.objects.create(
            user=user, title=title, message=message,
            notification_type=notification_type, action_url=action_url,
        )
