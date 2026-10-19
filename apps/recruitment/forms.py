from django import forms
from apps.core.forms import date_input, datetime_input
from .models import Candidate, Interview, JobOpening


class JobOpeningForm(forms.ModelForm):
    class Meta:
        model = JobOpening
        fields = ["title", "department", "location", "employment_type", "headcount",
                  "salary_range_min", "salary_range_max", "description", "requirements",
                  "status", "posted_date", "close_date", "hiring_manager"]
        widgets = {
            "posted_date": date_input(),
            "close_date": date_input(),
            "description": forms.Textarea(attrs={"rows": 4}),
            "requirements": forms.Textarea(attrs={"rows": 4}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            from apps.accounts.models import User
            from apps.organizations.models import Department
            self.fields["department"].queryset = Department.objects.filter(
                company=company, is_active=True).order_by("name")
            self.fields["hiring_manager"].queryset = User.objects.filter(
                memberships__company=company, is_active=True).distinct()


class CandidateForm(forms.ModelForm):
    class Meta:
        model = Candidate
        fields = ["opening", "first_name", "last_name", "email", "phone",
                  "current_role", "years_experience", "resume", "source",
                  "stage", "expected_salary", "rating"]

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["opening"].queryset = JobOpening.objects.filter(
                company=company).order_by("-created_at")


class InterviewForm(forms.ModelForm):
    class Meta:
        model = Interview
        fields = ["candidate", "interviewer", "scheduled_at", "duration_minutes",
                  "location", "status", "feedback", "recommendation"]
        widgets = {
            "scheduled_at": datetime_input(),
            "feedback": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company:
            self.fields["candidate"].queryset = Candidate.objects.filter(
                opening__company=company).order_by("first_name", "last_name")
            from apps.accounts.models import User
            self.fields["interviewer"].queryset = User.objects.filter(
                memberships__company=company, is_active=True).distinct()
