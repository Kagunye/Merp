"""Accounting forms."""
from django import forms
from django.forms import inlineformset_factory

from apps.core.scoping import narrow
from .models import Account, JournalEntry, JournalEntryLine


class AccountForm(forms.ModelForm):
    class Meta:
        model = Account
        fields = ["code", "name", "account_type", "parent", "description", "is_reconcilable", "currency"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "currency": forms.TextInput(attrs={"placeholder": "KES"}),
        }

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self._company = company
        narrow(self, company, parent="company")
        qs = self.fields["parent"].queryset.filter(is_active=True)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        self.fields["parent"].queryset = qs
        self.fields["parent"].empty_label = "— No parent (top-level)"

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip()
        clash = Account.objects.filter(company=self._company, code=code).exclude(pk=self.instance.pk).first()
        if clash:
            raise forms.ValidationError(f"Code {code} is already used by {clash.name}.")
        return code


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

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        narrow(self, company, account="company")
        self.fields["account"].queryset = self.fields["account"].queryset.filter(is_active=True)


JournalEntryLineFormSet = inlineformset_factory(
    JournalEntry,
    JournalEntryLine,
    form=JournalEntryLineForm,
    extra=3,
    min_num=2,
    validate_min=True,
    can_delete=True,
)


class TaxRateForm(forms.ModelForm):
    class Meta:
        from .models import TaxRate
        model = TaxRate
        fields = ["name", "code", "rate", "is_inclusive", "account", "effective_from", "effective_to"]
        widgets = {
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {"rate": "Rate (%)", "account": "Post tax to", "is_inclusive": "Prices include this tax"}

    def __init__(self, *args, **kwargs):
        company = kwargs.pop("company", None)
        super().__init__(*args, **kwargs)
        self._company = company
        narrow(self, company, account="company")
        self.fields["account"].queryset = self.fields["account"].queryset.filter(
            is_active=True, account_type__in=["TAX", "LIABILITY"]).order_by("code")

    def clean_code(self):
        from .models import TaxRate
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if TaxRate.objects.filter(company=self._company, code=code).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError(f"Tax code {code} already exists.")
        return code
