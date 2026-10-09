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
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-order_date", "-created_at"]

    def __str__(self):
        return self.reference


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


class Bill(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("RECEIVED", "Received / Outstanding"),
        ("PARTIAL", "Partially Paid"),
        ("PAID", "Paid"),
        ("OVERDUE", "Overdue"),
        ("CANCELLED", "Cancelled"),
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
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    outstanding_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-bill_date", "-created_at"]

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        self.outstanding_amount = self.total_amount - self.paid_amount
        super().save(*args, **kwargs)
