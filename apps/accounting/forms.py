"""Accounting forms."""
import calendar
import datetime
import re
from decimal import Decimal

from django import forms
from apps.core.forms import date_input, is_new
from django.forms import inlineformset_factory
from apps.organizations.models import FiscalYear
from .models import (
    Account, Currency, ExchangeRate, JournalEntry, JournalEntryLine,
)

# An ISO 4217 code: exactly three plain capital letters. Python's [A-Z] is the
# ASCII range, so accented look-alikes are refused too.
CURRENCY_CODE = re.compile(r"[A-Z]{3}")

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
        # Only a preview at this stage: the number is taken in save(), once the
        # whole form and its lines have passed. Taking it here used one up for
        # every journal sent back for a mistake, leaving gaps in the JE- series
        # that an auditor would have to have explained.
        from apps.core.models import SequenceCounter
        return SequenceCounter.peek_number(company, "JE-")

    def save(self, commit=True):
        if self.instance._state.adding:
            company = self.company or getattr(self.instance, "company", None)
            from apps.core.models import SequenceCounter
            self.instance.reference = SequenceCounter.claim_number(
                company, "JE-", self.instance.reference,
                taken=lambda ref: JournalEntry.objects.filter(
                    company=company, reference=ref).exists())
        return super().save(commit)


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

    A year is twelve months. Only a first year, or a transition year when the
    year end moves, may be shorter or longer, and only when the person says
    so with the box below: then anything from one month to eighteen (the
    longest a first accounting period may run under the Companies Act) is
    accepted. Without the rule a "year" of six calendar years or of two days
    was saved, and every close, budget and comparison built on it was wrong.
    """

    MAX_TRANSITION_MONTHS = 18

    transition_year = forms.BooleanField(
        required=False, label="First or transition year",
        help_text="Tick only for a company's first year, or the one year in which the "
                  "year end moves. It may then run from 1 to 18 months; every other "
                  "fiscal year runs exactly 12 months.")

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
        from .models import EARLIEST_BOOK_DATE, latest_book_date
        if start < EARLIEST_BOOK_DATE:
            self.add_error(
                "start_date",
                f"{start:%d %b %Y} is before {EARLIEST_BOOK_DATE:%Y}. Check the year.")
            return cleaned
        latest = latest_book_date()
        if end > latest:
            self.add_error(
                "end_date",
                f"{end:%d %b %Y} is more than five years ahead (the latest year end "
                f"accepted is {latest:%d %b %Y}). Create years as they come.")
            return cleaned
        next_day = end + datetime.timedelta(days=1)
        if next_day != _add_months(start, 12):
            shortest = _add_months(start, 1)
            longest = _add_months(start, self.MAX_TRANSITION_MONTHS)
            if not cleaned.get("transition_year"):
                self.add_error(
                    "end_date",
                    f"A fiscal year runs 12 months: starting {start:%d %b %Y}, it ends "
                    f"{_add_months(start, 12) - datetime.timedelta(days=1):%d %b %Y}. "
                    "If this is the company's first year, or the year its year end "
                    "moves, tick 'First or transition year'.")
                return cleaned
            if next_day < shortest or next_day > longest:
                self.add_error(
                    "end_date",
                    f"A first or transition year runs from one month to "
                    f"{self.MAX_TRANSITION_MONTHS}: starting {start:%d %b %Y}, it ends "
                    f"between {shortest - datetime.timedelta(days=1):%d %b %Y} and "
                    f"{longest - datetime.timedelta(days=1):%d %b %Y}.")
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
            # A year slipped in before one that is already closed could never be
            # closed in order, and its postings would already have been read by
            # the later year's close. Refusing it keeps every year end drawn on
            # the years before it, once.
            closed_later = FiscalYear.objects.filter(
                company=self.company, is_closed=True, start_date__gt=end)
            if self.instance.pk:
                closed_later = closed_later.exclude(pk=self.instance.pk)
            closed_later = closed_later.order_by("start_date").first()
            if closed_later:
                self.add_error(
                    "start_date",
                    f"{closed_later.name} is already closed, and this year comes "
                    f"before it. Years are closed in order, so an earlier year cannot "
                    f"be added under a closed one. Reopen {closed_later.name} first if "
                    "the earlier year really has to be kept in this book.")
        return cleaned


def _add_months(day, months):
    """The same day `months` later, or the month's last day if it is shorter."""
    month_index = day.month - 1 + months
    year, month = day.year + month_index // 12, month_index % 12 + 1
    last = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, min(day.day, last))


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
        if not CURRENCY_CODE.fullmatch(code):
            # The register is shared by every book, and the code is what each
            # document, rate and journal records: "US1" or "Q Q" would be a
            # currency nobody can be paid in, and a look-alike letter would
            # quietly split one currency's history in two.
            raise forms.ValidationError(
                f"'{code}' is not a currency code. Use the three-letter ISO 4217 "
                "code in plain capitals A to Z, such as USD, KES or EUR.")
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
            self._refuse_backdating(company, currency, on_date, cleaned.get("rate"))
        return cleaned

    def _refuse_backdating(self, company, currency, on_date, rate):
        """Refuse a rate that would change what a booked document was worth.

        A rate governs every day from its own date until the next rate. Filed
        on a day before documents already posted in that currency, it would
        take over their days: the history would then say a dollar invoice
        booked at 130 was raised at 1.0, and the next document on that day
        would convert differently from the first. A correction is filed on or
        after the day of the last document that used the old rate (a rate for
        a document's own day is that day's rate moving, which the document,
        carrying its own stamped rate, is not changed by). Filing the same
        figure that already applies changes nothing and is allowed.
        """
        from .currency import window_in_use

        if rate is None:
            return
        exclude = self.instance.pk
        current = ExchangeRate.objects.filter(
            company=company, currency=currency, effective_date__lte=on_date)
        if exclude:
            current = current.exclude(pk=exclude)
        current = current.order_by("-effective_date").values_list("rate", flat=True).first()
        if current is not None and current == rate:
            return
        used = window_in_use(company, currency, on_date, exclude_pk=exclude, later_only=True)
        if used:
            self.add_error(
                "effective_date",
                f"{used} was already booked in {currency} on the rate that applies from "
                f"{on_date:%d %b %Y}, so a different rate cannot be filed for that day. "
                f"Date the new rate after the last {currency} document it should not "
                "change.")
