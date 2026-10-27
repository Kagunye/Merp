"""Procurement models: Purchase Requisition, PO, Bill, Payment."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class PurchaseRequisition(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("PENDING_APPROVAL", "Pending Approval"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("PO_CREATED", "PO Created"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    reference = models.CharField(max_length=50, db_index=True)
    requested_by = models.ForeignKey("accounts.User", on_delete=models.PROTECT, related_name="requisitions")
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL, null=True, blank=True)
    required_date = models.DateField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_requisitions"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.reference


class PurchaseRequisitionLine(models.Model):
    requisition = models.ForeignKey(PurchaseRequisition, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    estimated_unit_cost = models.DecimalField(max_digits=15, decimal_places=4, default=Decimal("0"))
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)

    @property
    def estimated_total(self):
        return self.quantity * self.estimated_unit_cost


class PurchaseOrder(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("SENT", "Sent to Supplier"),
        ("PARTIALLY_RECEIVED", "Partially Received"),
        ("FULLY_RECEIVED", "Fully Received"),
        ("BILLED", "Billed"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="purchase_orders")
    reference = models.CharField(max_length=50, db_index=True)
    supplier = models.ForeignKey("crm.Supplier", on_delete=models.PROTECT, related_name="purchase_orders")
    requisition = models.ForeignKey(PurchaseRequisition, on_delete=models.SET_NULL, null=True, blank=True)
    order_date = models.DateField(db_index=True)
    expected_delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)
    total_amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-order_date", "-created_at"]

    def __str__(self):
        return self.reference

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.order_date)

    @property
    def rate(self):
        """The booked rate, or 1 for an order kept in the book currency."""
        from apps.accounting.currency import document_rate
        return document_rate(self, self.order_date)

    def calculate_totals(self):
        """Roll the lines up into the header, in the order's own currency."""
        from apps.accounting.currency import to_base
        lines = self.lines.all()
        self.subtotal = sum((l.line_total for l in lines), Decimal("0"))
        self.tax_amount = sum((l.tax_amount for l in lines), Decimal("0"))
        self.total_amount = self.subtotal + self.tax_amount
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        self.save(update_fields=[
            "subtotal", "tax_amount", "total_amount", "total_amount_base", "updated_at",
        ])


class PurchaseOrderLine(models.Model):
    order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    description = models.CharField(max_length=500, blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_cost = models.DecimalField(max_digits=15, decimal_places=4)
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    received_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    billed_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))

    @property
    def line_total(self):
        return self.quantity * self.unit_cost

    @property
    def tax_amount(self):
        if not self.tax_rate:
            return Decimal("0")
        return self.line_total * (self.tax_rate.rate / 100)

    @property
    def to_receive(self):
        """Ordered quantity the goods receipt has not yet taken in."""
        return self.quantity - self.received_quantity

    @property
    def to_bill(self):
        """Received quantity the supplier bill has not yet asked to be paid for.

        Billing is measured against what arrived rather than what was ordered:
        paying for goods that never came is how a payables ledger drifts.
        """
        return min(self.quantity, self.received_quantity) - self.billed_quantity

    @property
    def tax_amount(self):
        if not self.tax_rate:
            return Decimal("0")
        return self.line_total * (self.tax_rate.rate / 100)

    @property
    def outstanding_quantity(self):
        """What the supplier still owes on this line."""
        return self.quantity - self.received_quantity

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class Bill(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("RECEIVED", "Received / Outstanding"),
        ("PARTIAL", "Partially Paid"),
        ("PAID", "Paid"),
        ("OVERDUE", "Overdue"),
        ("CANCELLED", "Cancelled"),
        ("DEBIT_NOTE", "Debit Note"),
        ("DEBITED", "Settled by debit note"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="bills")
    reference = models.CharField(max_length=50, db_index=True)
    supplier_reference = models.CharField(max_length=100, blank=True, default="")
    supplier = models.ForeignKey("crm.Supplier", on_delete=models.PROTECT, related_name="bills")
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="bills")
    bill_date = models.DateField(db_index=True)
    due_date = models.DateField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    currency = models.CharField(max_length=3, default="KES")
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)
    total_amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    outstanding_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)
    # Returning goods, or a price the supplier agreed to reduce: a debit note
    # answers the bill and both stay in the book.
    original_bill = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="debit_notes")
    credited_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    # The part of this bill that was already in the warehouse when it arrived:
    # stock costed on receipt against the received-not-invoiced accrual, which
    # this bill releases instead of charging to stock twice. Kept in book money
    # so the order's accrual can be worked out from what is left.
    accrued_cleared = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-bill_date", "-created_at"]

    def __str__(self):
        return self.reference

    @property
    def rate(self):
        """The booked rate, or 1 for a bill kept in the book currency."""
        from apps.accounting.currency import document_rate
        return document_rate(self, self.bill_date)

    @property
    def outstanding_base(self):
        """What is still owed in the ledger's own currency."""
        from apps.accounting.currency import to_base
        return to_base(self.outstanding_amount, self.rate)

    def save(self, *args, **kwargs):
        from apps.accounting.models import to_base

        # A cancelled bill is no longer owed, whatever its total said.
        if self.status == "CANCELLED":
            self.outstanding_amount = Decimal("0")
        else:
            self.outstanding_amount = self.total_amount - self.paid_amount - self.credited_amount
        # The book-currency value follows the bill's own money and its booked rate.
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        # Only a bill the ledger already carries can settle itself. A draft is
        # not yet a payable, and a debit note keeps its own name: it answers a
        # payable rather than being one.
        if self.total_amount > 0 and self.status in ("RECEIVED", "PARTIAL", "OVERDUE"):
            if self.outstanding_amount <= 0:
                self.status = "PAID" if self.paid_amount > 0 else "DEBITED"
            elif self.paid_amount > 0:
                self.status = "PARTIAL"
        super().save(*args, **kwargs)

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.bill_date)
