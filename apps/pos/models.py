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
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    line_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"

    @property
    def net_total(self):
        """The line before tax — what the revenue account is credited with."""
        return (self.line_total or Decimal("0")) - (self.tax_amount or Decimal("0"))
