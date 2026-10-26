"""Double-entry accounting models."""
from decimal import Decimal, ROUND_HALF_UP

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

# The accounts a period close empties: everything that belongs to the profit
# or loss for the period rather than to the ongoing balance sheet.
INCOME_STATEMENT_TYPES = ("REVENUE", "COST_OF_GOODS", "EXPENSE")


def period_for(company, posting_date):
    """The accounting period a posting date falls in, or None.

    Looked up by date rather than taken from the current period, because a
    late adjustment is posted for the month it belongs to, not for today.
    """
    from apps.organizations.models import AccountingPeriod

    return AccountingPeriod.objects.filter(
        fiscal_year__company=company,
        start_date__lte=posting_date,
        end_date__gte=posting_date,
    ).order_by("start_date").first()


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
        return self.balance_as_at(None)

    def balance_as_at(self, on_date):
        """The account's balance at the close of `on_date`, in its normal sign.

        None means every posting the book holds, future-dated ones included —
        which is what an "is anything still on this account" question needs.
        Anything that reports a figure *as at* a day passes the day: a receipt
        dated next week has not happened yet, and a balance that counts it is a
        forecast, not a balance.
        """
        lines = self.journal_lines.filter(journal_entry__status="POSTED")
        if on_date is not None:
            lines = lines.filter(journal_entry__posting_date__lte=on_date)
        totals = lines.aggregate(dr=models.Sum("debit_amount"), cr=models.Sum("credit_amount"))
        debit_total = totals["dr"] or Decimal("0")
        credit_total = totals["cr"] or Decimal("0")
        if NORMAL_BALANCE.get(self.account_type) == "DEBIT":
            return debit_total - credit_total
        return credit_total - debit_total

    @property
    def has_postings(self):
        """Whether any journal line, posted or still a draft, names this account.

        Drafts count: a draft is a working paper that will be posted as typed,
        and changing what kind of account it lands on underneath it would post
        it somewhere the bookkeeper never chose.
        """
        return self.journal_lines.exclude(journal_entry__status="CANCELLED").exists()

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
        ("CREDIT_NOTE", "Credit Note"),
        ("DEBIT_NOTE", "Debit Note (returned goods)"),
        ("PAYMENT", "Payment"),
        ("RECEIPT", "Receipt"),
        ("PAYROLL", "Payroll"),
        ("DEPRECIATION", "Depreciation"),
        ("ASSET", "Fixed Asset Acquisition"),
        ("DISPOSAL", "Fixed Asset Disposal"),
        ("STOCK_ADJUSTMENT", "Stock Adjustment"),
        ("OPENING", "Opening Balance"),
        ("CLOSING", "Period Closing"),
        ("REVALUATION", "Exchange Revaluation"),
        ("REVERSAL", "Reversal"),
    ]

    # The kinds of entry a person types on the manual journal screen. Every
    # other kind is machinery: an invoice, a payroll run, a depreciation charge
    # or a period close writes its own entry, and the type is how the rest of
    # the book recognises it — a CLOSING entry is left out of the profit and
    # loss and out of the next close, a REVALUATION is exchange machinery that
    # the next revaluation takes off again. Letting a person type those would
    # let a hand-made journal hide from the reports that are meant to show it.
    # OPENING stays on the list because a new book is often opened by hand as
    # one ordinary journal; the opening-balances screen is the guarded way in.
    USER_ENTRY_TYPES = ("MANUAL", "OPENING")

    # Where an entry made by a module is answered, so the generic Reverse
    # button can say which screen to use instead of offsetting the ledger
    # behind the document's back.
    REVERSE_ELSEWHERE = {
        "INVOICE": "Cancel the invoice, or raise a credit note against it, from Sales → Invoices.",
        "CREDIT_NOTE": "A credit note answers its invoice; work with it from Sales → Invoices.",
        "BILL": "Cancel the bill, or raise a debit note against it, from Purchasing → Bills.",
        "DEBIT_NOTE": "A debit note answers its bill; work with it from Purchasing → Bills.",
        "PAYMENT": "Reverse the payment from the screen it was recorded on (Sales → Payments "
                   "for a receipt, the bill for a supplier payment, the till for a refund).",
        "RECEIPT": "Refund the sale from the till it was rung on, or reverse the receipt "
                   "from Sales → Payments.",
        "PAYROLL": "Cancel the payroll run from Payroll → Runs.",
        "DEPRECIATION": "Stand the month's charge down from the asset's page in Fixed Assets.",
        "ASSET": "Stand the purchase down from the asset's page in Fixed Assets.",
        "DISPOSAL": "Bring the asset back from its page in Fixed Assets.",
        "STOCK_ADJUSTMENT": "Post an opposite stock movement from Inventory, so the shelf "
                            "and the ledger move together.",
        "CLOSING": "Reopen the period, or the fiscal year, from Accounting → Fiscal Years.",
        "REVALUATION": "Take the revaluation off from Accounting → Exchange Revaluation.",
        "REVERSAL": "A reversal is not reversed again — that would quietly reinstate the "
                    "original. Post a fresh entry for what should stand.",
    }

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
    # The ledger only ever holds book-currency figures, but anyone reading a
    # journal line has to be able to tell what the document underneath it was
    # denominated in and at which rate it was translated.
    source_currency = models.CharField(max_length=3, blank=True, default="")
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)

    class Meta:
        ordering = ["-posting_date", "-created_at"]
        verbose_name_plural = "journal entries"
        constraints = [
            models.UniqueConstraint(
                fields=["company", "reference"], name="unique_reference_per_company",
            ),
        ]

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
        if not self.lines.exists():
            raise ValidationError("Journal entry must have at least one line.")
        # Every module writes its lines straight to the table, so the rules a
        # line form enforces are enforced again here, where every entry passes.
        lines = self.lines.all()
        if lines.filter(models.Q(debit_amount__lt=0) | models.Q(credit_amount__lt=0)).exists():
            raise ValidationError(
                "A journal line cannot carry a negative amount. Put the figure on the "
                "other side of the line instead — a negative credit is a debit.")
        if lines.filter(debit_amount__gt=0, credit_amount__gt=0).exists():
            raise ValidationError(
                "A journal line is either a debit or a credit, not both. Split it into "
                "two lines.")
        if lines.filter(debit_amount=0, credit_amount=0).exists():
            raise ValidationError(
                "A journal line with neither a debit nor a credit moves nothing. Give "
                "it an amount or remove it.")
        if not self.is_balanced():
            raise ValidationError(
                f"Journal entry is not balanced: debit={self.total_debit}, credit={self.total_credit}."
            )
        if self.total_debit <= 0:
            raise ValidationError(
                "This entry adds up to nothing, so there is nothing to post. Enter the "
                "amounts it should move, or cancel the draft.")
        period = self.period if self.period_id else period_for(self.company, self.posting_date)
        if period is None:
            # A date the calendar does not cover belongs to no year, so no close
            # would ever sweep it and no report bounded by a year would show it.
            raise ValidationError(
                f"No fiscal year covers {self.posting_date:%d %b %Y}. Create that year "
                "under Accounting → Fiscal Years before posting into it, or check the date.")
        if period.status == "LOCKED" or period.fiscal_year.is_closed:
            # A closed year has been swept into retained earnings. Anything
            # posted into it now would sit in the profit and loss accounts of
            # a year whose result has already been carried forward.
            raise ValidationError(
                f"{period.fiscal_year.name} is closed, and {self.posting_date:%d %b %Y} "
                "falls inside it. Reopen the fiscal year first, or date the entry in an "
                "open year.")
        with transaction.atomic():
            self.period = period
            self.status = "POSTED"
            self.posted_by = user
            self.posted_at = timezone.now()
            self.save(update_fields=[
                "status", "posted_by", "posted_at", "period", "updated_at",
            ])

    @property
    def calendar_warning(self):
        """Why this entry deserves a second look, in the bookkeeper's terms.

        Reported and never blocked: a company must be able to file a late
        adjustment into a period it has already closed, but the entry has to
        say out loud that it does.
        """
        if self.period_id is None:
            return f"No accounting period covers {self.posting_date:%d %b %Y}."
        if self.period.status != "OPEN":
            return (f"{self.period.name} is {self.period.get_status_display().lower()} "
                    f"and this entry is dated inside it.")
        if self.period.fiscal_year.is_closed:
            return f"{self.period.fiscal_year.name} is closed and this entry is dated inside it."
        return ""

    @property
    def is_reversal(self):
        """Whether this entry exists to offset another one."""
        return self.entry_type == "REVERSAL" or JournalEntry.objects.filter(
            reversed_by=self).exists()

    def linked_documents(self):
        """The rows elsewhere in the book that point at this entry.

        Read from the model registry rather than a hand-kept list, so a module
        added later — a till, a payroll run, an expense claim — is recognised
        as the owner of its entry without this file having to know it exists.
        """
        found = []
        for relation in self._meta.related_objects:
            if relation.many_to_many or relation.related_model in (
                    JournalEntry, JournalEntryLine):
                continue
            if relation.related_model._meta.app_label == "banking":
                # A bank statement line is matched to an entry, it does not
                # write one: the journal is still the bookkeeper's own.
                continue
            row = relation.related_model._default_manager.filter(
                **{relation.field.name: self}).first()
            if row is not None:
                found.append(row)
        return found

    def generic_reversal_refusal(self):
        """Why the journal screen's own Reverse button must not touch this entry.

        Returns "" when it may. The button is for journals a person typed: an
        entry written by an invoice, a payroll run, a depreciation charge or a
        close belongs to that document, and offsetting it here would leave the
        document saying one thing and the ledger another — an invoice still
        owed while receivables show it settled, an asset still worn while its
        accumulated depreciation is back. Those are answered on their own
        screens, which reverse the entry and the document together.
        """
        if self.status != "POSTED":
            return "Only a posted entry can be reversed; a draft is cancelled instead."
        if self.reversed_by_id:
            return f"{self.reference} has already been reversed by {self.reversed_by.reference}."
        if self.is_reversal:
            return self.REVERSE_ELSEWHERE["REVERSAL"]
        if self.entry_type not in self.USER_ENTRY_TYPES:
            return (f"{self.reference} was written by the ledger as a "
                    f"{self.get_entry_type_display().lower()} entry. "
                    + self.REVERSE_ELSEWHERE.get(
                        self.entry_type, "Reverse it from the screen that made it."))
        documents = self.linked_documents()
        if self.source_document_type or documents:
            owner = (f"{documents[0]._meta.verbose_name} {documents[0]}" if documents
                     else self.source_document_type)
            return (f"{self.reference} belongs to {owner}. Reverse it from that "
                    "document's own screen, so the document and the ledger move together.")
        return ""

    def reverse(self, user=None, posting_date=None):
        if self.status != "POSTED":
            raise ValidationError("Only posted entries can be reversed.")
        if self.reversed_by_id:
            raise ValidationError(
                f"{self.reference} has already been reversed by {self.reversed_by.reference}."
            )
        if self.is_reversal:
            # Reversing a reversal reinstates the original without anyone having
            # posted it again — the original would be live a second time while
            # still carrying its "reversed" link.
            raise ValidationError(
                f"{self.reference} is itself a reversal, and a reversal is not reversed "
                "again. Post a fresh entry for what should stand.")
        with transaction.atomic():
            from apps.core.models import SequenceCounter
            ref = SequenceCounter.next_number(self.company, "JE-REV-")
            on_date = posting_date or self.posting_date
            reversal = JournalEntry.objects.create(
                company=self.company,
                reference=ref,
                # A reversal of closing machinery is still closing machinery:
                # tagging it REVERSAL would make the period's activity look like
                # fresh trading and the next close would count it twice. The same
                # holds for a revaluation, which is exchange machinery either way.
                entry_type=("CLOSING" if self.entry_type == "CLOSING" else
                            "REVALUATION" if self.entry_type == "REVALUATION" else
                            "REVERSAL"),
                source_document_type=self.source_document_type,
                source_document_id=self.source_document_id,
                # Reversing in the original period keeps a closed period's
                # figures netting to zero; posting the reversal today would
                # leave the closed month overstated for ever.
                posting_date=on_date,
                notes=f"Reversal of {self.reference}",
                status="DRAFT",
                # Deliberately reversing on another date means the entry belongs
                # to that other period, so the calendar has to follow the date
                # rather than the entry being answered.
                period=(self.period if on_date == self.posting_date
                        else period_for(self.company, on_date)),
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
            # The original stays POSTED. A reversal is a second entry that
            # offsets it, not an eraser: every balance query in the book counts
            # POSTED lines only, so flipping the original out of POSTED while
            # its mirror stays in would move the account twice as far as it
            # should instead of back to where it was.
            self.reversed_by = reversal
            self.save(update_fields=["reversed_by", "updated_at"])
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
        if not self.debit_amount and not self.credit_amount:
            raise ValidationError("A line needs a debit or a credit; a line of nothing moves nothing.")


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


# ────────────────────────────── foreign currency ────────────────────────────

class Currency(BaseModel):
    """A currency the book is allowed to transact in.

    The company's own code (Company.currency) is what the ledger is kept in;
    every other currency here is only usable once the company has filed an
    exchange rate for it, because a document without a rate cannot be booked.
    """
    code = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=60)
    symbol = models.CharField(max_length=6, blank=True, default="")
    # Digits after the decimal point: 2 for shillings and dollars, 0 for yen.
    minor_units = models.PositiveSmallIntegerField(default=2)

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code

    @property
    def exponent(self):
        return Decimal(1).scaleb(-self.minor_units)


