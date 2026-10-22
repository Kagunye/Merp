"""HR forms.

A contract is the only place an employee's basic pay is recorded, and payroll
reads that figure every month — so the dates and the money are validated here
rather than left to whoever types them.
"""
from django import forms

from apps.core.forms import date_input
from .models import Contract, Employee

FIELD_CLASS = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"


class ContractForm(forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["employee", "contract_type", "job_title", "department",
                  "start_date", "end_date", "basic_salary", "is_current"]
        widgets = {
            "start_date": date_input(),
            "end_date": date_input(),
        }
        help_texts = {
            "basic_salary": "The figure payroll applies the statutory tables to. "
                            "Allowances and deductions are added on the payroll side.",
            "is_current": "The contract payroll uses this month. Saving a new current "
                          "contract retires the previous one.",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            self.fields["employee"].queryset = Employee.objects.filter(
                company=company, is_active=True).order_by(
                "last_name", "first_name")
            from apps.organizations.models import Department
            self.fields["department"].queryset = Department.objects.filter(
                company=company, is_active=True)
            self.fields["department"].empty_label = "— No department"

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if start and end and end < start:
            self.add_error(
                "end_date", "A contract cannot end before the day it started.")
        return cleaned
