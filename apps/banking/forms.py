from datetime import date
from decimal import Decimal

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
            from django.db.models import Q
            # Only bank and cash accounts are offered. An account saved before
            # this rule on, say, receivables keeps its current choice in the
            # list so the form can say why it has to move, rather than calling
            # it an invalid choice.
            self.fields["gl_account"].queryset = Account.objects.filter(
                Q(account_type__in=self.MONEY_TYPES) | Q(pk=self.instance.gl_account_id),
                company=company, is_active=True,
            ).order_by("code")
            self.fields["gl_account"].help_text = (
                "The bank or cash account this money really sits in -- the balance you "
                "reconcile is read from here."
            )
            pick_currency(self, company, rated_only=False)

    # Only a bank or cash ledger account can stand behind a bank account. The
    # balance on the banking screens, the reconciliation's "books balance" and
    # every receipt or payment made through this bank account are read from or
    # posted to it: mapped to receivables or inventory, a deposit would look
    # like a customer paying or stock arriving, and the bank would be
    # reconciled against figures that are not money at all.
    MONEY_TYPES = ("BANK", "CASH")

    def clean_gl_account(self):
        account = self.cleaned_data["gl_account"]
        if account.account_type not in self.MONEY_TYPES:
            self.add_error(
                "gl_account",
                f"{account.code} {account.name} is a "
                f"{account.get_account_type_display().lower()} account. A bank account has "
                "to sit on a Bank or Cash ledger account — pick one of those, or add one "
                "to the chart of accounts first.",
            )
            return account
        # One ledger account, one bank account. Two bank accounts on the same
        # ledger account would each show the whole of its balance, and a
        # statement reconciled on one would be compared with money that is
        # partly the other's.
        clash = BankAccount.objects.filter(
            company=account.company, gl_account=account, is_active=True,
        ).exclude(pk=self.instance.pk).first()
        if clash:
            self.add_error(
                "gl_account",
                f"{account.code} {account.name} already holds {clash.name}. Each bank "
                "account needs a ledger account of its own, or both would show the same "
                "balance — add a new Bank account to the chart for this one.",
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

    def clean_amount(self):
        """The size of the movement, always above zero.

        Which way the money went is the transaction type. A negative deposit is
        a withdrawal typed in the wrong box, and would turn a reconciliation's
        cleared total the wrong way round; a zero is a line with nothing on it.
        """
        amount = self.cleaned_data.get("amount")
        if amount is None:
            return amount
        if not amount.is_finite() or amount <= Decimal("0"):
            raise forms.ValidationError(
                "Enter the amount as a figure above zero — choose Debit / Outflow for "
                "money going out rather than typing a minus sign.")
        return amount


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
