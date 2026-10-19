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
        self.fields["gl_account"].queryset = Account.objects.filter(
            company=company, is_active=True, account_type="EXPENSE"
        )
        self.fields["gl_account"].empty_label = "— Post to the default expense account (6000)"


class ExpenseClaimForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaim
        fields = ["reference", "employee", "claim_date", "notes"]
        widgets = {
            "claim_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.hr.models import Employee
        self.fields["employee"].queryset = Employee.objects.filter(company=company, is_active=True)
        if is_new(self):
            self.fields["claim_date"].initial = datetime.date.today()


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
