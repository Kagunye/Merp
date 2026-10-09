"""Help Desk / Support Ticketing."""
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class Ticket(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("OPEN", "Open"),
        ("IN_PROGRESS", "In Progress"),
        ("WAITING", "Waiting on customer"),
        ("RESOLVED", "Resolved"),
        ("CLOSED", "Closed"),
    ]
    PRIORITY_CHOICES = [
        ("LOW", "Low"), ("NORMAL", "Normal"),
        ("HIGH", "High"), ("URGENT", "Urgent"),
    ]
    CHANNEL_CHOICES = [
        ("EMAIL", "Email"), ("PHONE", "Phone"),
        ("WEB", "Web"), ("CHAT", "Chat"),
    ]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="tickets")
    reference = models.CharField(max_length=50, db_index=True)
    subject = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    customer = models.ForeignKey("crm.Customer", on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="tickets")
    requester_name = models.CharField(max_length=200, blank=True, default="")
    requester_email = models.EmailField(blank=True, default="")
    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default="WEB")
    priority = models.CharField(max_length=10, choices=PRIORITY_CHOICES, default="NORMAL", db_index=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="OPEN", db_index=True)
    assigned_to = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_tickets",
    )
    opened_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-opened_at"]

    def __str__(self):
        return f"{self.reference} — {self.subject}"


class TicketComment(models.Model):
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="comments")
    author = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True)
    body = models.TextField()
    is_internal = models.BooleanField(default=False, help_text="Hide from the customer.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"Comment on {self.ticket.reference}"
