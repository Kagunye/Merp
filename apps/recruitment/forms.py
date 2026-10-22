from django import forms
from apps.core.scoping import narrow

from .models import Candidate, Interview, JobOpening


class JobOpeningForm(forms.ModelForm):
    class Meta:
        model = JobOpening
        fields = ["title", "department", "location", "employment_type", "headcount",
                  "salary_range_min", "salary_range_max", "description", "requirements",
                  "status", "posted_date", "close_date", "hiring_manager"]
        widgets = {
            "posted_date": forms.DateInput(attrs={"type": "date"}),
            "close_date": forms.DateInput(attrs={"type": "date"}),
            "description": forms.Textarea(attrs={"rows": 4}),
            "requirements": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, department="company", hiring_manager="users")


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["opening", "first_name", "last_name", "email", "phone",
                  "current_role", "years_experience", "resume", "source",
                  "stage", "expected_salary", "rating"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        narrow(self, company, opening="company")

    def clean_opening(self):
        # The opening is a candidate's only link to a company: never accept
        # one from another company, even if the dropdown was bypassed.
        opening = self.cleaned_data.get("opening")
        if opening is not None and (self.company is None or opening.company_id != self.company.pk):
            raise forms.ValidationError("Select a job opening from your company.")
        return opening


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        fields = ["candidate", "interviewer", "scheduled_at", "duration_minutes",
                  "location", "status", "feedback", "recommendation"]
        widgets = {
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "feedback": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        narrow(self, company, candidate="opening__company", interviewer="users")
