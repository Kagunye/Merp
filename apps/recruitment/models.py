"""Recruitment / Applicant Tracking System."""
from django.core.exceptions import ValidationError
from django.db import models
from apps.core.models import BaseModel, NotesMixin


class JobOpening(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("OPEN", "Open"),
        ("ON_HOLD", "On Hold"),
        ("FILLED", "Filled"),
        ("CLOSED", "Closed"),
    ]
    EMPLOYMENT_TYPE = [
        ("FULL_TIME", "Full-time"),
        ("PART_TIME", "Part-time"),
        ("CONTRACT", "Contract"),
        ("INTERN", "Internship"),
    ]
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="job_openings")
    title = models.CharField(max_length=200)
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL, null=True, blank=True)
    location = models.CharField(max_length=200, blank=True, default="")
    employment_type = models.CharField(max_length=12, choices=EMPLOYMENT_TYPE, default="FULL_TIME")
    headcount = models.PositiveSmallIntegerField(default=1)
    salary_range_min = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    salary_range_max = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    description = models.TextField(blank=True, default="")
    requirements = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT", db_index=True)
    posted_date = models.DateField(null=True, blank=True)
    close_date = models.DateField(null=True, blank=True)
    hiring_manager = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="hiring_openings",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.title

    def clean(self):
        """An opening's numbers have to describe a job someone could take:
        at least one seat, a pay band that isn't negative or upside down,
        and a closing date that isn't before the advert went up."""
        errors = {}
        if self.headcount is not None and self.headcount < 1:
            errors["headcount"] = "An opening is for at least one person. Enter 1 or more."
        for name in ("salary_range_min", "salary_range_max"):
            value = getattr(self, name)
            if value is not None and value < 0:
                errors[name] = "A salary can't be negative. Enter 0 or more, or leave it blank."
        lo, hi = self.salary_range_min, self.salary_range_max
        if lo is not None and hi is not None and lo > hi and "salary_range_max" not in errors:
            errors["salary_range_max"] = (
                f"The top of the range ({hi:,.2f}) is below the bottom ({lo:,.2f}). "
                "Swap them, or correct one.")
        if self.posted_date and self.close_date and self.close_date < self.posted_date:
            errors["close_date"] = (
                f"The opening can't close ({self.close_date:%d %b %Y}) before it was "
                f"posted ({self.posted_date:%d %b %Y}).")
        if errors:
            raise ValidationError(errors)


class Candidate(BaseModel, NotesMixin):
    STAGE_CHOICES = [
        ("APPLIED", "Applied"),
        ("SCREENING", "Screening"),
        ("INTERVIEW", "Interview"),
        ("OFFER", "Offer"),
        ("HIRED", "Hired"),
        ("REJECTED", "Rejected"),
        ("WITHDRAWN", "Withdrew"),
    ]
    opening = models.ForeignKey(JobOpening, on_delete=models.CASCADE, related_name="candidates")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True, default="")
    current_role = models.CharField(max_length=200, blank=True, default="")
    years_experience = models.PositiveSmallIntegerField(default=0)
    resume = models.FileField(upload_to="recruitment/resumes/%Y/%m/", null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, default="", help_text="Referral, LinkedIn, careers page, ...")
    stage = models.CharField(max_length=12, choices=STAGE_CHOICES, default="APPLIED", db_index=True)
    rating = models.PositiveSmallIntegerField(default=0, help_text="0–5")
    expected_salary = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    applied_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-applied_at"]

    def __str__(self):
        return f"{self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}"

    def clean(self):
        errors = {}
        if self.rating is not None and not 0 <= self.rating <= 5:
            errors["rating"] = "Rate the candidate from 0 (not yet rated) to 5."
        if self.expected_salary is not None and self.expected_salary < 0:
            errors["expected_salary"] = "An expected salary can't be negative. Enter 0 or more, or leave it blank."
        if errors:
            raise ValidationError(errors)


class Interview(models.Model):
    STATUS_CHOICES = [
        ("SCHEDULED", "Scheduled"),
        ("COMPLETED", "Completed"),
        ("NO_SHOW", "No show"),
        ("CANCELLED", "Cancelled"),
    ]
    candidate = models.ForeignKey(Candidate, on_delete=models.CASCADE, related_name="interviews")
    interviewer = models.ForeignKey("accounts.User", on_delete=models.SET_NULL, null=True, blank=True)
    scheduled_at = models.DateTimeField()
    duration_minutes = models.PositiveSmallIntegerField(default=45)
    location = models.CharField(max_length=200, blank=True, default="")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="SCHEDULED")
    feedback = models.TextField(blank=True, default="")
    recommendation = models.CharField(max_length=20, blank=True, default="",
        help_text="e.g. Strong Hire, Hire, No Hire, Strong No Hire")

    class Meta:
        ordering = ["-scheduled_at"]

    def __str__(self):
        return f"Interview — {self.candidate} @ {self.scheduled_at:%Y-%m-%d %H:%M}"

    def clean(self):
        if self.duration_minutes is not None and self.duration_minutes <= 0:
            raise ValidationError({"duration_minutes":
                                   "An interview has to last at least one minute. Enter its length in minutes."})
