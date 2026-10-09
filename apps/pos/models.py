"""Point of Sale models."""
from decimal import Decimal
from django.db import models
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

    def __str__(self):
        return f"POS Session — {self.cashier} ({self.opened_at.date()})"


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
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    payment_method = models.CharField(max_length=10, choices=PAYMENT_METHODS, default="CASH")
    amount_tendered = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    change_given = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="COMPLETED")
    invoice = models.ForeignKey("sales.Invoice", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference


class POSSaleLine(models.Model):
    sale = models.ForeignKey(POSSale, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    line_total = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"
