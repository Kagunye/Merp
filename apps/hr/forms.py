"""HR forms: hiring, contracts (salary), leave."""
import datetime

from django import forms
from django.db.models import Q

from apps.core import guards
from apps.core.scoping import narrow

from .models import Contract, Employee, LeaveRequest

MINIMUM_AGE = 18
MAX_DAYS_AHEAD_TO_HIRE = 180


class EmployeeForm(guards.GuardedFormMixin, forms.ModelForm):
    # A new hire's first contract is captured on the same screen.
    basic_salary = forms.DecimalField(label="Basic salary (monthly)", max_digits=12, decimal_places=2,
                                      required=False, help_text="Starts their first contract. Needed for payroll.")
    contract_type = forms.ChoiceField(choices=Contract.CONTRACT_TYPES, initial="PERMANENT", required=False)

    class Meta:
        model = Employee
        fields = [
            "employee_number", "first_name", "last_name", "other_names",
            "gender", "date_of_birth", "date_hired", "status",
            "department", "branch", "job_title", "reports_to",
            "national_id", "kra_pin", "nssf_number", "nhif_number",
            "phone", "email", "bank_name", "bank_account_number", "notes",
        ]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, department="company", branch="company", reports_to="company")
        for name in ("department", "branch", "reports_to"):
            self.fields[name].queryset = self.fields[name].queryset.filter(is_active=True)
        if not self.instance._state.adding:
            del self.fields["basic_salary"], self.fields["contract_type"]

    def clean_basic_salary(self):
        return guards.not_negative(self.cleaned_data.get("basic_salary"), "Basic salary")

    def clean_date_hired(self):
        hired = self.cleaned_data.get("date_hired")
        if hired and hired > datetime.date.today() + datetime.timedelta(days=MAX_DAYS_AHEAD_TO_HIRE):
            raise forms.ValidationError("Hire date is more than six months away. Add the person nearer their start date.")
        return guards.plausible_date(hired, "Hire date", ahead_days=MAX_DAYS_AHEAD_TO_HIRE, years_back=60)

    def clean(self):
        data = super().clean()
        born, hired = data.get("date_of_birth"), data.get("date_hired")
        if born and hired and hired <= born:
            self.add_error("date_hired", "Hire date is on or before their date of birth — check both dates.")
        elif born and hired:
            age = hired.year - born.year - ((hired.month, hired.day) < (born.month, born.day))
            if age < MINIMUM_AGE:
                self.add_error("date_of_birth", f"They would be {age} on their hire date. Staff must be at "
                                                f"least {MINIMUM_AGE}; employing minors needs labour-office approval.")
        guards.unique(self, Employee, self.company, "employee_number", "Employee number")
        for field, label in (("kra_pin", "KRA PIN"), ("national_id", "National ID"), ("nssf_number", "NSSF number")):
            value = (data.get(field) or "").strip()
            if not value or self.company is None:
                continue
            other = Employee.objects.filter(company=self.company, **{f"{field}__iexact": value})
            if not self.instance._state.adding:
                other = other.exclude(pk=self.instance.pk)
            other = other.first()
            if other:
                self.add_error(field, f"{label} {value} already belongs to {other.full_name} ({other.employee_number}).")
        return data


class ContractForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = Contract
        fields = ["contract_type", "job_title", "department", "start_date", "end_date", "basic_salary"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"})}
        labels = {"basic_salary": "Basic salary (monthly)"}

    def __init__(self, *args, company=None, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.employee = employee
        narrow(self, company, department="company")

    def clean_basic_salary(self):
        return guards.positive(self.cleaned_data.get("basic_salary"), "Basic salary")

    def clean_start_date(self):
        start = guards.plausible_date(self.cleaned_data.get("start_date"), "Start date")
        if start and self.employee and start < self.employee.date_hired:
            raise forms.ValidationError(f"{self.employee.full_name} was hired on {self.employee.date_hired:%d %b %Y}; "
                                        f"a contract can't start before that.")
        return start

    def clean(self):
        data = super().clean()
        guards.order(self, "start_date", "end_date", "The contract can't end before it starts.")
        return data


class LeaveRequestForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ["employee", "leave_type", "start_date", "end_date", "notes"]
        widgets = {"start_date": forms.DateInput(attrs={"type": "date"}),
                   "end_date": forms.DateInput(attrs={"type": "date"}),
                   "notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, company=None, only_self=False, own_employee=None, **kwargs):
        """With `only_self`, the person may only request leave for `own_employee` (themselves)."""
        super().__init__(*args, **kwargs)
        narrow(self, company, employee="company", leave_type="company")
        emps = self.fields["employee"].queryset.filter(is_active=True, status__in=["ACTIVE", "PROBATION"])
        if only_self:
            emps = emps.filter(pk=own_employee.pk) if own_employee else emps.none()
            self.fields["employee"].help_text = "You can request leave for yourself only."
            self.fields["employee"].error_messages["invalid_choice"] = "You can request leave for yourself only; ask HR to do it for someone else."
            if own_employee:
                self.fields["employee"].initial = own_employee.pk
        self.fields["employee"].queryset = emps
        self.fields["leave_type"].queryset = self.fields["leave_type"].queryset.filter(is_active=True)

    def clean_start_date(self):
        return guards.plausible_date(self.cleaned_data.get("start_date"), "Start date")

    def clean(self):
        data = super().clean()
        emp, kind, start, end = (data.get(k) for k in ("employee", "leave_type", "start_date", "end_date"))
        if start and end and end < start:
            self.add_error("end_date", "Leave can't end before it starts.")
            return data
        if not (emp and kind and start and end):
            return data
        live = LeaveRequest.objects.filter(employee=emp, status__in=["PENDING", "APPROVED"])
        clash = live.filter(start_date__lte=end, end_date__gte=start).first()
        if clash:
            self.add_error(None, f"{emp.full_name} already has {clash.leave_type.name.lower()} from "
                                 f"{clash.start_date:%d %b} to {clash.end_date:%d %b %Y} ({clash.get_status_display().lower()}).")
            return data
        requested = (end - start).days + 1
        year = start.year
        taken = sum(lr.days_requested for lr in live.filter(leave_type=kind).filter(
            Q(start_date__year=year) | Q(end_date__year=year)))
        allowed = kind.days_per_year
        if allowed and taken + requested > allowed:
            self.add_error(None, f"{requested} days requested, but {emp.full_name} has {max(allowed - taken, 0)} of "
                                 f"{allowed} {kind.name.lower()} days left in {year}.")
        return data
