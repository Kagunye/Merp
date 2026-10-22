from django import forms

from apps.core import guards
from apps.core.scoping import narrow
from .models import ExpenseCategory, ExpenseClaim, ExpenseClaimLine


class ExpenseCategoryForm(forms.ModelForm):
    class Meta:
        model = ExpenseCategory
        fields = ["name", "gl_account"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, gl_account="company")


class ExpenseClaimForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = ExpenseClaim
        fields = ["reference", "employee", "claim_date"]
        widgets = {"claim_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, company=None, only_self=False, own_employee=None, **kwargs):
        """With `only_self`, people may only claim for themselves (`own_employee`)."""
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, employee="company")
        if only_self:
            qs = self.fields["employee"].queryset
            self.fields["employee"].queryset = qs.filter(pk=own_employee.pk) if own_employee else qs.none()
            self.fields["employee"].help_text = "You can claim for yourself only."
            self.fields["employee"].error_messages["invalid_choice"] = "You can claim for yourself only; ask finance to do it for someone else."
            if own_employee:
                self.fields["employee"].initial = own_employee.pk

    def clean_claim_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("claim_date"), "Claim date"),
                                    "Claim date")

    def clean(self):
        data = super().clean()
        guards.unique(self, ExpenseClaim, self.company, "reference", "Claim reference")
        return data


class ExpenseClaimLineForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = ExpenseClaimLine
        fields = ["category", "description", "expense_date", "amount", "receipt"]
        widgets = {"expense_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, category="company")

    def clean_amount(self):
        return guards.positive(self.cleaned_data.get("amount"), "Amount")

    def clean_expense_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("expense_date"), "Expense date"),
                                    "Expense date")
