"""Projects and Task management models."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class Project(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("PLANNING", "Planning"),
        ("ACTIVE", "Active"),
        ("ON_HOLD", "On Hold"),
        ("COMPLETED", "Completed"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="projects")
    reference = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PLANNING", db_index=True)
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    budget = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    customer = models.ForeignKey("crm.Customer", on_delete=models.SET_NULL, null=True, blank=True)
    manager = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True)
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} — {self.name}"

    @property
    def progress_percent(self):
        total = self.tasks.count()
        if total == 0:
            return 0
        done = self.tasks.filter(status="DONE").count()
        return int((done / total) * 100)


class Task(BaseModel, NotesMixin):
    PRIORITY_CHOICES = [("LOW", "Low"), ("MEDIUM", "Medium"), ("HIGH", "High"), ("URGENT", "Urgent")]
    STATUS_CHOICES = [
        ("TODO", "To Do"), ("IN_PROGRESS", "In Progress"),
        ("REVIEW", "In Review"), ("DONE", "Done"), ("CANCELLED", "Cancelled"),
    ]
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tasks")
    title = models.CharField(max_length=300)
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="TODO", db_index=True)
    priority = models.CharField(max_length=6, choices=PRIORITY_CHOICES, default="MEDIUM")
    assigned_to = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    estimated_hours = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    actual_hours = models.DecimalField(max_digits=8, decimal_places=2, default=Decimal("0"))
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="subtasks")

    class Meta:
        ordering = ["priority", "due_date"]

    def __str__(self):
        return self.title
