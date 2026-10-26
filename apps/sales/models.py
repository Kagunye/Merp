"""Sales models: Quotation, Sales Order, Invoice, Payment."""
from decimal import Decimal, ROUND_HALF_UP
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from apps.core.models import BaseModel, NotesMixin


def cents(amount):
    """A figure as it is written on a document: to the cent, half up.

    A header total is what the customer is asked to pay and what the ledger
    posts, so it is rounded once, here, the same way every time. Leaving the
    rounding to the database column would round half-to-even on some backends
    and half-up in the journal, and the two would disagree by a cent.
    """
    return Decimal(str(amount or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


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
        # Rounded the way the invoice raised from this order rounds its own
        # header — goods to the cent, tax to the cent, total as their sum — so
        # converting the whole order bills exactly what the order promised.
        self.subtotal = cents(subtotal)
        self.tax_amount = cents(tax)
        self.total_amount = cents(taxable) + self.tax_amount
        self.total_amount_base = to_base(
            self.total_amount, self.exchange_rate or Decimal("1"))
        self.save(update_fields=[
            "subtotal", "tax_amount", "total_amount", "total_amount_base", "updated_at",
        ])

    def sync_invoiced_quantities(self):
        """Work out again how much of each line has been billed, from the invoices.

        The order's `invoiced_quantity` is a summary of the invoices raised from
        it, never an independent figure: a draft invoice can be edited down from
        20 to 8, or deleted, or cancelled, and the 12 nobody billed must become
        billable again. Counting the live invoices each time — everything raised
        from this order except cancelled ones — is what keeps the two in step;
        adding and subtracting on each event drifts as soon as one event is
        missed. Credit notes are not counted here: a credit corrects an invoice,
        it does not un-bill the order.

        An invoice line knows the order line it came from. Lines written before
        that link existed fall back to the order line for the same product,
        which is what the old matching did.

        The order's status follows: fully billed is INVOICED; an order that was
        INVOICED and now has something left to bill goes back to whatever its
        deliveries say it is.
        """
        lines = list(self.lines.all())
        if not lines:
            return
        billed = {line.pk: Decimal("0") for line in lines}
        by_product = {}
        for line in lines:
            by_product.setdefault(line.product_id, line.pk)
        live = InvoiceLine.objects.filter(
            invoice__sales_order=self, invoice__original_invoice__isnull=True,
        ).exclude(invoice__status__in=["CANCELLED", "CREDIT_NOTE"])
        for invoice_line in live:
            target = invoice_line.order_line_id
            if target not in billed:
                target = by_product.get(invoice_line.product_id)
            if target is not None:
                billed[target] += invoice_line.quantity or Decimal("0")
        for line in lines:
            if line.invoiced_quantity != billed[line.pk]:
                line.invoiced_quantity = billed[line.pk]
                line.save(update_fields=["invoiced_quantity"])

        if self.status in ("DRAFT", "CANCELLED"):
            return
        if all(line.invoiced_quantity >= line.quantity for line in lines):
            status = "INVOICED"
        elif self.status == "INVOICED":
            delivered = [line.delivered_quantity or Decimal("0") for line in lines]
            if all(d >= line.quantity for d, line in zip(delivered, lines)):
                status = "DELIVERED"
            elif any(d > 0 for d in delivered):
                status = "PARTIALLY_DELIVERED"
            else:
                status = "CONFIRMED"
        else:
            status = self.status
        if status != self.status:
            self.status = status
            self.save(update_fields=["status", "updated_at"])


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
    # Where the goods on a direct invoice leave from. An invoice raised from an
    # order never issues stock — the order's delivery already took the goods
    # off the shelf — but a counter-style invoice for stocked products is the
    # only document that says they left, so it has to say from where. Left
    # blank, the company's default warehouse is used.
    warehouse = models.ForeignKey(
        "organizations.Warehouse", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
        help_text="Where stocked goods on this invoice are issued from. Blank "
                  "uses the company's default warehouse.")
    # On a credit note only: whether the goods physically came back. A return
    # puts the units back on the shelf at what they cost when they went out; a
    # price-only credit (overcharged, a discount agreed afterwards) moves no
    # stock and leaves cost of sales alone.
    goods_returned = models.BooleanField(default=False)

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
    def outstanding_base(self):
        """What is still owed in the ledger's own currency."""
        from apps.accounting.currency import to_base
        return to_base(self.outstanding_amount, self.rate)

    def clean(self):
        from apps.accounting.currency import stamp_document
        super().clean()
        if self.pk and not self.journal_entry_id:
            # A draft keeps the rate it was given only while it is still the
            # same money on the same day. Switch a shilling draft to dollars,
            # or move its date, and the stored rate describes a different
            # document: kept, a USD 100 invoice would post as KES 100. The rate
            # is dropped so the stamp below books the day's rate for the
            # currency and date the draft now carries.
            stored = type(self).objects.filter(pk=self.pk).values(
                "currency", "invoice_date").first()
            if stored and ((stored["currency"] or "").upper() != (self.currency or "").upper()
                           or stored["invoice_date"] != self.invoice_date):
                self.exchange_rate = None
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
    # Six places, not two: an order's own discount is folded into each line
    # when the order is invoiced, and 5% off the line then 2.5% off the order
    # is 7.375% — rounded to 7.38% the invoice would bill less than the order.
    discount_percent = models.DecimalField(max_digits=9, decimal_places=6, default=Decimal("0"))
    tax_rate = models.ForeignKey("accounting.TaxRate", on_delete=models.SET_NULL, null=True, blank=True)
    # Where the line came from. An invoice line raised from an order points at
    # the order line it bills, so the order can count what is billed; a credit
    # note line points at the invoice line it credits, so the same unit cannot
    # be credited twice across several notes.
    order_line = models.ForeignKey(
        SalesOrderLine, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="invoice_lines")
    credited_line = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="credit_lines")

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
    # Money taken in on account and applied to an invoice afterwards has its
    # own date — the day the credit was matched, never earlier than the paper
    # it settles — and, when the rate moved between the receipt and the
    # invoice, its own entry for the realised exchange. A slice settled the day
    # the money came in carries neither: the receipt's own date and entry say it.
    allocation_date = models.DateField(null=True, blank=True)
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+")

    def __str__(self):
        return f"{self.payment.reference} → {self.invoice.reference}: {self.allocated_amount}"


class SubmissionToken(models.Model):
    """A one-time ticket printed into a form, spent when the form is sent.

    A double click, a refresh after a slow save, a browser that re-posts on
    "back": each sends the same form twice, and for a receipt with a blank
    number or a partial credit note both copies are perfectly valid on their
    own — two receipts for one payment, two notes for one return. Only the
    form itself can tell the second copy from a genuine second document, so
    every create form carries a ticket and the server refuses one it has
    already seen. Spending is one UPDATE on an unspent row, so two requests
    racing each other cannot both win.
    """
    token = models.CharField(max_length=64, unique=True)
    purpose = models.CharField(max_length=40)
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.purpose} {self.token[:8]}"


@receiver(post_delete, sender=Invoice)
def _hand_back_order_quantities(sender, instance, **kwargs):
    """A deleted draft bills nothing, so the order it came from is billable again."""
    if instance.sales_order_id:
        order = SalesOrder.objects.filter(pk=instance.sales_order_id).first()
        if order:
            order.sync_invoiced_quantities()
