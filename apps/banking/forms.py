from django import forms
from apps.core.forms import date_input, lock_currency
from .models import BankAccount, BankTransaction


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_number", "account_type",
                  "currency", "branch", "gl_account", "opening_balance"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            from apps.organizations.models import Branch
            from apps.accounting.models import Account
            self.fields["branch"].queryset = Branch.objects.filter(company=company, is_active=True)
            self.fields["gl_account"].queryset = Account.objects.filter(company=company, is_active=True)
            lock_currency(self, company)


class BankTransactionForm(forms.ModelForm):
    class Meta:
        model = BankTransaction
        fields = ["bank_account", "transaction_date", "value_date",
                  "transaction_type", "amount", "description", "reference"]
        widgets = {
            "transaction_date": date_input(),
            "value_date": date_input(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True).order_by("name")
