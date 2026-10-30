"""Banking: Bank accounts, transactions, reconciliation."""
from decimal import Decimal
from django.db import models
from django.db.models import Q
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
    # The entry that put the opening balance into the ledger: Dr this bank's
    # ledger account / Cr opening equity (3100), or the other way round for an
    # account that opened overdrawn. Without it the bank book starts from the
    # opening balance while the ledger starts from nothing, and the two can
    # never agree. Kept on the account so it is booked once, and so the
    # account knows it has history the moment it is created.
    opening_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="bank_openings",
    )
    # Where the till's card slips and M-Pesa messages are banked. The money a
    # card machine or a Lipa na M-Pesa till takes is settled into one real bank
    # account, and the ticket's entry has to debit that account's ledger
    # account or its statement will never reconcile. At most one account per
    # company carries each flag; with none flagged the till falls back to the
    # Bank ledger account (1200), which is how every company started.
    receives_pos_card = models.BooleanField(
        default=False, verbose_name="Receives POS card takings",
        help_text="The till's card sales are banked here. Only one account per company.")
    receives_pos_mpesa = models.BooleanField(
        default=False, verbose_name="Receives POS M-Pesa takings",
        help_text="The till's M-Pesa sales are banked here. Only one account per company.")

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["company"], condition=Q(receives_pos_card=True),
                name="bank_one_pos_card_account"),
            models.UniqueConstraint(
                fields=["company"], condition=Q(receives_pos_mpesa=True),
                name="bank_one_pos_mpesa_account"),
        ]

    def __str__(self):
        return f"{self.name} ({self.bank_name})"

    @property
    def has_history(self):
        """Has anything been recorded against this account yet?

        A transaction on its bank book, a statement (even a draft or a reversed
        one) or the entry that booked its opening balance. Once any of those
        exists, the ledger account, the currency and the opening balance are
        what that history was written in, and changing them would leave the
        bank book and the ledger telling two different stories.
        """
        if self._state.adding:
            return False
        return bool(self.opening_entry_id) or self.transactions.exists()             or self.reconciliations.exists()

    @property
    def current_balance(self):
        """What the ledger account behind this bank account holds today.

        The ledger is kept in the book's own currency, so for a foreign-currency
        bank account this is the book-currency value — show it labelled with
        the book's currency, never with this account's own.

        As at today, not "every line the book holds": a receipt or payment
        dated next week has not happened yet, and a balance that counted it
        would be a forecast the bank statement can never agree with. Future
        lines are listed separately on the account's page.
        """
        from django.utils import timezone
        return self.gl_account.balance_as_at(timezone.localdate())

    def future_transactions(self):
        """Bank-book lines dated after today -- money that has not moved yet."""
        from django.utils import timezone
        return self.transactions.filter(transaction_date__gt=timezone.localdate())             .order_by("transaction_date", "created_at")

    @property
    def is_foreign(self):
        """Is this account held in a currency other than the book's own?"""
        book = (getattr(self.company, "currency", "") or "").upper()
        return bool(self.currency) and self.currency.upper() != book

    @property
    def statement_balance(self):
        """The balance in the account's own currency, as the bank would print it.

        The opening balance plus every transaction recorded against the account
        up to today — those are entered in the account's own money — which is
        what a USD statement has to be compared with. Dated as at today for the
        same reason as current_balance: the two are compared with each other.
        """
        from django.utils import timezone
        return self.book_balance_at(timezone.localdate())

    def book_balance_at(self, on_date):
        """The bank book's balance at the close of `on_date`, in its own money."""
        from django.db.models import Case, DecimalField, F, Sum, Value, When
        lines = self.transactions.all()
        if on_date is not None:
            lines = lines.filter(transaction_date__lte=on_date)
        signed = lines.aggregate(t=Sum(Case(
            When(transaction_type="CREDIT", then=F("amount")),
            When(transaction_type="DEBIT", then=-F("amount")),
            default=Value(0),
            output_field=DecimalField(max_digits=15, decimal_places=2))))["t"]
        return (self.opening_balance or Decimal("0")) + (signed or Decimal("0"))


