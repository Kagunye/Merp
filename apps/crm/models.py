"""CRM: Customers, Leads, Contacts."""
from django.db import models
from apps.core.models import BaseModel, AddressMixin, ContactMixin, NotesMixin


class Customer(BaseModel, AddressMixin, ContactMixin, NotesMixin):
    CUSTOMER_TYPES = [
        ("INDIVIDUAL", "Individual"),
        ("BUSINESS", "Business"),
        ("GOVERNMENT", "Government"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="customers")
    name = models.CharField(max_length=200, db_index=True)
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPES, default="BUSINESS")
    tax_id = models.CharField(max_length=100, blank=True, default="")
    credit_limit = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    currency = models.CharField(max_length=3, default="KES")
    assigned_to = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="customers"
    )
    receivable_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True
    )
    logo = models.ImageField(upload_to="customers/logos/", null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def outstanding_balance(self):
        from apps.sales.models import Invoice
        from django.db.models import Sum
        return Invoice.objects.filter(
            customer=self, status__in=["SENT", "PARTIAL"]
        ).aggregate(t=Sum("outstanding_amount"))["t"] or 0


class Supplier(BaseModel, AddressMixin, ContactMixin, NotesMixin):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="suppliers")
    name = models.CharField(max_length=200, db_index=True)
    tax_id = models.CharField(max_length=100, blank=True, default="")
    payment_terms_days = models.PositiveSmallIntegerField(default=30)
    currency = models.CharField(max_length=3, default="KES")
    payable_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True
    )
    rating = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Lead(BaseModel, ContactMixin, NotesMixin):
    STATUS_CHOICES = [
        ("NEW", "New"),
        ("CONTACTED", "Contacted"),
        ("QUALIFIED", "Qualified"),
        ("PROPOSAL", "Proposal Sent"),
        ("NEGOTIATION", "Negotiation"),
        ("WON", "Won"),
        ("LOST", "Lost"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    company_name = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default="NEW", db_index=True)
    source = models.CharField(max_length=100, blank=True, default="")
    estimated_value = models.DecimalField(max_digits=15, decimal_places=2, null=True, blank=True)
    assigned_to = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True
    )
    expected_close_date = models.DateField(null=True, blank=True)
    converted_to_customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.name
