from django import forms
from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "gl_account"]


class ExpenseClaimForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaim
        fields = ["reference", "employee", "claim_date"]
        widgets = {"claim_date": forms.DateInput(attrs={"type": "date"})}


class ExpenseClaimLineForm(forms.ModelForm):
    class Meta:
        model = ExpenseClaimLine
        fields = ["category", "description", "expense_date", "amount", "receipt"]
        widgets = {"expense_date": forms.DateInput(attrs={"type": "date"})}
