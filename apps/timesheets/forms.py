"""Timesheet forms.

Hours on a timesheet become billable time on an invoice and paid time on a
payslip, so a figure no day can hold — nought, a negative, thirty hours — is
refused here rather than discovered on a customer's bill.
"""
import datetime
from decimal import Decimal

from django import forms
from django.db.models import Sum

from apps.core.forms import date_input
from .models import TimesheetEntry

DAY = Decimal("24")
# The oldest day a timesheet can sensibly be about: anything earlier is a typo
# in the year, not a late entry.
EARLIEST = datetime.date(2000, 1, 1)


class TimesheetEntryForm(forms.ModelForm):
    class Meta:
        model = TimesheetEntry
        fields = ["project", "task", "entry_date", "hours", "description", "billable"]
        widgets = {
            "entry_date": date_input(),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        self.user = user
        if company is not None:
            from apps.projects.models import Project, Task
            self.fields["project"].queryset = Project.objects.filter(company=company)
            self.fields["task"].queryset = Task.objects.filter(project__company=company)

    def clean_hours(self):
        hours = self.cleaned_data.get("hours")
        if hours is None:
            return hours
        if hours <= 0:
            raise forms.ValidationError(
                "Enter the hours worked as a figure above nought. A day with no work "
                "in it needs no entry.")
        if hours > DAY:
            raise forms.ValidationError(
                f"{hours} hours is more than there are in a day. Split work that ran "
                "over several days into one entry per day.")
        return hours

    def clean_entry_date(self):
        day = self.cleaned_data.get("entry_date")
        if day is None:
            return day
        latest = datetime.date.today() + datetime.timedelta(days=365)
        if day < EARLIEST or day > latest:
            raise forms.ValidationError(
                f"{day:%d %b %Y} is not a day this timesheet can be about. Check the "
                "year — time is logged for days already worked, or planned at most a "
                "year ahead.")
        return day

    def clean(self):
        cleaned = super().clean()
        day, hours = cleaned.get("entry_date"), cleaned.get("hours")
        user = self.user or getattr(self.instance, "user", None)
        if day and hours and user is not None and getattr(user, "pk", None):
            # Each entry can be sensible on its own and the day still impossible:
            # two twenty-hour entries are forty hours somebody is paid or billed
            # for in one day.
            logged = TimesheetEntry.objects.filter(
                user=user, entry_date=day).exclude(pk=self.instance.pk).aggregate(
                total=Sum("hours"))["total"] or Decimal("0")
            if logged + hours > DAY:
                self.add_error(
                    "hours",
                    f"{logged} hour(s) are already logged on {day:%d %b %Y}, so "
                    f"{hours} more would make {logged + hours} — more than the day "
                    f"holds. At most {max(DAY - logged, Decimal('0'))} more can go on "
                    "that day.")
        return cleaned