class Reconciliation(BaseModel, NotesMixin):
    """A bank statement and the book transactions cleared against it.

    The arithmetic is deliberately the bookkeeper's version: the opening
    balance carried forward, plus every transaction you have cleared, must
    land on the closing balance printed on the statement. The gap between the
    two is the `difference`, and it is what an unreconciled account looks like.
    """
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("POSTED", "Posted"),
        ("REVERSED", "Reversed"),
    ]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="reconciliations")
    bank_account = models.ForeignKey(BankAccount, on_delete=models.PROTECT, related_name="reconciliations")
    reference = models.CharField(max_length=50, db_index=True, help_text="Statement number printed by the bank.")
    statement_date = models.DateField(db_index=True, help_text="Last day covered by the statement.")
    opening_balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    closing_balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    reconciled_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="reconciliations"
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-statement_date", "-created_at"]

    def __str__(self):
        return f"{self.reference} — {self.bank_account.name} ({self.statement_date})"

    @property
    def cleared_movement(self):
        """Net of the transactions cleared against this statement."""
        from django.db.models import Sum, Case, When, Value, DecimalField, F
        totals = self.transactions.aggregate(
            credit=Sum(Case(When(transaction_type="CREDIT", then=F("amount")), default=Value(0),
                            output_field=DecimalField(max_digits=15, decimal_places=2))),
            debit=Sum(Case(When(transaction_type="DEBIT", then=F("amount")), default=Value(0),
                           output_field=DecimalField(max_digits=15, decimal_places=2))),
        )
        return (totals["credit"] or Decimal("0")) - (totals["debit"] or Decimal("0"))

    @property
    def cleared_count(self):
        return self.transactions.count()

    @property
    def computed_balance(self):
        """What the book says the account holds after the cleared items."""
        return self.opening_balance + self.cleared_movement

    @property
    def difference(self):
        return self.closing_balance - self.computed_balance

    @property
    def is_balanced(self):
        return abs(self.difference) < Decimal("0.01")

    @property
    def is_locked(self):
        return self.status == "POSTED"


class BankTransaction(BaseModel, NotesMixin):
    """One line of the bank book: a movement the bank statement should show.

    Every line has a ledger entry behind it, and where it came from decides
    who may change it:

    * MANUAL   — typed on the banking screen (bank charges, interest, the
                 owner putting capital in). The screen wrote its own entry,
                 Dr bank / Cr the counter account for a deposit and the other
                 way round for a withdrawal, so the screen may re-book it while
                 it is uncleared and reverse it when it is deleted.
    * TRANSFER — one side of a transfer between two of the company's own bank
                 or cash accounts. Both sides share one entry, so they are
                 removed together.
    * LEDGER   — fed from an entry some other screen posted: a customer
                 receipt, a supplier payment, a till's card takings, a payroll
                 run, an expense claim, a hand-typed journal. The document that
                 made the entry owns it; the bank book only mirrors it so the
                 statement can be ticked against it. It is corrected at its
                 source, and disappears again when that entry is reversed
                 before the bank has seen it.
    """
    TRANSACTION_TYPES = [
        ("CREDIT", "Credit / Inflow"),
        ("DEBIT", "Debit / Outflow"),
    ]
    ORIGINS = [
        ("MANUAL", "Entered on the banking screen"),
        ("TRANSFER", "Transfer between own accounts"),
        ("LEDGER", "From a posted ledger entry"),
    ]
    origin = models.CharField(max_length=8, choices=ORIGINS, default="MANUAL", db_index=True)
    # The other side of a manual deposit or withdrawal: the expense a bank
    # charge is, the income interest is, the equity or loan the money came
    # from. Kept on the line so an edit re-books against the same account.
    counter_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, null=True, blank=True,
        related_name="bank_counter_lines",
    )
    bank_account = models.ForeignKey(BankAccount, on_delete=models.CASCADE, related_name="transactions")
    transaction_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    transaction_type = models.CharField(max_length=6, choices=TRANSACTION_TYPES)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    description = models.CharField(max_length=500, blank=True, default="")
    reference = models.CharField(max_length=200, blank=True, default="")
    is_reconciled = models.BooleanField(default=False, db_index=True)
    reconciliation = models.ForeignKey(
        Reconciliation, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions"
    )
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )
    running_balance = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-transaction_date", "-created_at"]

    def __str__(self):
        return f"{self.transaction_type} {self.amount} — {self.transaction_date}"

    @property
    def signed_amount(self):
        """Inflow positive, outflow negative -- how the account balance moves."""
        return self.amount if self.transaction_type == "CREDIT" else -self.amount
