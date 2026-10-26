"""HR models: Employee, Leave, Attendance, Contract."""
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from apps.core.models import BaseModel, AddressMixin, ContactMixin, NotesMixin


class Employee(BaseModel, AddressMixin, ContactMixin, NotesMixin):
    STATUS_CHOICES = [
        ("ACTIVE", "Active"),
        ("ON_LEAVE", "On Leave"),
        ("PROBATION", "Probation"),
        ("TERMINATED", "Terminated"),
        ("RESIGNED", "Resigned"),
    ]
    GENDER_CHOICES = [("M", "Male"), ("F", "Female"), ("O", "Other")]

    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE, related_name="employees")
    employee_number = models.CharField(max_length=50, db_index=True)
    user = models.OneToOneField("accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="employee")
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    other_names = models.CharField(max_length=200, blank=True, default="")
    national_id = models.CharField(max_length=50, blank=True, default="")
    kra_pin = models.CharField(max_length=50, blank=True, default="", verbose_name="KRA PIN")
    nssf_number = models.CharField(max_length=50, blank=True, default="", verbose_name="NSSF No.")
    nhif_number = models.CharField(max_length=50, blank=True, default="", verbose_name="NHIF/SHIF No.")
    gender = models.CharField(max_length=1, choices=GENDER_CHOICES, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)
    date_hired = models.DateField()
    date_terminated = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="ACTIVE", db_index=True)
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL, null=True, blank=True)
    branch = models.ForeignKey("organizations.Branch", on_delete=models.SET_NULL, null=True, blank=True)
    job_title = models.CharField(max_length=200, blank=True, default="")
    reports_to = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="direct_reports")
    bank_name = models.CharField(max_length=200, blank=True, default="")
    bank_account_number = models.CharField(max_length=100, blank=True, default="")
    profile_photo = models.ImageField(upload_to="employees/photos/", null=True, blank=True)

    class Meta:
        ordering = ["last_name", "first_name"]
        unique_together = [("company", "employee_number")]

    def __str__(self):
        return f"{self.employee_number} — {self.first_name} {self.last_name}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class Contract(BaseModel):
    CONTRACT_TYPES = [
        ("PERMANENT", "Permanent"),
        ("CONTRACT", "Fixed Term Contract"),
        ("CASUAL", "Casual / Daily"),
        ("INTERNSHIP", "Internship"),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="contracts")
    contract_type = models.CharField(max_length=15, choices=CONTRACT_TYPES, default="PERMANENT")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    basic_salary = models.DecimalField(max_digits=12, decimal_places=2)
    is_current = models.BooleanField(default=True)
    job_title = models.CharField(max_length=200)
    department = models.ForeignKey("organizations.Department", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.employee.full_name} — {self.contract_type} from {self.start_date}"

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError(
                "A contract cannot end before the day it started.")
        # The row is validated even when the form could not read the employee
        # (an empty or partial post), so the message cannot lean on a person
        # who is not attached yet — asking for the name would crash the page
        # instead of telling the user what is missing.
        who = self.employee.full_name if self.employee_id else "This employee"
        if self.basic_salary is None or self.basic_salary <= 0:
            raise ValidationError(
                f"{who} has no basic pay on this contract. "
                "Payroll works the month out of this figure, so a contract that "
                "pays nothing is a contract that has not been priced yet.")
        if self.end_date and self.is_current and self.end_date < timezone.localdate():
            raise ValidationError(
                "This contract ended in the past, so it cannot be the current one. "
                "Uncheck it, or write a new contract from the day this one left off.")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Exactly one current contract per employee: payroll takes the current one
        # to work the month out, and two of them would pay the person twice.
        if self.is_current:
            self.employee.contracts.exclude(pk=self.pk).filter(
                is_current=True).update(is_current=False)


class LeaveType(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    days_per_year = models.PositiveSmallIntegerField(default=21)
    is_paid = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True)
    carry_forward = models.BooleanField(default=False)
    max_carry_forward_days = models.PositiveSmallIntegerField(default=0)

    class Meta:
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name


class LeaveRequest(BaseModel, NotesMixin):
    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("APPROVED", "Approved"),
        ("REJECTED", "Rejected"),
        ("CANCELLED", "Cancelled"),
    ]
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="leave_requests")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PENDING", db_index=True)
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="approved_leaves"
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.employee.full_name} — {self.leave_type.name} ({self.start_date} to {self.end_date})"

    @property
    def days_requested(self):
        if not (self.start_date and self.end_date):
            return 0
        delta = self.end_date - self.start_date
        return delta.days + 1

    # Leave that is still a claim on the year's entitlement: granted, or asked
    # for and not yet answered. A refused or withdrawn request has given the
    # days back.
    COUNTED_STATUSES = ["PENDING", "APPROVED"]

    def entitlement(self):
        """The days this request may draw on in its leave year: the type's
        allowance, plus whatever the type lets a person carry over."""
        days = self.leave_type.days_per_year
        if self.leave_type.carry_forward:
            days += self.leave_type.max_carry_forward_days
        return days

    def days_already_taken(self):
        """Days of the same leave, in the same calendar year, already granted or
        waiting — everything this request has to fit beside."""
        others = LeaveRequest.objects.filter(
            employee_id=self.employee_id, leave_type_id=self.leave_type_id,
            status__in=self.COUNTED_STATUSES,
            start_date__year=self.start_date.year,
        ).exclude(pk=self.pk)
        return sum((r.days_requested for r in others), 0)

    def clean(self):
        super().clean()
        if not (self.start_date and self.end_date):
            return
        if self.end_date < self.start_date:
            raise ValidationError(
                "Leave cannot end before it starts. Check the dates — the last day of "
                "leave is the day before the person is back at work.")
        if not (self.leave_type_id and self.employee_id):
            return
        allowed = self.entitlement()
        taken = self.days_already_taken()
        if taken + self.days_requested > allowed:
            left = max(allowed - taken, 0)
            raise ValidationError(
                f"This asks for {self.days_requested} day(s) of {self.leave_type.name}, "
                f"but {self.employee.full_name} has {left} of {allowed} left for "
                f"{self.start_date.year} ({taken} already granted or waiting). Shorten "
                "the request, split it across leave years, or use a different kind of "
                "leave.")
