"""Point of Sale models."""
from decimal import Decimal
from django.db import models
from django.db.models import Q
from apps.core.models import BaseModel, NotesMixin


class POSSession(BaseModel):
    STATUS_CHOICES = [("OPEN", "Open"), ("CLOSING", "Closing"), ("CLOSED", "Closed")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    branch = models.ForeignKey("organizations.Branch", on_delete=models.CASCADE)
    cashier = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="pos_sessions")
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="OPEN")
    opening_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    closing_cash = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_sales = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_refunds = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    # Who actually shut the drawer. Usually the cashier; when an administrator
    # closes a till on someone's behalf the cash-up has to be able to say so,
    # because the count on it is then theirs and not the cashier's.
    closed_by = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                  blank=True, related_name="pos_sessions_closed")
    # The entry that wrote a short or over drawer off to cash over/short. Held on
    # the session so a second close or a late count can see the variance is
    # already in the book and never books it twice.
    variance_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="pos_variances")

    def __str__(self):
        return f"POS Session — {self.cashier} ({self.opened_at.date()})"

    @property
    def variance(self):
        """What the counted drawer is away from the tickets. None until counted."""
        if self.closing_cash is None:
            return None
        return (self.closing_cash or Decimal("0")) - (self.expected_cash or Decimal("0"))


class POSSale(BaseModel, NotesMixin):
    PAYMENT_METHODS = [
        ("CASH", "Cash"),
        ("CARD", "Card"),
        ("MPESA", "M-Pesa"),
        ("BANK", "Bank Transfer"),
        ("SPLIT", "Split Payment"),
    ]
    STATUS_CHOICES = [("COMPLETED", "Completed"), ("REFUNDED", "Refunded"), ("PARTIAL_REFUND", "Partial Refund")]

    session = models.ForeignKey(POSSession, on_delete=models.CASCADE, related_name="sales")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.SET_NULL, null=True, blank=True)
    # The shelf the goods came off, recorded on the ticket: a refund has to put
    # them back where they were taken from, and a cash-up has to be able to say
    # which warehouse the day's trading emptied.
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL,
                                  null=True, blank=True, related_name="pos_sales")
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default="CASH")
    amount_tendered = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="COMPLETED")
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True)
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="pos_sales")
    # A refund is its own ticket rather than an edit of the sale: the money that
    # went over the counter in the morning and came back in the afternoon are two
    # events, and the till has to be able to prove both.
    refund_of = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="refunds")
    refunded_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    # The one-time token the terminal or refund form was rendered with. A
    # browser that submits the same form twice (a double click, a reload after a
    # slow response) sends the same token, and the till hands back the ticket
    # the first submission made instead of taking the money a second time.
    submission_token = models.CharField(max_length=64, blank=True, default="", db_index=True)
    # Who served the customer, picked on the till's top bar. Display only: the
    # drawer and the cash-up stay the cashier's, but a sales report can credit
    # the person on the floor who made the sale.
    salesperson = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True,
                                    blank=True, related_name="pos_sales_served")
    # The customer's own reference (an LPO number, a table, a phone number),
    # typed on the top bar. Not the ticket number -- that is `reference`.
    customer_reference = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "submission_token"],
                condition=~Q(submission_token=""),
                name="pos_one_ticket_per_submission",
            ),
        ]

    def __str__(self):
        return self.reference

    @property
    def outstanding_refund(self):
        """What is still returnable on this ticket."""
        return (self.total_amount or Decimal("0")) - (self.refunded_amount or Decimal("0"))


class POSSaleLine(models.Model):
    sale = models.ForeignKey(POSSale, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL,
                                 null=True, blank=True, related_name="pos_sale_lines")
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    # The line discount a supervisor gave, as a percentage of the shelf price,
    # and what it took off the line before tax (discount_amount).
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    line_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    @property
    def net_total(self):
        """The line before tax — what the revenue account is credited with."""
        return (self.line_total or Decimal("0")) - (self.tax_amount or Decimal("0"))


class POSCashMovement(BaseModel):
    """Money put into or taken out of a drawer that is not a sale or a refund.

    A payout (OUT) is notes handed over the counter for something the shop
    paid for -- a delivery boy's fare, milk for the office -- or a cash drop
    to the safe. A pay-in (IN) is a float top-up mid-shift. Either way the
    drawer's expected cash moves with it, and the ledger is told at once:

        payout   DR the counter account (an expense, or the bank/safe)   CR the till (1100)
        pay-in   DR the till (1100)   CR the counter account (float equity, the bank/safe)
    """
    DIRECTIONS = [("OUT", "Payout"), ("IN", "Pay-in")]

    session = models.ForeignKey(POSSession, on_delete=models.CASCADE, related_name="cash_movements")
    reference = models.CharField(max_length=50, db_index=True)
    direction = models.CharField(max_length=3, choices=DIRECTIONS)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    account = models.ForeignKey("accounting.Account", on_delete=models.PROTECT,
                                related_name="pos_cash_movements")
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL,
                                      null=True, blank=True, related_name="pos_cash_movements")
    performed_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT,
                                     related_name="pos_cash_movements")
    # One-time form token, as on a ticket: the same payout form submitted
    # twice takes the notes out of the drawer once.
    submission_token = models.CharField(max_length=64, blank=True, default="", db_index=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "submission_token"],
                condition=~Q(submission_token=""),
                name="pos_one_cash_movement_per_submission",
            ),
        ]

    def __str__(self):
        return f"{self.reference} {self.get_direction_display()} {self.amount}"
