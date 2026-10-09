"""Sales models: Quotation, Sales Order, Invoice, Payment."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class SalesOrder(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("CONFIRMED", "Confirmed"),
        ("PARTIALLY_DELIVERED", "Partially Delivered"),
        ("DELIVERED", "Fully Delivered"),
        ("INVOICED", "Invoiced"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="sales_orders")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="sales_orders")
    order_date = models.DateField(db_index=True)
    delivery_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=25, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    sales_rep = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_orders"
    )
    currency = models.CharField(max_length=3, default="KES")
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-order_date", "-created_at"]

    def __str__(self):
        return self.reference

    def calculate_totals(self):
        lines = self.lines.all()
        subtotal = sum(l.line_total for l in lines)
        discount = subtotal * (self.discount_percent / 100)
        taxable = subtotal - discount
        tax = sum(l.tax_amount for l in lines)
        self.subtotal = subtotal
        self.tax_amount = tax
        self.total_amount = taxable + tax
        self.save(update_fields=["subtotal", "tax_amount", "total_amount", "updated_at"])


class SalesOrderLine(models.Model):
    order = models.ForeignKey(SalesOrder, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    description = models.CharField(max_length=500, blank=True, default="")
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    delivered_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))
    invoiced_quantity = models.DecimalField(max_digits=12, decimal_places=4, default=Decimal("0"))

    @property
    def line_total(self):
        base = self.quantity * self.unit_price
        disc = base * (self.discount_percent / 100)
        return base - disc

    @property
    def tax_amount(self):
        if not self.tax_rate:
            return Decimal("0")
        return self.line_total * (self.tax_rate.rate / 100)

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


class Invoice(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("SENT", "Sent / Outstanding"),
        ("PARTIAL", "Partially Paid"),
        ("PAID", "Paid"),
        ("OVERDUE", "Overdue"),
        ("CANCELLED", "Cancelled"),
        ("CREDIT_NOTE", "Credit Note"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="invoices")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="invoices")
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    invoice_date = models.DateField(db_index=True)
    due_date = models.DateField()
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    currency = models.CharField(max_length=3, default="KES")
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    paid_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    outstanding_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-invoice_date", "-created_at"]

    def __str__(self):
        return self.reference

    def save(self, *args, **kwargs):
        self.outstanding_amount = self.total_amount - self.paid_amount
        if self.outstanding_amount <= 0:
            self.status = "PAID"
        elif self.paid_amount > 0:
            self.status = "PARTIAL"
        super().save(*args, **kwargs)


class InvoiceLine(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=500)
    quantity = models.DecimalField(max_digits=12, decimal_places=4)
    unit_price = models.DecimalField(max_digits=15, decimal_places=4)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)

    @property
    def line_total(self):
        base = self.quantity * self.unit_price
        disc = base * (self.discount_percent / 100)
        return base - disc

    @property
    def tax_amount(self):
        if not self.tax_rate:
            return Decimal("0")
        return self.line_total * (self.tax_rate.rate / 100)


class CustomerPayment(BaseModel, NotesMixin):
    PAYMENT_METHODS = [
        ("CASH", "Cash"),
        ("BANK_TRANSFER", "Bank Transfer"),
        ("CHEQUE", "Cheque"),
        ("MPESA", "M-Pesa"),
        ("CARD", "Card"),
        ("OTHER", "Other"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="customer_payments")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default="CASH")
    bank_account = models.ForeignKey("banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True)
    transaction_reference = models.CharField(max_length=200, blank=True, default="")
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")

    class Meta:
        ordering = ["-payment_date", "-created_at"]

    def __str__(self):
        return f"{self.reference} — {self.customer.name} KES {self.amount}"


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(CustomerPayment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="allocations")
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.payment.reference} → {self.invoice.reference}: {self.allocated_amount}"
