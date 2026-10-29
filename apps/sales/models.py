"""Sales models: Quotation, Sales Order, Invoice, Payment."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class SalesQuote(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("SENT", "Sent"),
        ("ACCEPTED", "Accepted"),
        ("EXPIRED", "Expired"),
        ("CANCELLED", "Cancelled"),
        ("CONVERTED", "Converted to Order"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="sales_quotes")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="sales_quotes")
    quote_date = models.DateField(db_index=True)
    valid_until = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    warehouse = models.ForeignKey("organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True)
    sales_rep = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="sales_quotes"
    )
    currency = models.CharField(max_length=3, default="KES")
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)
    total_amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)
    sales_order = models.ForeignKey("sales.SalesOrder", on_delete=models.SET_NULL, null=True, blank=True, related_name="source_quotes")

    class Meta:
        ordering = ["-quote_date", "-created_at"]

    def __str__(self):
        return self.reference

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.quote_date)

    def calculate_totals(self):
        from apps.accounting.currency import to_base
        lines = self.lines.all()
        subtotal = sum((l.line_total for l in lines), Decimal("0"))
        discount = subtotal * (self.discount_percent / 100)
        taxable = subtotal - discount
        kept = (Decimal("100") - self.discount_percent) / 100
        tax = sum((l.tax_amount * kept for l in lines), Decimal("0"))
        self.subtotal = subtotal
        self.tax_amount = tax
        self.total_amount = taxable + tax
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        self.save(update_fields=[
            "subtotal", "tax_amount", "total_amount", "total_amount_base", "updated_at",
        ])


class SalesQuoteLine(models.Model):
    quote = models.ForeignKey(SalesQuote, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey("inventory.Product", on_delete=models.PROTECT)
    description = models.CharField(max_length=500, blank=True, default="")
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

    def __str__(self):
        return f"{self.product.name} x {self.quantity}"


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
    # The rate is captured on the document, not looked up later: a March
    # invoice must still read as March money when it is paid in June.
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)
    total_amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("0"))
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-order_date", "-created_at"]

    def __str__(self):
        return self.reference

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.order_date)

    def calculate_totals(self):
        from apps.accounting.currency import to_base
        lines = self.lines.all()
        subtotal = sum((l.line_total for l in lines), Decimal("0"))
        discount = subtotal * (self.discount_percent / 100)
        taxable = subtotal - discount
        # VAT is charged on what the customer actually pays for the goods, so
        # the order's own discount comes off before the rate is applied. Taxing
        # the undiscounted value would bill tax on money being forgiven.
        kept = (Decimal("100") - self.discount_percent) / 100
        tax = sum((l.tax_amount * kept for l in lines), Decimal("0"))
        self.subtotal = subtotal
        self.tax_amount = tax
        self.total_amount = taxable + tax
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        self.save(update_fields=[
            "subtotal", "tax_amount", "total_amount", "total_amount_base", "updated_at",
        ])


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

    @property
    def to_deliver(self):
        """What the warehouse still owes against this line."""
        return max(self.quantity - (self.delivered_quantity or Decimal("0")),
                   Decimal("0"))

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
        ("CREDITED", "Settled by credit note"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="invoices")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="invoices")
    sales_order = models.ForeignKey(SalesOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    invoice_date = models.DateField(db_index=True)
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
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True
    )
    # A credit note answers the invoice it refers to. Both stay in the book and
    # net against each other; nothing is ever deleted to fix a mistake.
    original_invoice = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="credit_notes")
    credited_amount = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-invoice_date", "-created_at"]

    def __str__(self):
        return self.reference

    @property
    def rate(self):
        """The booked rate, or 1 for a document kept in the book currency."""
        from apps.accounting.currency import document_rate
        return document_rate(self, self.invoice_date)

    @property
    def known_rate(self):
        """The rate if one is booked or filed, else None — for showing, not posting."""
        from django.core.exceptions import ValidationError
        try:
            return self.rate
        except ValidationError:
            return None

    @property
    def outstanding_base(self):
        """What is still owed in the ledger's own currency."""
        from apps.accounting.currency import display_rate, to_base
        return to_base(self.outstanding_amount, display_rate(self, self.invoice_date))

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.invoice_date)

    def save(self, *args, **kwargs):
        from apps.accounting.models import to_base

        self.outstanding_amount = self.total_amount - self.paid_amount - self.credited_amount
        # The lines are only written after the header is saved, so the form's
        # clean() cannot know the total yet; the book-currency value is kept in
        # step with the foreign total here instead.
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        # A draft with no lines yet has nothing to settle, so it must not be
        # reported as settled, and only an outstanding invoice can change its
        # own name. A credit note is not a receivable either — it is the answer
        # to one, so it keeps its own identity.
        # PAID and CREDITED are in the list because a receipt can be given back
        # (a bounced cheque, a reversal): the paper that was called settled is
        # owed again, and an invoice that stayed PAID with a balance on it would
        # be a statement the ledger contradicts.
        if self.total_amount > 0 and self.status in (
                "SENT", "PARTIAL", "OVERDUE", "PAID", "CREDITED"):
            if self.outstanding_amount <= 0:
                self.status = "PAID" if self.paid_amount > 0 else "CREDITED"
            elif self.paid_amount > 0:
                self.status = "PARTIAL"
            else:
                self.status = "SENT"
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
    # A reversed payment is money the bank gave back — a bounced cheque, a
    # cancelled cheque, a receipt typed against the wrong customer. The entry is
    # offset, never deleted, and this flag is what stops a second reversal of
    # the same receipt.
    STATUS_CHOICES = [("RECEIVED", "Received"), ("REVERSED", "Reversed")]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="customer_payments")
    reference = models.CharField(max_length=50, db_index=True)
    customer = models.ForeignKey("crm.Customer", on_delete=models.PROTECT, related_name="payments")
    payment_date = models.DateField(db_index=True)
    amount = models.DecimalField(max_digits=15, decimal_places=2)
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHODS, default="CASH")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="RECEIVED")
    bank_account = models.ForeignKey("banking.BankAccount", on_delete=models.SET_NULL, null=True, blank=True)
    transaction_reference = models.CharField(max_length=200, blank=True, default="")
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    # Money received today is worth today's rate, not the rate the invoice was
    # raised at; the difference between the two is the realised gain or loss.
    exchange_rate = models.DecimalField(
        max_digits=18, decimal_places=6, null=True, blank=True, default=None)
    amount_base = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"))

    class Meta:
        ordering = ["-payment_date", "-created_at"]

    def __str__(self):
        return f"{self.reference} — {self.customer.name} {self.currency} {self.amount}"

    @property
    def rate(self):
        from apps.accounting.currency import document_rate
        return document_rate(self, self.payment_date)

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        stamp_document(self, self.payment_date)


class PaymentAllocation(models.Model):
    payment = models.ForeignKey(CustomerPayment, on_delete=models.CASCADE, related_name="allocations")
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="allocations")
    allocated_amount = models.DecimalField(max_digits=15, decimal_places=2)
    # The invoice's own book value for this slice of it, kept so the gain or
    # loss on settlement can be proved line by line.
    base_amount = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.payment.reference} → {self.invoice.reference}: {self.allocated_amount}"