class ExchangeRate(models.Model):
    """What one unit of a foreign currency was worth on a given day.

    Rates are dated rather than overwritten: an invoice booked in March must
    still convert at the March rate when it is paid in June, so the history of
    the rate is what makes the ledger reproducible.
    """
    SOURCES = [
        ("MANUAL", "Entered by hand"),
        ("BANK", "Bank statement"),
        ("CBK", "Central bank"),
    ]

    company = models.ForeignKey(
        "organizations.Company", on_delete=models.CASCADE, related_name="exchange_rates"
    )
    currency = models.ForeignKey(Currency, on_delete=models.PROTECT, related_name="rates")
    effective_date = models.DateField(db_index=True)
    # Book currency units per one unit of this currency.
    rate = models.DecimalField(max_digits=18, decimal_places=6)
    source = models.CharField(max_length=10, choices=SOURCES, default="MANUAL")
    note = models.CharField(max_length=200, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-effective_date"]
        constraints = [
            models.UniqueConstraint(
                fields=["company", "currency", "effective_date"],
                name="one_rate_per_currency_per_day",
            ),
        ]

    def __str__(self):
        return f"1 {self.currency.code} = {self.rate} on {self.effective_date}"

    def clean(self):
        if self.rate is not None and self.rate <= 0:
            raise ValidationError({"rate": "An exchange rate must be more than zero."})

    @classmethod
    def rate_for(cls, company, code, on_date):
        """The rate to convert `code` with on `on_date`.

        The latest rate on or before the date — a weekend or a holiday simply
        keeps using Friday's rate. The book's own currency is always 1.
        Raises ValidationError when nothing has been filed, because inventing
        a rate is how a silent mis-statement starts.
        """
        code = (code or "").upper()
        book = (getattr(company, "currency", "") or "").upper()
        if not code or code == book:
            return Decimal("1")
        rate = cls.objects.filter(
            company=company, currency__code=code, effective_date__lte=on_date,
        ).order_by("-effective_date").values_list("rate", flat=True).first()
        if rate is None:
            raise ValidationError(
                f"No exchange rate for {code} on or before {on_date:%d %b %Y}. "
                f"File one under Accounting → Currencies before posting in {code}."
            )
        return rate


def to_base(amount, rate):
    """Convert a foreign amount into the book currency, half-up to the cent.

    Rounding once, here, is what keeps a document total and the journal lines
    that come from it equal — rounding each line separately would leave a
    residue that no amount of checking can explain.
    """
    if amount is None:
        return Decimal("0.00")
    return (Decimal(str(amount)) * Decimal(str(rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP)

