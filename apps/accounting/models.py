"""Double-entry accounting models."""
import datetime
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
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


def closed_period_refusal(period, posting_date, entry_type=""):
    """Why nothing may be posted on `posting_date` into `period`, or "".

    Closed means closed. Closing a month is the finance manager saying its
    figures are final: the profit has been carried to current year profit,
    the VAT return drawn from it, the management accounts sent. A journal, an
    invoice, a payroll run or a reversal dated inside it afterwards would
    change a month that every report has already printed, without anyone who
    signed it off being told. So the posting is refused, and the bookkeeper
    is told the two ways forward: have the period reopened, or date the entry
    in a period that is still open.

    Only the closing machinery itself passes a closed (not locked) month: the
    year end is drawn on the last day of a month that its own close has just
    shut, and reopening a month reverses its closing entry while the month is
    still closed. A closed fiscal year, whose periods are locked, takes
    nothing at all until the year is reopened.
    """
    if period is None:
        return ""
    if period.status == "LOCKED" or period.fiscal_year.is_closed:
        # A closed year has been swept into retained earnings. Anything
        # posted into it now would sit in the profit and loss accounts of
        # a year whose result has already been carried forward.
        return (f"{period.fiscal_year.name} is closed, and {posting_date:%d %b %Y} "
                "falls inside it. A finance manager has to reopen the fiscal year "
                "first, or date the entry in an open year.")
    if period.status == "CLOSED" and entry_type != "CLOSING":
        return (f"{period.name} is closed, and {posting_date:%d %b %Y} falls inside it. "
                "A closed period takes no new postings: ask a finance manager to reopen "
                f"{period.name} from Accounting → Fiscal Years, or date the entry in an "
                "open period.")
    return ""


# The window a posting date has to fall in. Nothing this book will ever hold
# happened before 1950, and nothing is booked more than five years ahead: a
# date outside it is a slip of the keyboard (0001-01-01, 9999-12-31), and
# posted it would sit outside every report anyone runs, or before every year
# the book will ever close.
EARLIEST_BOOK_DATE = datetime.date(1950, 1, 1)
YEARS_AHEAD = 5


def latest_book_date(today=None):
    """The last day a posting or a fiscal year may reach: today plus five years."""
    today = today or timezone.localdate()
    try:
        return today.replace(year=today.year + YEARS_AHEAD)
    except ValueError:  # 29 February, five years on, is 28 February
        return today.replace(year=today.year + YEARS_AHEAD, day=28)


def book_date_refusal(on_date, what="An entry"):
    """Why `on_date` cannot be a date in this book at all, or ""."""
    if on_date is None:
        return ""
    if on_date < EARLIEST_BOOK_DATE:
        return (f"{what} dated {on_date:%d %b %Y}: that is before {EARLIEST_BOOK_DATE:%Y}, "
                "which no book kept here reaches back to. Check the year and enter "
                "the real date.")
    latest = latest_book_date()
    if on_date > latest:
        return (f"{what} dated {on_date:%d %b %Y}: that is more than {YEARS_AHEAD} years "
                f"ahead (the latest date accepted is {latest:%d %b %Y}). Check the "
                "year and enter the real date.")
    return ""


def uncovered_date_refusal(company, posting_date):
    """Why a date no accounting period covers still may not be posted to, or "".

    A date outside every fiscal year is normally posted with a calendar
    warning: a new book whose bookkeeper has not yet created this year must
    still be able to invoice and pay. But a date on or before the end of the
    latest *closed* year is different. That year's close has already carried
    everything before it into retained earnings and its balance sheet has been
    reported; a journal dated the day before the first fiscal year (or in a
    gap between years) would change that closing balance sheet without
    touching the year, and since no year can be created under a closed one,
    it could never be closed itself. So such a date is refused outright.
    """
    from apps.organizations.models import FiscalYear

    closed = (FiscalYear.objects.filter(company=company, is_closed=True)
              .order_by("-end_date").first())
    if closed and posting_date <= closed.end_date:
        return (f"{posting_date:%d %b %Y} is on or before the end of {closed.name} "
                f"({closed.end_date:%d %b %Y}), which is closed, and no open period "
                "covers it. Posting it would change the balance sheet that year end "
                f"has already carried forward. Date the entry after "
                f"{closed.end_date:%d %b %Y}, or have a finance manager reopen "
                f"{closed.name} and create a fiscal year around the date first.")
    return ""


