import datetime
from decimal import Decimal

from django import forms
from django.db.models import Sum

from apps.core import guards
from apps.core.scoping import narrow

from .models import TimesheetEntry

HOURS_IN_A_DAY = Decimal("24")


class TimesheetEntryForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = TimesheetEntry
        fields = ["project", "task", "entry_date", "hours", "description", "billable"]
        widgets = {
            "entry_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        narrow(self, company, project="company", task="project__company")

    def clean_hours(self):
        hours = guards.positive(self.cleaned_data.get("hours"), "Hours")
        if hours is not None and hours > HOURS_IN_A_DAY:
            raise forms.ValidationError("A day has 24 hours. Split longer work across the days it happened.")
        return hours

    def clean_entry_date(self):
        day = guards.plausible_date(self.cleaned_data.get("entry_date"), "Date")
        if day and day > datetime.date.today():
            raise forms.ValidationError("Time can't be logged for a day that hasn't happened yet.")
        return day

    def clean(self):
        cleaned = super().clean()
        project, task = cleaned.get("project"), cleaned.get("task")
        if project and task and task.project_id != project.pk:
            self.add_error("task", "Task does not belong to the selected project.")
        day, hours = cleaned.get("entry_date"), cleaned.get("hours")
        if self.user is not None and day and hours:
            logged = TimesheetEntry.objects.filter(user=self.user, entry_date=day).exclude(pk=self.instance.pk) \
                .aggregate(t=Sum("hours"))["t"] or Decimal("0")
            if logged + hours > HOURS_IN_A_DAY:
                self.add_error("hours", f"You already have {logged:g} hours on {day:%d %b}; adding {hours:g} would "
                                        f"pass 24.")
        return cleaned
