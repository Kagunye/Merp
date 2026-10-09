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
