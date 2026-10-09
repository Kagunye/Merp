"""Expense claims and budget models."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class ExpenseCategory(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    gl_account = models.ForeignKey("accounting.Account", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name


class ExpenseClaim(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("SUBMITTED", "Submitted"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("PAID", "Paid"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="expense_claims")
    reference = models.CharField(max_length=50, db_index=True)
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="expense_claims")
    claim_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="DRAFT")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_expenses"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-claim_date"]

    def __str__(self):
        return self.reference


class ExpenseClaimLine(models.Model):
    claim = models.ForeignKey(ExpenseClaim, on_delete=models.CASCADE, related_name="lines")
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)
    description = models.CharField(max_length=500)
    expense_date = models.DateField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    receipt = models.FileField(upload_to="expenses/receipts/", null=True, blank=True)

    def __str__(self):
        return f"{self.description}: {self.amount}"
