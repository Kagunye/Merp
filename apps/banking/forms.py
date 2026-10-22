from django import forms

from apps.core import guards
from apps.core.scoping import narrow
from .models import BankAccount, BankTransaction


class BankAccountForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_number", "account_type",
                  "currency", "branch", "gl_account", "opening_balance"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, gl_account="company")
        # Money in a bank account can only live in a bank or cash ledger account.
        self.fields["gl_account"].queryset = self.fields["gl_account"].queryset.filter(
            is_active=True, account_type__in=["BANK", "CASH"]).order_by("code")
        self.fields["gl_account"].help_text = "The bank or cash account in the ledger that mirrors this account."

    def clean(self):
        data = super().clean()
        guards.unique(self, BankAccount, self.company, "account_number", "Account number")
        return data


class BankTransactionForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ["bank_account", "transaction_date", "value_date",
                  "transaction_type", "amount", "description", "reference"]
        widgets = {
            "transaction_date": forms.DateInput(attrs={"type": "date"}),
            "value_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, bank_account="company")

    def clean_amount(self):
        return guards.positive(self.cleaned_data.get("amount"), "Amount (choose debit or credit for the direction)")

    def clean_transaction_date(self):
        return guards.not_in_future(guards.plausible_date(self.cleaned_data.get("transaction_date"), "Date"), "Date")
