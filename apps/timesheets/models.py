"""Timesheet tracking — hours per user per project."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel


class TimesheetEntry(BaseModel):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("SUBMITTED", "Submitted"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE,
                                related_name="timesheet_entries")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE,
                             related_name="timesheet_entries")
    project = models.ForeignKey("projects.Project", on_delete=models.SET_NULL,
                                null=True, blank=True, related_name="timesheet_entries")
    task = models.ForeignKey("projects.Task", on_delete=models.SET_NULL,
                             null=True, blank=True, related_name="timesheet_entries")
    entry_date = models.DateField(db_index=True)
    hours = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    description = models.CharField(max_length=500, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    billable = models.BooleanField(default=True)
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_timesheets",
    )
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-entry_date", "-created_at"]
        indexes = [models.Index(fields=["user", "entry_date"])]

    def __str__(self):
        return f"{self.user} — {self.entry_date} — {self.hours}h"
