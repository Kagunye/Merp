"""Accounting forms."""
from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new
from django.forms import inlineformset_factory
from apps.organizations.models import FiscalYear
from .models import (
    Account, Currency, ExchangeRate, JournalEntry, JournalEntryLine,
)

# Every form field in this app shares one look, and the class list is long
# enough that repeating it invites a typo.
FIELD_CLASS = "form-input w-full border border-gray-300 rounded-lg px-3 py-2 text-sm"


class AccountForm(forms.ModelForm):
    # The accounts a bookkeeper has to tie out to something outside the ledger --
    # a bank statement, a customer list, a supplier list, the tax return.
    RECONCILABLE_TYPES = ("CASH", "BANK", "RECEIVABLE", "PAYABLE", "TAX")

    class Meta:
        model = Account
        fields = ["code", "name", "account_type", "parent", "description", "is_reconcilable", "currency"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "currency": forms.TextInput(attrs={"placeholder": "KES"}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company:
            parents = Account.objects.filter(company=company, is_active=True)
            if self.instance.pk:
                parents = parents.exclude(pk=self.instance.pk)
            self.fields["parent"].queryset = parents
            self.fields["parent"].empty_label = "— No parent (top-level)"
            base = (company.currency or "KES").upper()
            self.fields["currency"].help_text = (
                f"Leave blank — the ledger holds this account in {base}. "
                "Foreign documents are converted before they are posted."
            )
            self.fields["currency"].widget.attrs["placeholder"] = base
        self.fields["is_reconcilable"].help_text = (
            "Tracked against a statement or subledger. Cash, bank, receivable, "
            "payable and tax accounts are always tracked."
        )

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        if code and self.company:
            clash = Account.objects.filter(company=self.company, code__iexact=code)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            clash = clash.first()
            if clash:
                raise forms.ValidationError(
                    f"Code {code} is already used by '{clash.name}'. Codes are unique per company."
                )
        return code

    def clean_account_type(self):
        kind = self.cleaned_data.get("account_type")
        original = self.instance.account_type if self.instance.pk else None
        if original and kind != original and self.instance.has_postings:
            # The type decides which statement an account reports on and which
            # side its balance is read from. Changing it under postings already
            # made would move them — a revenue account turned liability takes
            # its sales out of the profit and loss without a single entry to
            # say so.
            raise forms.ValidationError(
                f"{self.instance.code} already has entries as "
                f"{self.instance.get_account_type_display().lower()}, so its type is "
                "fixed. Open a new account of the type you need and move the balance "
                "across with a journal.")
        return kind

    def clean_is_reconcilable(self):
        if self.cleaned_data.get("account_type") in self.RECONCILABLE_TYPES:
            return True
        return self.cleaned_data.get("is_reconcilable")


    def clean_currency(self):
        code = (self.cleaned_data.get("currency") or "").strip().upper()
        base = getattr(self, "company", None) and (self.company.currency or "KES").upper()
        if code and base and code != base:
            raise forms.ValidationError(
                f"An account balance is kept in one currency only, and this book is "
                f"kept in {base}. Foreign documents are converted to {base} before "
                f"they reach the ledger, so leave this blank — a dollar bank account "
                f"is still carried at its {base} value."
            )
        return code

    def clean(self):
        cleaned = super().clean()
        parent = cleaned.get("parent")
        if parent and self.instance.pk:
            # A chart is a tree: walking up from the chosen parent must never
            # arrive back at this account. (Walking up from the account itself
            # would miss choosing one of its own children as its parent.)
            seen, node = set(), parent
            while node is not None and node.pk not in seen:
                seen.add(node.pk)
                node = node.parent
            if self.instance.pk in seen:
                self.add_error("parent", "An account cannot be its own parent or child.")
        return cleaned


class JournalEntryForm(forms.ModelForm):
    class Meta:
        model = JournalEntry
        fields = ["reference", "entry_type", "posting_date", "notes"]
        widgets = {
            "posting_date": date_input(),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        # Only the kinds of entry a person types are offered. A hand-made
        # journal typed CLOSING would be left out of the profit and loss and out
        # of every close; typed REVALUATION or DEPRECIATION it would be taken
        # for machinery a module owns. The model's own validation then refuses
        # any other value a tampered form sends.
        self.fields["entry_type"].choices = [
            (code, label) for code, label in JournalEntry.ENTRY_TYPES
            if code in JournalEntry.USER_ENTRY_TYPES
        ]
        self.fields["entry_type"].help_text = (
            "Manual for an ordinary journal. Invoices, bills, payroll, depreciation, "
            "closes and revaluations write their own entries.")
        # JournalEntry.pk is a UUID with a default, so a brand new instance
        # already carries one; _state.adding is what says it has never been saved.
        if self.instance._state.adding:
            self.fields["reference"].required = False
            self.fields["reference"].initial = ""
            self.fields["reference"].help_text = "Generated by Mofisi when you save."
        else:
            # A reference is the audit anchor of an entry; nothing rewrites it.
            self.fields["reference"].disabled = True

    def clean_reference(self):
        if not self.instance._state.adding:
            return self.instance.reference
        # Whatever the browser sends is ignored: the ledger mints its own
        # sequential references, so two people saving at once cannot collide.
        company = self.company or getattr(self.instance, "company", None)
        if not company:
            raise forms.ValidationError("A journal entry needs a company before it can be numbered.")
        from apps.core.models import SequenceCounter
        return SequenceCounter.next_number(company, "JE-")


class JournalEntryLineForm(forms.ModelForm):
    class Meta:
        model = JournalEntryLine
        fields = ["account", "description", "debit_amount", "credit_amount"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["account"].queryset = Account.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        debit = cleaned.get("debit_amount") or Decimal("0")
        credit = cleaned.get("credit_amount") or Decimal("0")
        if debit > 0 and credit > 0:
            self.add_error("credit_amount", "A line is either a debit or a credit, not both.")
        elif cleaned.get("account") and debit == 0 and credit == 0 \
                and not cleaned.get("DELETE"):
            # A line of nothing is noise in the ledger, and a journal made only
            # of them would post as an entry that moved nothing at all.
            self.add_error(
                "debit_amount",
                "Enter a debit or a credit for this line, or clear the account to drop it.")
        return cleaned


JournalEntryLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalEntryLine,
    form=JournalEntryLineForm,
    extra=3,
    min_num=2,
    validate_min=True,
    can_delete=True,
)


class FiscalYearForm(forms.ModelForm):
    """A new year on the calendar, refused when it cannot be a year of this book.

    Every posting date has to fall in exactly one period: two years over the
    same months would give a date two periods, and close the same months twice
    into retained earnings. A year that ends before it starts has no months to
    cut into periods, and used to be saved with none.
    """

    class Meta:
        model = FiscalYear
        fields = ["name", "start_date", "end_date", "is_current"]
        widgets = {"start_date": date_input(), "end_date": date_input()}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        for name, field in self.fields.items():
            if isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs["class"] = "form-checkbox"
            else:
                field.widget.attrs["class"] = FIELD_CLASS

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if name and self.company:
            clash = FiscalYear.objects.filter(company=self.company, name__iexact=name)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise forms.ValidationError(f"There is already a fiscal year called {name}.")
        return name

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_date"), cleaned.get("end_date")
        if not (start and end):
            return cleaned
        if end <= start:
            self.add_error(
                "end_date",
                f"The year ends on {end:%d %b %Y}, which is not after it starts on "
                f"{start:%d %b %Y}. Check the two dates — a fiscal year runs forward.")
            return cleaned
        if self.company:
            overlap = FiscalYear.objects.filter(
                company=self.company, start_date__lte=end, end_date__gte=start)
            if self.instance.pk:
                overlap = overlap.exclude(pk=self.instance.pk)
            other = overlap.order_by("start_date").first()
            if other:
                self.add_error(
                    "start_date",
                    f"{start:%d %b %Y} – {end:%d %b %Y} overlaps {other.name} "
                    f"({other.start_date:%d %b %Y} – {other.end_date:%d %b %Y}). A date "
                    "can only belong to one year; start this one the day after the "
                    "other ends.")
        return cleaned


class CurrencyForm(forms.ModelForm):
    """Registering a currency the book is allowed to hear about."""

    class Meta:
        model = Currency
        fields = ["code", "name", "symbol", "minor_units"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].widget.attrs["class"] = FIELD_CLASS
        self.fields["code"].help_text = "The ISO code, in capitals: USD, KES, EUR."
        self.fields["name"].widget.attrs["class"] = FIELD_CLASS
        self.fields["symbol"].widget.attrs["class"] = FIELD_CLASS
        self.fields["minor_units"].widget.attrs["class"] = FIELD_CLASS
        self.fields["minor_units"].help_text = (
            "Digits after the decimal point — 2 for shillings and dollars, 0 for yen."
        )

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if not code:
            raise forms.ValidationError("A currency needs a code.")
        clash = Currency.objects.filter(code=code)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError(f"{code} is already registered.")
        return code


class ExchangeRateForm(forms.ModelForm):
    """Filing what one unit of a foreign currency bought on a given day."""

    class Meta:
        model = ExchangeRate
        fields = ["currency", "effective_date", "rate", "source", "note"]
        widgets = {"effective_date": date_input()}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        if company and is_new(self):
            self.instance.company = company
        book = (getattr(company, "currency", "") or "KES").upper()
        self.fields["currency"].queryset = Currency.objects.filter(
            is_active=True).exclude(code=book).order_by("code")
        self.fields["currency"].empty_label = "— Choose a currency"
        self.fields["currency"].widget.attrs["class"] = FIELD_CLASS
        self.fields["rate"].widget.attrs.update(
            {"class": FIELD_CLASS, "step": "0.000001"})
        self.fields["rate"].help_text = (
            f"How many {book} one unit of this currency bought."
        )
        self.fields["source"].widget.attrs["class"] = FIELD_CLASS
        self.fields["note"].widget.attrs["class"] = FIELD_CLASS

    def clean_rate(self):
        rate = self.cleaned_data.get("rate")
        if rate is not None and rate <= 0:
            raise forms.ValidationError(
                "A rate must be more than zero — a currency is never worthless.")
        return rate

    def clean(self):
        cleaned = super().clean()
        currency = cleaned.get("currency")
        on_date = cleaned.get("effective_date")
        company = self.company or self.instance.company
        if currency and on_date and company:
            clash = ExchangeRate.objects.filter(
                company=company, currency=currency, effective_date=on_date)
            if self.instance.pk:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                self.add_error(
                    "effective_date",
                    f"{currency} already has a rate filed for {on_date:%d %b %Y}. "
                    f"Edit that one — a day can only have one rate.")
        return cleaned
