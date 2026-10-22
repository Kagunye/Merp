"""Projects forms."""
from django import forms

from apps.core import guards
from .models import Project, Task


class ProjectForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = Project
        fields = ["reference", "name", "description", "status", "start_date", "end_date", "budget", "customer",
                  "manager", "branch", "notes"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company

    def clean_budget(self):
        return guards.not_negative(self.cleaned_data.get("budget"), "Budget")

    def clean_start_date(self):
        return guards.plausible_date(self.cleaned_data.get("start_date"), "Start date")

    def clean(self):
        data = super().clean()
        guards.order(self, "start_date", "end_date", "The project can't end before it starts.")
        guards.unique(self, Project, self.company, "reference", "Project reference")
        return data


class TaskForm(guards.GuardedFormMixin, forms.ModelForm):
    class Meta:
        model = Task
        fields = ["title", "description", "status", "priority", "assigned_to", "due_date", "estimated_hours", "notes"]

    def clean_estimated_hours(self):
        return guards.not_negative(self.cleaned_data.get("estimated_hours"), "Estimated hours")
