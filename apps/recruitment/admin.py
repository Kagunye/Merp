from django.contrib import admin
from .models import Candidate, Interview, JobOpening


@admin.register(JobOpening)
class JobOpeningAdmin(admin.ModelAdmin):
    list_display = ("title", "department", "status", "employment_type", "headcount", "posted_date")
    list_filter = ("status", "employment_type")


@admin.register(Candidate)
class CandidateAdmin(admin.ModelAdmin):
    list_display = ("first_name", "last_name", "email", "opening", "stage", "rating")
    list_filter = ("stage",)
    search_fields = ("first_name", "last_name", "email")


@admin.register(Interview)
class InterviewAdmin(admin.ModelAdmin):
    list_display = ("candidate", "interviewer", "scheduled_at", "status")
    list_filter = ("status",)
