"""Notification models."""
import uuid
from django.db import models


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