def posting_refusal(entry, period):
    """The calendar and chart rules every posting meets, as one message, or "".

    Kept apart from JournalEntry.post so the rules can grow without the
    posting method itself changing: the date has to be a real book date, a
    date outside every fiscal year may not reach behind a closed year, and
    every line has to name an account that is still in use. A draft keeps the
    account it was typed against, so an account retired after the draft was
    saved would otherwise be posted to without anyone choosing it again.
    Closing machinery is exempt from the last rule: a period close only
    empties accounts, and a retired account has to be empty anyway.
    """
    refusal = book_date_refusal(entry.posting_date)
    if refusal:
        return refusal
    if period is None:
        refusal = uncovered_date_refusal(entry.company, entry.posting_date)
        if refusal:
            return refusal
    if entry.entry_type != "CLOSING":
        retired = sorted({
            f"{line.account.code} {line.account.name}"
            for line in entry.lines.select_related("account").filter(account__is_active=False)
        })
        if retired:
            return (f"{', '.join(retired)} {'is' if len(retired) == 1 else 'are'} retired, "
                    "so nothing more may be posted to it. Change the line to an account "
                    "in use, or bring the account back from the chart's Retired filter.")
    return ""


# Accounts the ledger's own machinery finds by code: the closes, the receivable
# and payable control accounts, the exchange, revaluation, stock, VAT,
# depreciation and disposal accounts. Retiring one does not stop the
# machinery: it silently falls back on "the first account of that type", so a
# month's profit lands in 3000 instead of current year profit, or an invoice
# finds no receivable at all and is left as a draft. They are marked
# is_system when the chart is seeded, and the codes are kept here as well so
# books seeded before the flag existed are protected the same way. Cash and
# bank (1100, 1200) are not on the list: a bank's ledger account is already
# held by the bank account mapped to it, and an emptied till may be retired.
SYSTEM_ACCOUNT_CODES = frozenset({
    "1300", "1350", "1400", "1450", "1690",
    "2100", "2110", "2150", "2200", "2210",
    "3100", "3200", "3300",
    "4100", "4310", "4320", "4350",
    "5000", "5300",
    "6900", "6950",
    "7300", "7310",
})


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
    # Set by the chart seeder on the accounts the ledger posts to by itself
    # (see SYSTEM_ACCOUNT_CODES). Such an account can be renamed but never
    # retired.
    is_system = models.BooleanField(default=False)

    class Meta:
        unique_together = [("company", "code")]
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"

    @property
    def is_protected(self):
        return self.is_system or self.code in SYSTEM_ACCOUNT_CODES

    def default_uses(self):
        """Where this account is set up as the account something posts to.

        Not the history (a retired account keeps that), but live settings: a
        tax rate, a product, a statutory deduction, an asset or expense
        category, a salary component, a customer's or supplier's own control
        account. Each would go on posting to the account after it was retired,
        or fall back on some other account nobody chose.
        """
        from django.apps import apps as registry

        found = []
        checks = (
            ("accounting.TaxRate", ("account",), "tax rate"),
            ("inventory.Product", ("income_account", "cogs_account", "inventory_account"),
             "product"),
            ("payroll.StatutoryRule", ("payable_account", "expense_account"),
             "statutory deduction"),
            ("payroll.SalaryComponent", ("gl_account",), "salary component"),
            ("fixed_assets.AssetCategory",
             ("gl_asset_account", "gl_depreciation_account", "gl_accumulated_account"),
             "asset category"),
            ("expenses.ExpenseCategory", ("gl_account",), "expense category"),
            ("crm.Customer", ("receivable_account",), "customer"),
            ("crm.Supplier", ("payable_account",), "supplier"),
        )
        for label, fields, noun in checks:
            try:
                model = registry.get_model(label)
            except LookupError:
                continue
            query = models.Q()
            for field in fields:
                query |= models.Q(**{field: self})
            rows = model._default_manager.filter(query)
            if any(f.name == "is_active" for f in model._meta.fields):
                rows = rows.filter(is_active=True)
            if label == "accounting.TaxRate":
                rows = rows.filter(models.Q(effective_to__isnull=True)
                                   | models.Q(effective_to__gte=timezone.localdate()))
            names = [str(row) for row in rows[:3]]
            if names:
                found.append(f"{noun} {', '.join(names)}"
                             + (" and others" if rows.count() > 3 else ""))
        return found

    def retire_refusal(self):
        """Why this account cannot be retired, or ""."""
        if self.is_protected:
            return (f"{self.code} {self.name} is one of the accounts the ledger posts to "
                    "by itself (closes, receivables and payables, exchange, stock, tax "
                    "or depreciation), so it cannot be retired. Rename it if the name "
                    "does not suit; post to another account by choosing it on the "
                    "document instead.")
        uses = self.default_uses()
        if uses:
            return (f"{self.code} is still set up as the account for "
                    f"{'; '.join(uses)}. Point those at another account first, then "
                    "retire it.")
        return ""

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
    # OPENING is not on the list. A book is opened once, by the finance
    # manager, on the opening-balances screen, which refuses a second live
    # opening entry. Typed on the journal screen it slipped past that check:
    # an accountant could post the share capital and the bank balance a second
    # time, and the balance sheet doubled without a single document behind it.
    USER_ENTRY_TYPES = ("MANUAL",)

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
        "OPENING": "Opening balances are reversed by the finance manager from "
                   "Accounting → Opening Balances, which then takes a fresh set.",
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
        # A date no fiscal year covers is still posted, with the calendar warning
        # the caller shows: refusing it would stop every invoice, till and payroll
        # in a book whose bookkeeper has not yet opened this year. The year close
        # sweeps it once the year is created around it. Not so a date on or
        # before the end of a closed year, an impossible date, or a line on a
        # retired account: see posting_refusal.
        refusal = (posting_refusal(self, period)
                   or closed_period_refusal(period, self.posting_date, self.entry_type))
        if refusal:
            raise ValidationError(refusal)
        with transaction.atomic():
            self.period = period
            self.status = "POSTED"
            self.posted_by = user
            self.posted_at = timezone.now()
            self.save(update_fields=[
                "status", "posted_by", "posted_at", "period", "updated_at",
            ])
            self._cash_floor_hook()

    def _cash_floor_hook(self):
        """A cash box cannot go below zero (gl_service, "The cash floor").

        Asked here, as every entry posts -- a reversal is posted like any other
        entry, so reverse() is covered too -- so no screen can forget it. Kept
        a separate hook so the other rules on posting stay apart from it. A
        refusal undoes the post: the status written above is rolled back with
        the atomic block, and the object is put back to what it was.
        """
        from apps.accounting import gl_service
        try:
            gl_service.check_cash_floor_on_post(self)
        except ValidationError:
            self.status = "DRAFT"
            self.posted_by = None
            self.posted_at = None
            raise

    @property
    def calendar_warning(self):
        """Why this entry deserves a second look, in the bookkeeper's terms.

        A draft has no period yet -- the period is stamped on it when it is
        posted -- so its warning is worked out from the period its date falls
        in, the one it *would* be posted into. Reading the empty field instead
        told every draft that no period covered it, which taught bookkeepers to
        ignore the banner on the day it mattered. A draft dated in a closed
        period is told so before it is refused at Post.
        """
        period = self.period if self.period_id else (
            period_for(self.company, self.posting_date)
            if self.company_id and self.posting_date else None)
        if period is None:
            return f"No accounting period covers {self.posting_date:%d %b %Y}."
        if period.fiscal_year.is_closed:
            return f"{period.fiscal_year.name} is closed and this entry is dated inside it."
        if period.status != "OPEN":
            return (f"{period.name} is {period.get_status_display().lower()} "
                    f"and this entry is dated inside it.")
        return ""

    @property
    def calendar_advice(self):
        """The calendar warning with what to do about it, for the entry's page."""
        warning = self.calendar_warning
        if not warning:
            return ""
        if warning.startswith("No accounting period"):
            return (f"{warning} It is posted without a period, and no year end "
                    "closes it until a fiscal year is created around its date under "
                    "Accounting → Fiscal Years.")
        if self.status == "DRAFT":
            return (f"{warning} A closed period takes no new postings, so this draft "
                    "will be refused at Post: ask a finance manager to reopen the "
                    "period, or change the date to one in an open period.")
        return (f"{warning} It was posted before the period was closed. A closed "
                "period takes no new postings, so to change anything in it a finance "
                "manager has to reopen the period first.")

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
            kind = self.get_entry_type_display().lower()
            article = "an" if kind[:1] in "aeiou" else "a"
            return (f"{self.reference} was written by the ledger as {article} "
                    f"{kind} entry. "
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
        on_date = posting_date or self.posting_date
        # Asked before anything is written. Cancelling an invoice, a bill or a
        # payroll run reverses its entry in the original period; when that
        # period has since been closed the cancellation is refused with the
        # same words a journal gets, rather than half-made and rolled back.
        target = (self.period if on_date == self.posting_date and self.period_id
                  else period_for(self.company, on_date))
        refusal = closed_period_refusal(
            target, on_date, "CLOSING" if self.entry_type == "CLOSING" else "REVERSAL")
        if refusal:
            raise ValidationError(f"{self.reference} cannot be reversed yet. {refusal}")
        with transaction.atomic():
            from apps.core.models import SequenceCounter
            ref = SequenceCounter.next_number(self.company, "JE-REV-")
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
            # Names the change if the cash floor refuses it (gl_service).
            reversal.cash_floor_doing = f"Reversing {self.reference}"
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
        """What the account actually did in the period, against the budget.

        The year-end closing entry is left out: it empties every revenue and
        expense account into retained earnings on the last day, so counting it
        would bring each P&L account's actual back to nought the moment the
        period is closed -- the budget would read as wholly unspent.
        """
        lines = JournalEntryLine.objects.filter(
            account=self.account,
            journal_entry__status="POSTED",
            journal_entry__posting_date__gte=self.period.start_date,
            journal_entry__posting_date__lte=self.period.end_date,
        ).exclude(journal_entry__entry_type="CLOSING")
        if self.account.normal_balance == "DEBIT":
            return (lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0")) - \
                   (lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0"))
        return (lines.aggregate(t=models.Sum("credit_amount"))["t"] or Decimal("0")) - \
               (lines.aggregate(t=models.Sum("debit_amount"))["t"] or Decimal("0"))

    @property
    def variance(self):
        return self.actual_amount - self.budgeted_amount


