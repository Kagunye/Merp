"""Organization models: Company, Branch, Department, Warehouse."""
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseModel, AddressMixin, ContactMixin, NotesMixin


class Company(BaseModel, AddressMixin, ContactMixin, NotesMixin):
    """Top-level legal entity."""
    name = models.CharField(max_length=200, db_index=True)
    legal_name = models.CharField(max_length=200, blank=True, default="")
    registration_number = models.CharField(max_length=100, blank=True, default="")
    tax_id = models.CharField(max_length=100, blank=True, default="", verbose_name="Tax ID / KRA PIN")
    vat_number = models.CharField(max_length=100, blank=True, default="")
    currency = models.CharField(max_length=3, default="KES")
    timezone = models.CharField(max_length=50, default="Africa/Nairobi")
    fiscal_year_start_month = models.PositiveSmallIntegerField(default=1)
    logo = models.ImageField(upload_to="companies/logos/", null=True, blank=True)
    industry = models.CharField(max_length=100, blank=True, default="")
    is_default = models.BooleanField(default=False)

    class Meta:
        verbose_name_plural = "companies"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_default:
            Company.objects.filter(is_default=True).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class Branch(BaseModel, AddressMixin, ContactMixin):
    """A physical or logical branch of a company."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="branches")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, blank=True, default="")
    is_headquarters = models.BooleanField(default=False)
    manager_name = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        verbose_name_plural = "branches"
        ordering = ["company", "name"]
        unique_together = [("company", "name")]

    def __str__(self):
        return f"{self.company.name} — {self.name}"

    def save(self, *args, **kwargs):
        # A company has one head office. Marking a branch as headquarters moves
        # the title to it rather than leaving two branches both claiming it --
        # reports, letterheads and the default branch all read "the" HQ, and
        # with two of them which one they picked would be an accident of order.
        if self.is_headquarters and self.company_id:
            Branch.objects.filter(company_id=self.company_id, is_headquarters=True).exclude(
                pk=self.pk).update(is_headquarters=False)
        super().save(*args, **kwargs)


class Department(BaseModel):
    """Department within a company."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="departments")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="departments")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, blank=True, default="")
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="children"
    )

    class Meta:
        ordering = ["company", "name"]
        unique_together = [("company", "name")]

    def __str__(self):
        return f"{self.company.name} — {self.name}"


class Warehouse(BaseModel, AddressMixin):
    """Stock storage location."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="warehouses")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="warehouses")
    name = models.CharField(max_length=200)
    code = models.CharField(max_length=20, blank=True, default="")
    is_default = models.BooleanField(default=False)

    class Meta:
        ordering = ["company", "name"]
        unique_together = [("company", "code")]

    def __str__(self):
        return f"{self.company.name} — {self.name}"

    def save(self, *args, **kwargs):
        # One default warehouse per company: it is where a sale or a receipt
        # with no warehouse named draws from or lands in. Making another one
        # the default hands the role over instead of leaving two candidates.
        if self.is_default and self.company_id:
            Warehouse.objects.filter(company_id=self.company_id, is_default=True).exclude(
                pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class FiscalYear(BaseModel):
    """Fiscal year for a company."""
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="fiscal_years")
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)
    is_closed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-start_date"]
        unique_together = [("company", "name")]

    def __str__(self):
        return f"{self.company.name} — {self.name}"

    def save(self, *args, **kwargs):
        if self.is_current:
            FiscalYear.objects.filter(company=self.company, is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)


class AccountingPeriod(BaseModel):
    """A single accounting period within a fiscal year."""
    PERIOD_STATUS = [
        ("OPEN", "Open"),
        ("CLOSED", "Closed"),
        ("LOCKED", "Locked"),
    ]
    fiscal_year = models.ForeignKey(FiscalYear, on_delete=models.CASCADE, related_name="periods")
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=PERIOD_STATUS, default="OPEN")

    class Meta:
        ordering = ["start_date"]

    def __str__(self):
        return f"{self.fiscal_year.name} — {self.name}"


class CompanyMembership(BaseModel):
    """Links users to companies with roles."""
    ROLE_CHOICES = [
        ("ADMIN", "Company Administrator"),
        ("FINANCE_MANAGER", "Finance Manager"),
        ("ACCOUNTANT", "Accountant"),
        ("SALES_MANAGER", "Sales Manager"),
        ("SALES_REP", "Sales Representative"),
        ("PROCUREMENT", "Procurement Officer"),
        ("WAREHOUSE_MANAGER", "Warehouse Manager"),
        ("HR_MANAGER", "HR Manager"),
        ("PAYROLL_OFFICER", "Payroll Officer"),
        ("PROJECT_MANAGER", "Project Manager"),
        ("EMPLOYEE", "Employee"),
        ("AUDITOR", "Auditor"),
        ("POS_CASHIER", "POS Cashier"),
        ("VIEWER", "View Only"),
    ]
    company = models.ForeignKey(Company, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey("accounts.User", on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=30, choices=ROLE_CHOICES, default="EMPLOYEE")
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True)
    department = models.ForeignKey(Department, on_delete=models.SET_NULL, null=True, blank=True)
    is_default = models.BooleanField(default=False)

    class Meta:
        unique_together = [("company", "user")]

    def __str__(self):
        return f"{self.user} @ {self.company} ({self.role})"

    def save(self, *args, **kwargs):
        if self.is_default:
            CompanyMembership.objects.filter(
                user=self.user, is_default=True
            ).exclude(pk=self.pk).update(is_default=False)
        super().save(*args, **kwargs)


class DocumentLayout(models.Model):
    """How the company's printed documents look: quotation, sales order,
    invoice, credit note and purchase order share one A4 layout.

    The logo is kept in the database (a small thumbnail), not the media
    folder, because the hosted media folder is emptied on every deploy.
    Terms are per document type and printed on their own page, only on the
    documents that carry terms (a credit note or delivery note does not).
    """
    company = models.OneToOneField(Company, on_delete=models.CASCADE, related_name="document_layout")
    logo_data = models.BinaryField(null=True, blank=True, editable=False)
    logo_type = models.CharField(max_length=40, blank=True, default="", editable=False)
    logo_updated = models.DateTimeField(null=True, blank=True, editable=False)
    tagline = models.CharField(max_length=120, blank=True, default="",
                               help_text="A line under the logo, e.g. 'Sufficiently Superior Services'.")
    banner = models.CharField(max_length=60, blank=True, default="",
                              help_text="Small text above the document title, e.g. 'General Hardware'.")
    accent = models.CharField(max_length=7, default="#1F4E9A",
                              help_text="Colour of the title band and table headings (#RRGGBB).")
    show_ship_to = models.BooleanField(default=True, verbose_name="Show a Ship To box")
    terms_quote = models.TextField(blank=True, default="", verbose_name="Quotation terms")
    terms_order = models.TextField(blank=True, default="", verbose_name="Sales order terms")
    terms_invoice = models.TextField(blank=True, default="", verbose_name="Invoice terms")
    terms_po = models.TextField(blank=True, default="", verbose_name="Purchase order terms")
    bank_details = models.TextField(blank=True, default="",
                                    help_text="How to pay: bank, account, branch, M-Pesa paybill. Printed on invoices.")
    footer = models.CharField(max_length=200, blank=True, default="",
                              help_text="Printed at the foot of every page.")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Document layout — {self.company}"

    def terms_for(self, kind):
        return {"quote": self.terms_quote, "order": self.terms_order,
                "invoice": self.terms_invoice, "po": self.terms_po}.get(kind, "")
