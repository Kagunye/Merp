from django import forms
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


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["opening", "first_name", "last_name", "email", "phone",
                  "current_role", "years_experience", "resume", "source",
                  "stage", "expected_salary", "rating"]


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        fields = ["candidate", "interviewer", "scheduled_at", "duration_minutes",
                  "location", "status", "feedback", "recommendation"]
        widgets = {
            "scheduled_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "feedback": forms.Textarea(attrs={"rows": 3}),
        }
