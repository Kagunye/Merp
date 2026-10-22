from django import forms
from apps.core.scoping import narrow

from .models import TimesheetEntry


class TimesheetEntryForm(forms.ModelForm):
    class Meta:
        model = TimesheetEntry
        fields = ["project", "task", "entry_date", "hours", "description", "billable"]
        widgets = {
            "entry_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, project="company", task="project__company")

    def clean(self):
        cleaned = super().clean()
        project, task = cleaned.get("project"), cleaned.get("task")
        if project and task and task.project_id != project.pk:
            self.add_error("task", "Task does not belong to the selected project.")
        return cleaned
