"""Payroll models."""
from decimal import Decimal
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class SalaryComponent(BaseModel):
    COMPONENT_TYPES = [
        ("BASIC", "Basic Salary"),
        ("ALLOWANCE", "Allowance"),
        ("DEDUCTION", "Deduction"),
        ("STATUTORY", "Statutory Deduction"),
        ("OVERTIME", "Overtime"),
        ("BONUS", "Bonus"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    component_type = models.CharField(max_length=15, choices=COMPONENT_TYPES)
    is_taxable = models.BooleanField(default=True)
    is_fixed = models.BooleanField(default=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    rate = models.DecimalField(max_digits=7, decimal_places=4, default=Decimal("0"))
    gl_account = models.ForeignKey("accounting.Account", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name


class PayrollRun(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("REVIEW", "Under Review"),
        ("APPROVED", "Approved"),
        ("POSTED", "Posted"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="payroll_runs")
    reference = models.CharField(max_length=50)
    period_start = models.DateField()
    period_end = models.DateField()
    payment_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT")
    total_gross = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_deductions = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_net = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    journal_entry = models.ForeignKey("accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-period_end"]

    def __str__(self):
        return f"{self.reference} ({self.period_start} to {self.period_end})"


class Payslip(BaseModel):
    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="payslips")
    employee = models.ForeignKey("hr.Employee", on_delete=models.PROTECT, related_name="payslips")
    gross_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_allowances = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    paye_tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    nssf_employee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    nhif_shif = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    housing_levy = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    net_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))

    class Meta:
        unique_together = [("payroll_run", "employee")]

    def __str__(self):
        return f"{self.employee.full_name} — {self.payroll_run.reference}"