# ────────────────────────────── foreign currency ────────────────────────────

MAX_MINOR_UNITS = 4


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
    # No currency in ISO 4217 has more than four (the Uruguayan unidad
    # previsional); a larger figure is a typing slip that would have every
    # amount in that currency rounded to a precision the ledger cannot hold.
    minor_units = models.PositiveSmallIntegerField(
        default=2, validators=[MaxValueValidator(MAX_MINOR_UNITS)])

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code

    def clean(self):
        super().clean()
        import re
        if self.code is not None and not re.fullmatch(r"[A-Z]{3}", self.code):
            raise ValidationError({"code": (
                f"'{self.code}' is not a currency code. Use the three-letter ISO 4217 "
                "code in plain capitals A to Z, such as USD, KES or EUR.")})
        if self.minor_units is not None and not 0 <= self.minor_units <= MAX_MINOR_UNITS:
            raise ValidationError({"minor_units": (
                f"{self.minor_units} digits after the point is not a currency. Use 0 to "
                f"{MAX_MINOR_UNITS}: 2 for shillings and dollars, 0 for yen.")})

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



class RevaluationShare(models.Model):
    """One document's part in an exchange revaluation, as the revaluation saw it.

    A revaluation is one journal for the whole book, but it is the sum of a
    share per open invoice and bill. When a document is later settled, part
    settled or cancelled, the share it contributed has to come back off -- and
    it has to be the share as it was *booked*, not as the document would be
    valued if the revaluation were drawn again today: a receipt keyed in late
    but dated before the revaluation would make a fresh reading show nothing
    owed, while the revaluation entry still carries the full share. So the
    share is written down here when the revaluation is posted, and the
    releases are worked from it.
    """
    revaluation = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="revaluation_shares")
    # "Invoice:<pk>" or "Bill:<pk>", the same key the release entries carry.
    document_key = models.CharField(max_length=80, db_index=True)
    side = models.CharField(max_length=2, choices=[("ar", "Receivable"), ("ap", "Payable")])
    currency = models.CharField(max_length=3)
    # What the document still owed on the revaluation's day, in its own money.
    foreign = models.DecimalField(max_digits=18, decimal_places=2)
    # Revalued less carried, in the book's money: what the revaluation booked
    # for this document on 1350 (receivables) or 2150 (payables).
    difference = models.DecimalField(max_digits=15, decimal_places=2)

    class Meta:
        unique_together = [("revaluation", "document_key")]

    def __str__(self):
        return f"{self.document_key} in {self.revaluation.reference}: {self.difference}"
