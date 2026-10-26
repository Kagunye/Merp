"""HR forms.

A contract is the only place an employee's basic pay is recorded, and payroll
reads that figure every month — so the dates and the money are validated here
rather than left to whoever types them.
"""
from django import forms

from apps.core.forms import date_input
from .access import manages_people, own_employee
from .models import Contract, Employee, LeaveRequest, LeaveType

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


class LeaveRequestForm(forms.ModelForm):
    """Ask for days off, for yourself — or for anybody, if the staff is your job.

    Whose leave it is decides whose entitlement it draws on, so the employee
    list is the first thing narrowed: somebody who does not run the staff sees
    only their own name, and a hand-made post naming a colleague fails the same
    way a name off the list would.
    """

    class Meta:
        model = LeaveRequest
        fields = ["employee", "leave_type", "start_date", "end_date", "notes"]
        widgets = {
            "start_date": date_input(),
            "end_date": date_input(),
        }

    def __init__(self, *args, company=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        staff = Employee.objects.none()
        if company:
            # Anybody who is still on the staff can be away from it — including
            # somebody already on leave, asking to extend it.
            staff = Employee.objects.filter(
                company=company, is_active=True,
                status__in=["ACTIVE", "PROBATION", "ON_LEAVE"]).order_by(
                "last_name", "first_name")
            if not manages_people(user, company):
                mine = own_employee(user, company)
                staff = staff.filter(pk=mine.pk) if mine else Employee.objects.none()
                if mine:
                    self.fields["employee"].initial = mine.pk
                    self.fields["employee"].empty_label = None
            self.fields["leave_type"].queryset = LeaveType.objects.filter(
                company=company, is_active=True)
        self.fields["employee"].queryset = staff
        # The dates and the entitlement are the request's own rules
        # (LeaveRequest.clean), so they hold for any screen that saves one.
