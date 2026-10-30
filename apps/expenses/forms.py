import datetime

from django import forms
from apps.core.forms import date_input, is_new

from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "gl_account"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.accounting.models import Account
        self.company = company
        self.fields["gl_account"].queryset = Account.objects.filter(
            company=company, is_active=True, account_type="EXPENSE"
        )
        self.fields["gl_account"].empty_label = "— Post to the default expense account (6000)"

    def clean_name(self):
        # The company is set by the view, not the form, so the database's
        # (company, name) uniqueness is never checked by the form itself — a
        # second "Travel" would reach the database and come back as a server
        # error. Two categories of one name would also split one kind of spend
        # across two lines of every report, so it is refused here, by name.
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Give the category a name, e.g. Travel.")
        if self.company is not None:
            clash = ExpenseCategory.objects.filter(company=self.company, name__iexact=name)
            if not is_new(self):
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError(
                    f"There is already a category called '{clash.first().name}'. Use that "
                    "one, or give this one a different name.")
        return name


class ExpenseClaimForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaim
        fields = ["reference", "employee", "claim_date", "notes"]
        widgets = {
            "claim_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.hr.access import manages_claims, own_employee
        from apps.hr.models import Employee
        staff = Employee.objects.filter(company=company, is_active=True)
        # A claim is money paid back to the person named on it. Anybody whose
        # job is not finance or HR may only name themselves: a clerk who can
        # raise a claim in a colleague's name can draw cash the colleague never
        # spent. A hand-made post naming somebody else fails as an invalid
        # choice, the same as a name off the list.
        if company is not None and not manages_claims(user, company):
            mine = own_employee(user, company)
            staff = staff.filter(pk=mine.pk) if mine else Employee.objects.none()
            if mine and is_new(self):
                self.fields["employee"].initial = mine.pk
        self.fields["employee"].queryset = staff
        if is_new(self):
            self.fields["claim_date"].initial = datetime.date.today()
            # Optional on a new claim: left blank, or as the previewed EXP-
            # number, the next number in the series is issued when the claim
            # is saved (SequenceCounter.claim_number in the create view).
            self.fields["reference"].required = False
            self.fields["reference"].help_text = (
                "Leave as shown to take the next claim number, or type your own.")
            if company is not None:
                from apps.core.models import SequenceCounter
                self.fields["reference"].initial = SequenceCounter.peek_number(company, "EXP-")


class ExpenseClaimLineForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaimLine
        fields = ["category", "description", "expense_date", "amount", "receipt"]
        widgets = {"expense_date": date_input()}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["category"].queryset = ExpenseCategory.objects.filter(company=company)
        if not self.is_bound and not self.fields["expense_date"].initial:
            self.fields["expense_date"].initial = datetime.date.today()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount <= 0:
            raise forms.ValidationError("Line amount must be greater than zero.")
        return amount
