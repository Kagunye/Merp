"""Accounting forms."""
from django import forms
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


class JournalEntryForm(forms.ModelForm):
    class Meta:
        model = JournalEntry
        fields = ["reference", "entry_type", "posting_date", "notes"]
        widgets = {
            "posting_date": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }


class JournalEntryLineForm(forms.ModelForm):
    class Meta:
        model = JournalEntryLine
        fields = ["account", "description", "debit_amount", "credit_amount"]


JournalEntryLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalEntryLine,
    form=JournalEntryLineForm,
    extra=3,
    min_num=2,
    validate_min=True,
    can_delete=True,
)
