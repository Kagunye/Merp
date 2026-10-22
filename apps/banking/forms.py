from datetime import date

from django import forms
from apps.core.forms import date_input, pick_currency
from .models import BankAccount, BankTransaction, Reconciliation


class BankAccountForm(forms.ModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_number", "account_type",
                  "currency", "branch", "gl_account", "opening_balance"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            from apps.accounting.models import Account
            self.fields["gl_account"].queryset = Account.objects.filter(
                company=company, is_active=True
            ).order_by("code")
            self.fields["gl_account"].help_text = (
                "The asset account this money really sits in -- the balance you "
                "reconcile is read from here."
            )
            pick_currency(self, company, rated_only=False)

    def clean_gl_account(self):
        account = self.cleaned_data["gl_account"]
        if account.account_type in ("LIABILITY", "EQUITY", "REVENUE", "EXPENSE",
                                    "COST_OF_GOODS", "TAX"):
            self.add_error(
                "gl_account",
                f"{account.code} is a {account.get_account_type_display().lower()} account. "
                "Money sitting in a bank belongs to an asset account.",
            )
        return account


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


class BankReconciliationForm(forms.ModelForm):
    """The header of a bank statement. Which transactions it covers is decided
    on the reconciliation screen itself, by ticking them off."""

    class Meta:
        model = Reconciliation
        fields = ["bank_account", "reference", "statement_date", "closing_balance", "notes"]
        widgets = {
            "statement_date": date_input(),
            "closing_balance": forms.NumberInput(attrs={"step": "0.01"}),
            "notes": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True
            ).order_by("name")
        self.fields["reference"].help_text = "The statement number printed by the bank."
        self.fields["statement_date"].help_text = "Last day covered by the statement."
        self.fields["closing_balance"].help_text = (
            "The balance printed at the foot of the statement, before any "
            "outstanding cheques or deposits."
        )
        self.fields["notes"].label = "Notes"
        self.fields["notes"].help_text = (
            "Required only if you intend to post while the statement is out of "
            "balance -- write down why the difference is accepted."
        )

    def clean_statement_date(self):
        statement_date = self.cleaned_data["statement_date"]
        if statement_date > date.today():
            raise forms.ValidationError("A statement cannot be dated in the future.")
        return statement_date

    def clean(self):
        cleaned = super().clean()
        account = cleaned.get("bank_account")
        statement_date = cleaned.get("statement_date")
        if account and statement_date and self.instance.pk is None:
            clash = Reconciliation.objects.filter(
                bank_account=account, statement_date=statement_date,
            ).exclude(status="REVERSED").first()
            if clash:
                self.add_error(
                    "statement_date",
                    f"{account.name} is already reconciled to {statement_date:%d %b %Y} "
                    f"on statement {clash.reference}. Reverse it first if that one is wrong.",
                )
        return cleaned
