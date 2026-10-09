from django import forms
from .models import BankAccount, BankTransaction


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_number", "account_type",
                  "currency", "branch", "gl_account", "opening_balance"]


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ["bank_account", "transaction_date", "value_date",
                  "transaction_type", "amount", "description", "reference"]
        widgets = {
            "transaction_date": forms.DateInput(attrs={"type": "date"}),
            "value_date": forms.DateInput(attrs={"type": "date"}),
        }
