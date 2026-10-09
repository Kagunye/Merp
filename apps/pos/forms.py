from django import forms
from django.utils import timezone
from .models import POSSession


class OpenSessionForm(forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["branch", "opening_cash"]


class CloseSessionForm(forms.ModelForm):
    class Meta:
        model = POSSession
        fields = ["closing_cash"]
