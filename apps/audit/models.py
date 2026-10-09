"""Audit trail models — complements django-auditlog."""
from django.db import models


class AuditLog(models.Model):
    ACTION_CHOICES = [
        ("CREATE", "Created"),
        ("UPDATE", "Updated"),
        ("DELETE", "Deleted"),
        ("VIEW", "Viewed"),
        ("LOGIN", "Logged In"),
        ("LOGOUT", "Logged Out"),
        ("APPROVE", "Approved"),
        ("REJECT", "Rejected"),
        ("POST", "Posted"),
        ("REVERSE", "Reversed"),
        ("EXPORT", "Exported"),
    ]
    user = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs",
    )
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.SET_NULL, null=True, blank=True
    )
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    model_name = models.CharField(max_length=100)
    object_id = models.CharField(max_length=50)
    object_repr = models.CharField(max_length=300, blank=True, default="")
    changes = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [
            models.Index(fields=["model_name", "object_id"]),
            models.Index(fields=["user", "timestamp"]),
        ]

    def __str__(self):
        return f"{self.action} {self.model_name}#{self.object_id} by {self.user}"
