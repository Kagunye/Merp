"""Accounting forms."""
from decimal import Decimal

from django import forms
from apps.core.forms import date_input
from django.forms import inlineformset_factory
from .models import Account, JournalEntry, JournalEntryLine


class AccountForm(forms.ModelForm):
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
                f"Leave blank — this company books everything in {base}. "
                "Balances are never converted between currencies."
            )
            self.fields["currency"].widget.attrs["placeholder"] = base

    def clean_currency(self):
        code = (self.cleaned_data.get("currency") or "").strip().upper()
        base = getattr(self, "company", None) and (self.company.currency or "KES").upper()
        if code and base and code != base:
            raise forms.ValidationError(
                f"There is no exchange-rate layer yet: a {code} account would add its "
                f"amounts to {base} balances untouched. Leave it blank or set the "
                f"company currency to {code}."
            )
        return code

    def clean(self):
        cleaned = super().clean()
        parent = cleaned.get("parent")
        if parent and self.instance.pk:
            # A chart is a tree: an account cannot parent its own ancestor.
            ancestor_ids = []
            node = self.instance
            while node is not None:
                ancestor_ids.append(node.pk)
                node = node.parent
            if parent.pk in ancestor_ids:
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
