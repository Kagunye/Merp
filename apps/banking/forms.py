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
    """A deposit or withdrawal typed on the banking screen.

    It always names its counter account, because saving it books the ledger
    entry behind it (see gl_service.post_bank_transaction): a bank charge with
    no expense to land on would move the bank book and leave the ledger where
    it was.
    """

    class Meta:
        model = BankTransaction
        fields = ["bank_account", "transaction_date", "value_date",
                  "transaction_type", "amount", "counter_account", "description", "reference"]
        widgets = {
            "transaction_date": date_input(),
            "value_date": date_input(),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        counter = self.fields["counter_account"]
        counter.required = True
        counter.label = "Counter account"
        counter.help_text = (
            "What the money was: an expense for bank charges, income for interest, equity "
            "for the owner's capital, a liability for a loan. Money between your own "
            "accounts is a transfer — use Banking → Transfers.")
        if company:
            from apps.accounting.gl_service import bank_counter_accounts
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True).order_by("name")
            counter.queryset = bank_counter_accounts(company)

    def clean_transaction_date(self):
        """Today or earlier: a bank book records money that has moved.

        A line dated next month would sit in the ledger as cash the company
        does not yet have, and would be offered for ticking on a statement the
        bank has not printed. A payment you expect to make is a plan, not a
        transaction — enter it on the day it goes through.
        """
        from django.utils import timezone

        day = self.cleaned_data.get("transaction_date")
        if day and day > timezone.localdate():
            raise forms.ValidationError(
                f"{day:%d %b %Y} is in the future. Record a bank transaction on or after the "
                "day it happens — today at the latest.")
        return day

    def clean(self):
        cleaned = super().clean()
        bank = cleaned.get("bank_account")
        counter = cleaned.get("counter_account")
        if bank and counter and counter.pk == bank.gl_account_id:
            self.add_error("counter_account",
                           "The counter account cannot be the bank's own ledger account.")
        return cleaned

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


class BankTransferForm(forms.Form):
    """Money moved between two of the company's own bank or cash accounts.

    Between two accounts in the same currency only the amount is needed; what
    left one arrived in the other. Between shillings and dollars the bank's
    own rate decides what arrives, so both figures are taken as the bank
    printed them and the gap against the filed rate is realised exchange.
    """
    from_account = forms.ModelChoiceField(queryset=BankAccount.objects.none(), label="From")
    to_account = forms.ModelChoiceField(queryset=BankAccount.objects.none(), label="To")
    transfer_date = forms.DateField(widget=date_input(), label="Date")
    amount_sent = forms.DecimalField(max_digits=15, decimal_places=2, label="Amount sent",
                                     help_text="In the paying account's currency.")
    amount_received = forms.DecimalField(
        max_digits=15, decimal_places=2, required=False, label="Amount received",
        help_text="In the receiving account's currency. Needed only when the two accounts "
                  "keep different currencies.")
    reference = forms.CharField(max_length=200, required=False)
    description = forms.CharField(max_length=300, required=False)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        accounts = (BankAccount.objects.filter(company=company, is_active=True)
                    .select_related("gl_account").order_by("name")
                    if company else BankAccount.objects.none())
        for name in ("from_account", "to_account"):
            self.fields[name].queryset = accounts
            self.fields[name].label_from_instance = (
                lambda a: f"{a.name} ({a.currency}) — {a.gl_account.code}")

    def clean_transfer_date(self):
        from django.utils import timezone

        day = self.cleaned_data["transfer_date"]
        if day > timezone.localdate():
            raise forms.ValidationError(
                f"{day:%d %b %Y} is in the future. Record the transfer on the day it goes "
                "through — today at the latest.")
        return day

    def _positive(self, name):
        amount = self.cleaned_data.get(name)
        if amount is not None and (not amount.is_finite() or amount <= Decimal("0")):
            raise forms.ValidationError("Enter the amount as a figure above zero.")
        return amount

    def clean_amount_sent(self):
        return self._positive("amount_sent")

    def clean_amount_received(self):
        return self._positive("amount_received")

    def clean(self):
        cleaned = super().clean()
        source, destination = cleaned.get("from_account"), cleaned.get("to_account")
        if source and destination:
            if source.pk == destination.pk:
                self.add_error("to_account", "Pick a different account to move the money to.")
            elif (source.currency or "").upper() != (destination.currency or "").upper() \
                    and cleaned.get("amount_received") is None:
                self.add_error(
                    "amount_received",
                    f"Enter what arrived in {destination.currency} — between {source.currency} "
                    f"and {destination.currency} the bank's own rate decides it.")
            elif (source.currency or "").upper() == (destination.currency or "").upper() \
                    and cleaned.get("amount_received") is not None \
                    and cleaned.get("amount_sent") is not None \
                    and cleaned["amount_received"] != cleaned["amount_sent"]:
                self.add_error(
                    "amount_received",
                    "Both accounts keep the same currency, so what arrives is what left. "
                    "Record any bank charge as a withdrawal of its own.")
        return cleaned


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
