"""HR models: Employee, Leave, Attendance, Contract."""
from django.db import models
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
        delta = self.end_date - self.start_date
        return delta.days + 1
