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

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.projects.models import Project, Task
        self.fields["project"].queryset = Project.objects.filter(company=company) if company else Project.objects.none()
        self.fields["task"].queryset = Task.objects.filter(project__company=company) if company else Task.objects.none()

    def clean(self):
        cleaned = super().clean()
        project, task = cleaned.get("project"), cleaned.get("task")
        if project and task and task.project_id != project.pk:
            self.add_error("task", "That task belongs to a different project.")
        return cleaned
