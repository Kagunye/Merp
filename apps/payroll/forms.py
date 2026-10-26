"""Payroll forms.

A run's figures are never typed: only the period, the accounts it moves between
and the tables it is calculated from are editable here.
"""
from decimal import Decimal

from django import forms

from apps.core.forms import date_input, is_new
from .models import PayrollRun, PayrollTaxBand, SalaryComponent, StatutoryRule, normalise_code
from .services import overlapping_run

HUNDRED = Decimal("100")


def refuse_negative(form, name, what):
    """A figure that only makes sense at nought or above.

    A negative pension rate or a negative flat levy does not fail loudly: it is
    worked out, it adds money to somebody's pay, and it books a liability with
    the wrong sign — which is far harder to find afterwards than a form error
    is to fix now.
    """
    value = form.cleaned_data.get(name)
    if value is not None and value < 0:
        raise forms.ValidationError(
            f"{what} cannot be negative. Enter it as a positive figure — whether it "
            "is added to pay or taken off is decided by the kind of row, not the sign.")
    return value


def refuse_beyond_whole(form, name, what):
    """A percentage of pay: between nought and the whole of it."""
    value = refuse_negative(form, name, what)
    if value is not None and value > HUNDRED:
        raise forms.ValidationError(
            f"{what} of {value}% is more than the whole of the pay it is taken from. "
            "A share of pay is between 0 and 100 — check for a misplaced decimal point.")
    return value

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
                kinds = self.account_types.get(name)
                self.fields[name].queryset = (
                    available.filter(account_type__in=kinds) if kinds else available)
                if not self.fields[name].required:
                    self.fields[name].empty_label = "— Choose an account"

    # Which kinds of account each picker may offer, by field. A ModelChoiceField
    # validates against its queryset, so restricting the list is also what
    # refuses a hand-made POST naming an account the list would never show.
    account_types = {}


class PayrollRunForm(AccountChoiceForm):
    account_fields = ("salary_account", "paid_into")
    # Pay is charged to a cost and paid out of money. Offering the bank as the
    # salary account books the gross as a debit to the bank: the bank looks
    # richer by everything withheld, and the month reports no salary cost.
    account_types = {
        "salary_account": ["EXPENSE"],
        "paid_into": ["BANK", "CASH"],
    }

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
                  "monthly_relief", "reduces_taxable_pay", "employee_minimum",
                  "payable_account", "expense_account", "effective_from", "is_active"]
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

    def clean_code(self):
        # Codes are keys the run groups by: 'nssf' typed beside 'NSSF' must be
        # the same table, or both are charged and the employee pays twice.
        code = normalise_code(self.cleaned_data.get("code"))
        if not code:
            raise forms.ValidationError("Give the table a short code, e.g. PAYE or NSSF.")
        return code

    def clean_employee_percent(self):
        return refuse_beyond_whole(self, "employee_percent", "The employee's share")

    def clean_employer_percent(self):
        return refuse_beyond_whole(self, "employer_percent", "The employer's share")

    def clean_employee_flat(self):
        return refuse_negative(self, "employee_flat", "The employee's fixed figure")

    def clean_employer_flat(self):
        return refuse_negative(self, "employer_flat", "The employer's fixed figure")

    def clean_basis_cap(self):
        value = self.cleaned_data.get("basis_cap")
        if value is not None and value <= 0:
            raise forms.ValidationError(
                "A pay ceiling has to be above nought — a ceiling of nothing would "
                "charge the rate on no pay at all. Leave it blank for no ceiling.")
        return value

    def clean_monthly_relief(self):
        return refuse_negative(self, "monthly_relief", "The monthly relief")

    def clean_employee_minimum(self):
        value = refuse_negative(self, "employee_minimum", "The employee's monthly minimum")
        # Nought is the same as no floor, and is stored as no floor.
        return value or None

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
                  "percent", "is_taxable", "counts_as_statutory_pay",
                  "reduces_taxable_pay", "gl_account", "is_active"]

    # A yes/no list rather than a tick box: a tick box that is not in the post
    # reads as "no", and this one defaults to yes — an older screen or a script
    # that never heard of it must not quietly take every allowance out of the
    # pension and health base.
    counts_as_statutory_pay = forms.TypedChoiceField(
        label="Counts as pay for statutory contributions",
        choices=[("True", "Yes — pension, health and housing shares are charged on it"),
                 ("False", "No — a reimbursement such as mileage, not pay")],
        coerce=lambda v: v in (True, "True", "true", "on", "1"),
        empty_value=True, required=False, initial="True",
        help_text="Only matters for an earning. Deductions are never pay.")

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

    def clean_amount(self):
        return refuse_negative(self, "amount", "The amount")

    def clean_percent(self):
        return refuse_beyond_whole(self, "percent", "The percentage")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("component_type") == "DEDUCTION" and not cleaned.get("gl_account"):
            self.add_error(
                "gl_account",
                "Money taken out of pay is held for somebody else — a union, a bank, a "
                "scheme — so it has to be credited to an account they can be paid from.")
        return cleaned


class PayrollTaxBandForm(forms.ModelForm):
    """One step of a tiered table.

    The steps are measured in pay, so they start at nought or above, and the
    rate on a step is a share of the pay inside it.
    """

    class Meta:
        model = PayrollTaxBand
        fields = ["lower", "upper", "percent"]

    def clean_lower(self):
        return refuse_negative(self, "lower", "Where a band starts")

    def clean_upper(self):
        return refuse_negative(self, "upper", "Where a band ends")

    def clean_percent(self):
        return refuse_beyond_whole(self, "percent", "The rate on a band")
