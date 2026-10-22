from django import forms
from django.utils import timezone
from apps.core import guards
from apps.core.scoping import narrow

from .models import POSSession


class OpenSessionForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["branch", "opening_cash"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, branch="company")

    def clean_opening_cash(self):
        return guards.not_negative(self.cleaned_data.get("opening_cash"), "Opening float")


class CloseSessionForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["closing_cash"]
        labels = {"closing_cash": "Cash counted in the drawer"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["closing_cash"].required = True

    def clean_closing_cash(self):
        return guards.not_negative(self.cleaned_data.get("closing_cash"), "Cash counted")
