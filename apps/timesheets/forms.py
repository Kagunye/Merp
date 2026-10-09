from django import forms
from .models import TimesheetEntry


class TimesheetEntryForm(forms.ModelForm):
    class Meta:
        model = TimesheetEntry
        fields = ["project", "task", "entry_date", "hours", "description", "billable"]
        widgets = {
            "entry_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }
