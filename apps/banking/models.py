"""Banking: Bank accounts, transactions, reconciliation."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class BankAccount(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="bank_accounts")
    name = models.CharField(max_length=200)
    bank_name = models.CharField(max_length=200)
    account_number = models.CharField(max_length=100)
    account_type = models.CharField(
        max_length=20,
        choices=[("CURRENT", "Current"), ("SAVINGS", "Savings"), ("FIXED", "Fixed Deposit"), ("PETTY_CASH", "Petty Cash")],
        default="CURRENT",
    )
    currency = models.CharField(max_length=3, default="KES")
    branch = models.CharField(max_length=200, blank=True, default="")
    gl_account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT, related_name="bank_accounts")
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.bank_name})"

    @property
    def current_balance(self):
        return self.gl_account.current_balance


class BankTransaction(BaseModel, NotesMixin):
    TRANSACTION_TYPES = [
        ("CREDIT", "Credit / Inflow"),
        ("DEBIT", "Debit / Outflow"),
    ]
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="transactions")
    transaction_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    transaction_type = models.CharField(max_length=6, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.CharField(max_length=500, blank=True, default="")
    reference = models.CharField(max_length=200, blank=True, default="")
    is_reconciled = models.BooleanField(default=False, db_index=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )
    running_balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-transaction_date", "-created_at"]

    def __str__(self):
        return f"{self.transaction_type} {self.amount} — {self.transaction_date}"
