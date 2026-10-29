"""HR forms.

A contract is the only place an employee's basic pay is recorded, and payroll
reads that figure every month — so the dates and the money are validated here
rather than left to whoever types them.
"""
from django import forms

from apps.core.forms import date_input
from .models import Contract, Employee

FIELD_CLASS = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"

MANDATORY_EMPLOYEE_FIELDS = [
    "employee_number", "first_name", "last_name", "other_names",
    "gender", "date_of_birth", "date_hired", "status",
    "department", "branch", "job_title",
    "national_id", "kra_pin", "nssf_number", "nhif_number",
    "phone", "email", "bank_name", "bank_account_number",
]


class EmployeeForm(forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            "employee_number", "first_name", "last_name", "other_names",
            "gender", "date_of_birth", "date_hired", "status",
            "department", "branch", "job_title", "reports_to",
            "national_id", "kra_pin", "nssf_number", "nhif_number",
            "phone", "email", "bank_name", "bank_account_number", "notes",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "date_hired": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        for fname in MANDATORY_EMPLOYEE_FIELDS:
            if fname in self.fields:
                self.fields[fname].required = True
        for fname, field in self.fields.items():
            if isinstance(field.widget, forms.Select):
                field.widget.attrs["class"] = "form-select w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, forms.Textarea):
                field.widget.attrs["class"] = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
            elif isinstance(field.widget, forms.DateInput):
                field.widget.attrs["class"] = FIELD_CLASS
                field.widget.attrs["type"] = "date"
            else:
                field.widget.attrs["class"] = FIELD_CLASS
        if company:
            from apps.organizations.models import Department, Branch
            self.fields["department"].queryset = Department.objects.filter(company=company, is_active=True)
            self.fields["branch"].queryset = Branch.objects.filter(company=company, is_active=True)
            self.fields["reports_to"].queryset = Employee.objects.filter(company=company, is_active=True)
            from apps.core.models import SequenceCounter
            self.fields["employee_number"].initial = SequenceCounter.next_number(company, "EMP-")


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
