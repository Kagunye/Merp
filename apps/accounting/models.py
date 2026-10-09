"""Double-entry accounting models."""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.core.models import BaseModel, NotesMixin


ACCOUNT_TYPES = [
    ("ASSET", "Asset"),
    ("LIABILITY", "Liability"),
    ("EQUITY", "Equity"),
    ("REVENUE", "Revenue"),
    ("EXPENSE", "Expense"),
    ("CASH", "Cash"),
    ("BANK", "Bank Account"),
    ("RECEIVABLE", "Accounts Receivable"),
    ("PAYABLE", "Accounts Payable"),
    ("COST_OF_GOODS", "Cost of Goods Sold"),
    ("TAX", "Tax"),
]

NORMAL_BALANCE = {
    "ASSET": "DEBIT", "CASH": "DEBIT", "BANK": "DEBIT",
    "RECEIVABLE": "DEBIT", "EXPENSE": "DEBIT", "COST_OF_GOODS": "DEBIT",
    "LIABILITY": "CREDIT", "EQUITY": "CREDIT", "REVENUE": "CREDIT",
    "PAYABLE": "CREDIT", "TAX": "CREDIT",
}


class Account(BaseModel):
    """Chart of Accounts entry."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="accounts")
    code = models.CharField(max_length=20, db_index=True)
    name = models.CharField(max_length=200)
    account_type = models.CharField(max_length=20, choices=ACCOUNT_TYPES)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children")
    description = models.TextField(blank=True, default="")
    is_reconcilable = models.BooleanField(default=False)
    currency = models.CharField(max_length=3, blank=True, default="")
    department = models.ForeignKey(
        "organizations.Department", on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        unique_together = [("company", "code")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def current_balance(self):
        lines = self.journal_lines.filter(journal_entry__status="POSTED")
        debit_total = lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0")
        credit_total = lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0")
        if NORMAL_BALANCE.get(self.account_type) == "DEBIT":
            return debit_total - credit_total
        return credit_total - debit_total

    @property
    def normal_balance(self):
        return NORMAL_BALANCE.get(self.account_type, "DEBIT")


class TaxRate(BaseModel):
    """Configurable tax rates."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="tax_rates")
    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20)
    rate = models.DecimalField(max_digits=7, decimal_places=4)
    is_inclusive = models.BooleanField(default=False)
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="tax_rates")
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        unique_together = [("company", "code")]

    def __str__(self):
        return f"{self.name} ({self.rate}%)"


