from django import forms
from apps.core.forms import date_input
from .models import TimesheetEntry


class TimesheetEntryForm(forms.ModelForm):
    class Meta:
        model = TimesheetEntry
        fields = ["project", "task", "entry_date", "hours", "description", "billable"]
        widgets = {
            "entry_date": date_input(),
            "description": forms.Textarea(attrs={"rows": 2}),
        }
