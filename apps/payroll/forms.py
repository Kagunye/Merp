"""Payroll forms.

A run's figures are never typed: only the period, the accounts it moves between
and the tables it is calculated from are editable here.
"""
from django import forms

from apps.core.forms import date_input, is_new
from .models import PayrollRun, SalaryComponent, StatutoryRule
from .services import overlapping_run

FIELD_CLASS = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"
TEXTAREA_CLASS = "form-textarea w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"


class AccountChoiceForm(forms.ModelForm):
    """Shared plumbing for the forms that pick accounts out of this book.

    Every account a payroll figure can land in has to be this company's own: a
    ModelForm would otherwise offer the whole table, including other books'
    accounts, and a journal posted into another company's ledger is a serious
    mistake, not a validation nuisance.
    """

    account_fields = ()

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if not company:
            return
        # A draft row is given its book before it is validated, not after it is
        # saved: the model's own checks — which table is this company's tiered
        # one, does this period already have a run — read `company`, and a form
        # validating an ownerless row would wave through a clash. An unsaved
        # instance already carries a UUID, so `_state.adding` is what says new.
        if is_new(self):
            self.instance.company = company
        from apps.accounting.models import Account

        available = Account.objects.filter(company=company, is_active=True).order_by("code")
        for name in self.account_fields:
            if name in self.fields:
                self.fields[name].queryset = available
                if not self.fields[name].required:
                    self.fields[name].empty_label = "— Choose an account"


class PayrollRunForm(AccountChoiceForm):
    account_fields = ("salary_account", "paid_into")

    class Meta:
        model = PayrollRun
        fields = ["reference", "period_start", "period_end", "payment_date",
                  "salary_account", "paid_into", "notes"]
        widgets = {
            "period_start": date_input(),
            "period_end": date_input(),
            "payment_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.company:
            from apps.core.models import SequenceCounter
            self.fields["reference"].initial = SequenceCounter.next_number(
                self.company, "PR-")
            self.fields["salary_account"].empty_label = "6100 Salaries & Wages (default)"
            self.fields["paid_into"].empty_label = "1200 Bank (default)"
            self.fields["paid_into"].queryset = self.fields["paid_into"].queryset.filter(
                account_type__in=["BANK", "CASH"])

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("period_start"), cleaned.get("period_end")
        if start and end and end < start:
            self.add_error("period_end", "A pay period cannot end before it starts.")
        if start and end and self.company:
            clash = overlapping_run(self.company, start, end, exclude=self.instance)
            if clash:
                self.add_error(
                    "period_end",
                    f"'{clash.reference}' already covers {clash.period_start} to "
                    f"{clash.period_end}. Two runs over the same days would pay "
                    "everybody twice — amend or cancel the first one.")
        return cleaned


class StatutoryRuleForm(AccountChoiceForm):
    account_fields = ("payable_account", "expense_account")

    class Meta:
        model = StatutoryRule
        fields = ["code", "name", "method", "basis", "employee_percent",
                  "employer_percent", "employee_flat", "employer_flat", "basis_cap",
                  "monthly_relief", "reduces_taxable_pay", "payable_account",
                  "expense_account", "effective_from", "is_active"]
        widgets = {"effective_from": date_input()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].help_text = (
            "Short key printed on the payslip — PAYE, NSSF, SHIF. A new rate for the "
            "same code is a new row with a later start date, never an edit of the old one.")
        self.fields["expense_account"].empty_label = "— None, the employer pays no share"
        self.fields["payable_account"].queryset = self.fields[
            "payable_account"].queryset.filter(
            account_type__in=["LIABILITY", "TAX", "PAYABLE"])

    def clean(self):
        cleaned = super().clean()
        if self.company and cleaned.get("code"):
            clash = StatutoryRule.objects.filter(
                company=self.company, code__iexact=cleaned["code"],
                effective_from=cleaned.get("effective_from"))
            if not is_new(self):
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error(
                    "code",
                    f"There is already a {cleaned['code']} table starting that day. "
                    "A new rate needs its own start date, so older payslips can still "
                    "be recomputed from the table that was in force.")
        return cleaned


class SalaryComponentForm(AccountChoiceForm):
    account_fields = ("gl_account",)

    class Meta:
        model = SalaryComponent
        fields = ["employee", "name", "component_type", "calculation", "amount",
                  "percent", "is_taxable", "gl_account", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.company:
            from apps.hr.models import Employee
            self.fields["employee"].queryset = Employee.objects.filter(
                company=self.company, is_active=True).order_by(
                "last_name", "first_name")
            self.fields["employee"].empty_label = "— Everyone in the company"
            self.fields["gl_account"].empty_label = "— Salaries & wages (6100)"
            self.fields["is_active"].initial = True

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("component_type") == "DEDUCTION" and not cleaned.get("gl_account"):
            self.add_error(
                "gl_account",
                "Money taken out of pay is held for somebody else — a union, a bank, a "
                "scheme — so it has to be credited to an account they can be paid from.")
        return cleaned