class JournalEntry(BaseModel, NotesMixin):
    """A balanced accounting journal entry."""
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("POSTED", "Posted"),
        ("REVERSED", "Reversed"),
        ("CANCELLED", "Cancelled"),
    ]
    ENTRY_TYPES = [
        ("MANUAL", "Manual Entry"),
        ("INVOICE", "Customer Invoice"),
        ("BILL", "Supplier Bill"),
        ("PAYMENT", "Payment"),
        ("RECEIPT", "Receipt"),
        ("PAYROLL", "Payroll"),
        ("DEPRECIATION", "Depreciation"),
        ("STOCK_ADJUSTMENT", "Stock Adjustment"),
        ("OPENING", "Opening Balance"),
        ("CLOSING", "Period Closing"),
        ("REVERSAL", "Reversal"),
    ]

    company = models.ForeignKey("organizations.Company", on_delete=models.PROTECT, related_name="journal_entries")
    reference = models.CharField(max_length=50, db_index=True)
    entry_type = models.CharField(max_length=20, choices=ENTRY_TYPES, default="MANUAL")
    posting_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    period = models.ForeignKey(
        "organizations.AccountingPeriod", on_delete=models.PROTECT, null=True, blank=True
    )
    reversed_by = models.OneToOneField(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="reversal_of"
    )
    posted_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, null=True, blank=True, related_name="posted_entries"
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    source_document_type = models.CharField(max_length=50, blank=True, default="")
    source_document_id = models.CharField(max_length=50, blank=True, default="")

    class Meta:
        ordering = ["-posting_date", "-created_at"]
        verbose_name_plural = "journal entries"

    def __str__(self):
        return f"{self.reference} ({self.posting_date})"

    @property
    def total_debit(self):
        return self.lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0")

    @property
    def total_credit(self):
        return self.lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0")

    def is_balanced(self):
        return abs(self.total_debit - self.total_credit) < Decimal("0.01")

    def post(self, user=None):
        if self.status != "DRAFT":
            raise ValidationError(f"Cannot post a journal entry with status '{self.status}'.")
        if not self.is_balanced():
            raise ValidationError(
                f"Journal entry is not balanced: debit={self.total_debit}, credit={self.total_credit}."
            )
        if not self.lines.exists():
            raise ValidationError("Journal entry must have at least one line.")
        with transaction.atomic():
            self.status = "POSTED"
            self.posted_by = user
            self.posted_at = timezone.now()
            self.save(update_fields=["status", "posted_by", "posted_at", "updated_at"])

    def reverse(self, user=None, posting_date=None):
        if self.status != "POSTED":
            raise ValidationError("Only posted entries can be reversed.")
        with transaction.atomic():
            from apps.core.models import SequenceCounter
            ref = SequenceCounter.next_number(self.company, "JE-REV-")
            reversal = JournalEntry.objects.create(
                company=self.company,
                reference=ref,
                entry_type="REVERSAL",
                posting_date=posting_date or timezone.now().date(),
                notes=f"Reversal of {self.reference}",
                status="DRAFT",
            )
            for line in self.lines.all():
                JournalEntryLine.objects.create(
                    journal_entry=reversal,
                    account=line.account,
                    description=f"Reversal: {line.description}",
                    debit_amount=line.credit_amount,
                    credit_amount=line.debit_amount,
                )
            reversal.post(user=user)
            self.status = "REVERSED"
            self.reversed_by = reversal
            self.save(update_fields=["status", "reversed_by", "updated_at"])
        return reversal


class JournalEntryLine(models.Model):
    """A single debit or credit line in a journal entry."""
    journal_entry = models.ForeignKey(JournalEntry, on_delete=models.CASCADE, related_name="lines")
    account = models.ForeignKey(Account, on_delete=models.PROTECT, related_name="journal_lines")
    description = models.CharField(max_length=500, blank=True, default="")
    debit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    credit_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    cost_center = models.ForeignKey(
        "organizations.Department", on_delete=models.SET_NULL, null=True, blank=True
    )
    partner_id = models.CharField(max_length=50, blank=True, default="")
    partner_type = models.CharField(max_length=20, blank=True, default="")

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return f"{self.account} Dr:{self.debit_amount} Cr:{self.credit_amount}"

    def clean(self):
        if self.debit_amount < 0 or self.credit_amount < 0:
            raise ValidationError("Amounts cannot be negative.")
        if self.debit_amount > 0 and self.credit_amount > 0:
            raise ValidationError("A line cannot have both debit and credit amounts.")


class BudgetLine(BaseModel):
    """Budget allocation per account/period."""
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    fiscal_year = models.ForeignKey("organizations.FiscalYear", on_delete=models.CASCADE)
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL, null=True, blank=True)
    period = models.ForeignKey("organizations.AccountingPeriod", on_delete=models.CASCADE)
    budgeted_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        unique_together = [("company", "fiscal_year", "account", "department", "period")]

    @property
    def actual_amount(self):
        lines = JournalEntryLine.objects.filter(
            account=self.account,
            journal_entry__status="POSTED",
            journal_entry__posting_date__gte=self.period.start_date,
            journal_entry__posting_date__lte=self.period.end_date,
        )
        if self.account.normal_balance == "DEBIT":
            return (lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0")) - \
                   (lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0"))
        return (lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0")) - \
               (lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0"))

    @property
    def variance(self):
        return self.actual_amount - self.budgeted_amount
