from django import forms
from django.utils import timezone
from apps.core.scoping import narrow

from .models import POSSession


class OpenSessionForm(forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["branch", "opening_cash"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, branch="company")


class CloseSessionForm(forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["closing_cash"]
