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
            "is_current": "The contract in force today. Saving a new current contract "
                          "ends the previous one the day before this one starts. "
                          "Payroll pays every contract for the days its dates cover, "
                          "so an ended contract still pays the month it ended in.",
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


LEAVER_STATUSES = ("TERMINATED", "RESIGNED")


class EmployeeForm(forms.ModelForm):
    """Edit an employee's record, and link it to the login they sign in with.

    The login is what lets a person see their own payslips, file their own
    leave and claim their own expenses, so it can only be somebody who is a
    member of this company — offering every user on the platform would let HR
    hand one company's payslips to a stranger — and only one employee record
    may hold it.

    Leaving is not a status picked from a list here: it has a date, and it ends
    the contract that pays the person. That is the Terminate screen's job, so
    this form only offers the leaver statuses to a record that already has one.
    """

    class Meta:
        model = Employee
        fields = [
            "employee_number", "first_name", "last_name", "other_names",
            "gender", "date_of_birth", "date_hired", "status",
            "department", "branch", "job_title", "reports_to", "user",
            "national_id", "kra_pin", "nssf_number", "nhif_number",
            "phone", "email", "bank_name", "bank_account_number", "notes",
        ]
        widgets = {"date_of_birth": date_input(), "date_hired": date_input()}
        labels = {"user": "Login"}
        help_texts = {
            "user": "The user who signs in as this employee — they will see their own "
                    "payslips, leave and claims. Only members of this company are offered.",
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            from apps.accounts.models import User
            from apps.organizations.models import Branch, Department
            self.fields["department"].queryset = Department.objects.filter(
                company=company, is_active=True)
            self.fields["branch"].queryset = Branch.objects.filter(
                company=company, is_active=True)
            self.fields["reports_to"].queryset = Employee.objects.filter(
                company=company, is_active=True).exclude(pk=self.instance.pk)
            # Members of this company whose login is not already somebody
            # else's employee record (the link is one login to one person).
            taken = Employee.objects.exclude(pk=self.instance.pk).filter(
                user__isnull=False).values("user_id")
            self.fields["user"].queryset = User.objects.filter(
                memberships__company=company, memberships__is_active=True,
            ).exclude(pk__in=taken).distinct().order_by("first_name", "last_name")
        self.fields["user"].required = False
        self.fields["user"].empty_label = "— No login linked"
        if self.instance.status not in LEAVER_STATUSES:
            self.fields["status"].choices = [
                c for c in self.fields["status"].choices if c[0] not in LEAVER_STATUSES]
            self.fields["status"].help_text = (
                "To record somebody leaving, use Terminate on their record: it takes "
                "the leaving date and ends their contract on it.")
            self.fields["status"].error_messages["invalid_choice"] = (
                "Use Terminate to record somebody leaving — it needs the leaving date "
                "and ends their contract, so payroll pays them up to that day and no "
                "further.")

    def clean_status(self):
        status = self.cleaned_data.get("status")
        if status in LEAVER_STATUSES and self.instance.status not in LEAVER_STATUSES:
            raise forms.ValidationError(
                "Use Terminate to record somebody leaving — it needs the leaving date "
                "and ends their contract, so payroll pays them up to that day and no "
                "further.")
        return status


class TerminationForm(forms.Form):
    """The day somebody leaves: they are paid up to it, and not after it."""

    date_terminated = forms.DateField(
        label="Last day of employment", widget=date_input(),
        help_text="They are paid up to and including this day, pro-rated for the "
                  "month, and the contract that pays them ends on it.")
    status = forms.ChoiceField(
        label="Reason", choices=[("TERMINATED", "Terminated"), ("RESIGNED", "Resigned")],
        initial="TERMINATED")

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee

    def clean_date_terminated(self):
        day = self.cleaned_data["date_terminated"]
        if self.employee and day < self.employee.date_hired:
            raise forms.ValidationError(
                f"{self.employee.full_name} was hired on "
                f"{self.employee.date_hired:%d %b %Y}, so they cannot leave before then. "
                "Check the date, or correct the hire date on their record first.")
        return day


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
