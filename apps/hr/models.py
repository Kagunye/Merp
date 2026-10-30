"""HR models: Employee, Leave, Attendance, Contract."""
import datetime

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
        # A contract that has already ended is history, not the one in force
        # today — but history is exactly what payroll needs to re-run an old
        # month or pay a leaver for the days they worked. So an ended contract
        # is simply not current; it is never refused for having ended.
        if self.has_ended():
            self.is_current = False
        if not (self.employee_id and self.start_date):
            return
        self._refuse_rewriting_settled_pay()
        self._refuse_overlap()

    # ── the contract history is a record payroll relies on ──

    def settled_payslips(self, start=None, end=None):
        """Payslips of approved or posted runs that paid days this contract covers.

        Contracts may not overlap, so a settled run over any day of this
        contract's dates paid that day from this contract. `start` and `end`
        narrow the days asked about (the contract's own dates by default).
        """
        from apps.payroll.models import APPROVED_RUNS, Payslip

        start = start or self.start_date
        end = end if end is not None else self.end_date
        slips = Payslip.objects.filter(
            employee_id=self.employee_id, payroll_run__status__in=APPROVED_RUNS,
            payroll_run__period_end__gte=start)
        if end is not None:
            slips = slips.filter(payroll_run__period_start__lte=end)
        return slips.select_related("payroll_run").order_by("payroll_run__period_end")

    def _refuse_rewriting_settled_pay(self):
        """A contract that paid a signed-off month keeps its pay, dates and person.

        Approved and posted runs were worked out from this row. Changing the
        salary, the dates or the employee afterwards does not change what was
        paid — it changes what the history says was owed, and the next re-run
        or termination pays the rewritten history. A raise is a new contract
        from the day it starts; a mistake in a settled contract is put right by
        cancelling the runs it paid and running them again.

        The end date may still move when every settled month lies before both
        the old and the new end date: that only changes days no signed-off run
        has paid yet, which is what recording a leaving date does.
        """
        if self._state.adding:
            return
        old = Contract.objects.filter(pk=self.pk).first()
        if old is None:
            return
        settled = list(old.settled_payslips())
        if not settled:
            return
        last_paid = max(s.payroll_run.period_end for s in settled)
        changed = []
        if old.employee_id != self.employee_id:
            changed.append("the employee")
        if old.basic_salary != self.basic_salary:
            changed.append("the basic pay")
        if old.start_date != self.start_date:
            changed.append("the start date")
        if old.end_date != self.end_date:
            def after(day):
                return day is None or day > last_paid
            if not (after(old.end_date) and after(self.end_date)):
                changed.append("the end date")
        if not changed:
            return
        runs = sorted({s.payroll_run.reference for s in settled})
        raise ValidationError(
            f"This contract paid {', '.join(runs[:3])}"
            f"{' and others' if len(runs) > 3 else ''}, which "
            f"{'is' if len(runs) == 1 else 'are'} approved or posted, so "
            f"{' and '.join(changed)} can no longer be changed — the settled months "
            "were worked out from it. For a raise, add a new contract from the day it "
            "starts. To correct a settled month, cancel that run and run it again.")

    def _refuse_overlap(self):
        """One contract pays any given day, and a newer one never loses to an older.

        Two contracts over the same days leave payroll choosing between two
        salaries for one person. The only overlap allowed is the ordinary
        supersede: a new current contract that starts after every other one
        ends the previous contract the day before it starts (see save()). A
        contract typed for an earlier period has to end before the next one
        starts, and is never made current over a later one — a back-dated
        contract with "current" left ticked must not quietly take today's pay
        away from the contract that replaced it.
        """
        others = Contract.objects.filter(employee_id=self.employee_id).exclude(pk=self.pk)
        later = others.filter(start_date__gt=self.start_date).order_by("start_date").first()
        if later is not None:
            # History, not today's pay: a later contract already exists.
            self.is_current = False
        for other in others:
            if not self._overlaps(other):
                continue
            supersedes = (self.is_current and later is None
                          and other.start_date < self.start_date)
            if supersedes:
                cut = other.settled_payslips(start=self.start_date).first()
                if cut is not None:
                    raise ValidationError(
                        f"The contract from {other.start_date:%d %b %Y} paid "
                        f"{cut.payroll_run.reference} "
                        f"({cut.payroll_run.period_start:%d %b} to "
                        f"{cut.payroll_run.period_end:%d %b %Y}), which is approved or "
                        f"posted. A new contract from {self.start_date:%d %b %Y} would "
                        "end it inside that settled month. Start the new contract after "
                        f"{cut.payroll_run.period_end:%d %b %Y}, or cancel and re-run "
                        "that month.")
                continue
            ends = (f"to {other.end_date:%d %b %Y}" if other.end_date else "with no end date")
            if other.start_date >= self.start_date:
                fix = (f"End this contract on or before "
                       f"{other.start_date - datetime.timedelta(days=1):%d %b %Y}, the day "
                       "before the later one starts.")
            else:
                fix = ("Start this contract after that one ends, or tick 'current' so "
                       "it supersedes it from its start date.")
            raise ValidationError(
                f"{self.employee.full_name} already has a contract from "
                f"{other.start_date:%d %b %Y} {ends}, which covers some of the same "
                f"days. One contract pays any given day. {fix}")

    def _overlaps(self, other):
        far = datetime.date.max
        mine_end = self.end_date or far
        theirs_end = other.end_date or far
        return self.start_date <= theirs_end and other.start_date <= mine_end

    def has_ended(self, on_date=None):
        """Did this contract finish before `on_date` (today by default)?"""
        on_date = on_date or timezone.localdate()
        return bool(self.end_date and self.end_date < on_date)

    def save(self, *args, **kwargs):
        # Saved by other screens than the form too (termination, a script), so
        # the rule that an ended contract is not current is applied here as
        # well as in clean().
        if self.has_ended():
            self.is_current = False
        super().save(*args, **kwargs)
        # Exactly one current contract per employee: "current" means the one in
        # force today, and two of them would leave nobody sure which pay is
        # right. Payroll itself reads the contracts by their dates (see
        # apps.payroll.services.contract_days), so the flag is a label for
        # today, not the switch that decides who is paid in an older month.
        if self.is_current:
            self.employee.contracts.exclude(pk=self.pk).filter(
                is_current=True).update(is_current=False)
            # The contract this one supersedes stops the day before this one
            # starts. Writing the end date down makes the history say what
            # happened — a raise from the 16th means the old pay ran to the
            # 15th — so a month re-run later pays each contract for its own
            # days. An older contract that already ends earlier is left alone.
            if self.start_date:
                day_before = self.start_date - datetime.timedelta(days=1)
                self.employee.contracts.exclude(pk=self.pk).filter(
                    start_date__lt=self.start_date,
                ).filter(
                    models.Q(end_date__isnull=True) | models.Q(end_date__gt=day_before),
                ).update(end_date=day_before)


def working_days(start, end):
    """Monday-to-Friday days from `start` to `end`, both included.

    Leave is counted in the days a person would otherwise have worked: a week
    off from Monday to Sunday is five days of annual leave, not seven, the way
    the Employment Act counts the 21-day entitlement. Public holidays are not
    taken off — the dates move every year and are gazetted late — so a request
    over Christmas counts Christmas Day; HR can shorten such a request by hand.
    """
    if not (start and end) or end < start:
        return 0
    total = (end - start).days + 1
    whole_weeks, rest = divmod(total, 7)
    days = whole_weeks * 5
    weekday = start.weekday()
    for offset in range(rest):
        if (weekday + offset) % 7 < 5:
            days += 1
    return days


# The leave a new company starts with, as Kenyan law and common practice set
# it: 21 working days of annual leave (Employment Act s.28), sick leave, three
# months' maternity and two weeks' paternity leave (s.29), and compassionate
# leave at the employer's discretion. Every row is data HR can change.
# (name, days a year, paid, carry forward, most days carried)
DEFAULT_LEAVE_TYPES = [
    ("Annual", 21, True, True, 5),
    ("Sick", 14, True, False, 0),
    ("Maternity", 90, True, False, 0),
    ("Paternity", 14, True, False, 0),
    ("Compassionate", 5, True, False, 0),
]


def ensure_default_leave_types(company):
    """Give a company the standard leave types, once.

    Called when a company is created and again the first time anybody opens
    the leave screens, so a book founded before leave types had a screen of
    their own is not left with an empty drop-down and no way to ask for leave.
    A type HR has renamed or retired is not put back: only a company with no
    leave types at all is seeded.
    """
    if company is None or LeaveType.objects.filter(company=company).exists():
        return []
    created = []
    for name, days, paid, carry, carry_days in DEFAULT_LEAVE_TYPES:
        LeaveType.objects.create(
            company=company, name=name, days_per_year=days, is_paid=paid,
            carry_forward=carry, max_carry_forward_days=carry_days)
        created.append(name)
    return created


class LeaveType(BaseModel):
    company = models.ForeignKey("organizations.Company", on_delete=models.CASCADE)
    name = models.CharField(max_length=100)
    days_per_year = models.PositiveSmallIntegerField(
        default=21, help_text="Working days (Monday to Friday) a year.")
    is_paid = models.BooleanField(default=True)
    requires_approval = models.BooleanField(default=True)
    carry_forward = models.BooleanField(
        default=False,
        help_text="Unused days of last year's allowance may be taken this year.")
    max_carry_forward_days = models.PositiveSmallIntegerField(
        default=0, help_text="The most unused days carried into the next year.")

    class Meta:
        unique_together = [("company", "name")]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        if self.carry_forward and not self.max_carry_forward_days:
            raise ValidationError({"max_carry_forward_days": (
                "Carrying leave forward needs a limit on how many days — enter the "
                "most unused days a person may take into the next year, or untick "
                "carry forward.")})


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
    # Who typed the request in. HR may file leave for somebody who phoned in
    # sick, but then somebody else decides it: one login that both files and
    # grants leave in a colleague's name can give anybody days off unseen.
    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_leaves")
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
        """Working days asked for — Monday to Friday, see working_days."""
        if not (self.start_date and self.end_date):
            return 0
        return working_days(self.start_date, self.end_date)

    # Leave that is still a claim on the year's entitlement: granted, or asked
    # for and not yet answered. A refused or withdrawn request has given the
    # days back.
    COUNTED_STATUSES = ["PENDING", "APPROVED"]

    def entitlement(self, year=None):
        """The days this request may draw on in that leave year (the start year by
        default): the type's allowance, plus what is carried over from last year."""
        year = year or self.start_date.year
        return self.leave_type.days_per_year + self.carried_into(year)

    def carried_into(self, year):
        """Unused days of last year's allowance brought into `year`.

        Only the days actually left over are carried — never the full limit
        whatever was taken — and never more than the type's limit. Somebody
        hired this year had no last year here, so they carry nothing. Last
        year's own allowance includes what it carried in, so the chain is
        followed back to the year the person was hired, and capped at every step.
        """
        kind = self.leave_type
        if not kind.carry_forward or not kind.max_carry_forward_days:
            return 0
        hired = self.employee.date_hired if self.employee_id else None
        if hired is None or year - 1 < hired.year:
            return 0
        carried = 0
        for past in range(hired.year, year):
            allowance = kind.days_per_year + carried
            taken = self.days_already_taken(past, include_self=True)
            carried = min(max(allowance - taken, 0), kind.max_carry_forward_days)
        return carried

    def years(self):
        """The calendar years this request has days in, earliest first."""
        if not (self.start_date and self.end_date) or self.end_date < self.start_date:
            return []
        return list(range(self.start_date.year, self.end_date.year + 1))

    def days_in_year(self, year):
        """How many of this request's working days fall in that calendar year.

        Each year has its own entitlement, so leave from 28 December to 5
        January is four days of this year's allowance and three of next year's
        — not seven of this year's because that is when it started.
        """
        if not (self.start_date and self.end_date):
            return 0
        first = max(self.start_date, datetime.date(year, 1, 1))
        last = min(self.end_date, datetime.date(year, 12, 31))
        return working_days(first, last)

    def days_already_taken(self, year=None, include_self=False):
        """Days of the same leave in that calendar year (the start year by
        default) already granted or waiting — everything this request has to
        fit beside. A request that straddles the year end counts only its days
        inside the year asked about."""
        year = year or self.start_date.year
        others = LeaveRequest.objects.filter(
            employee_id=self.employee_id, leave_type_id=self.leave_type_id,
            status__in=self.COUNTED_STATUSES,
            start_date__lte=datetime.date(year, 12, 31),
            end_date__gte=datetime.date(year, 1, 1),
        )
        if not include_self:
            others = others.exclude(pk=self.pk)
        return sum((r.days_in_year(year) for r in others), 0)

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
        if self.days_requested == 0:
            raise ValidationError(
                f"{self.start_date:%d %b} to {self.end_date:%d %b %Y} is a weekend, so it "
                "takes no working days of leave. Leave is counted Monday to Friday — "
                "choose dates that include a working day.")
        clash = LeaveRequest.objects.filter(
            employee_id=self.employee_id, status__in=self.COUNTED_STATUSES,
            start_date__lte=self.end_date, end_date__gte=self.start_date,
        ).exclude(pk=self.pk).select_related("leave_type").first()
        if clash:
            raise ValidationError(
                f"{self.employee.full_name} already has {clash.leave_type.name.lower()} "
                f"leave {clash.get_status_display().lower()} from "
                f"{clash.start_date:%d %b} to {clash.end_date:%d %b %Y}, which covers some "
                "of the same days. A day off is only taken once — change the dates, or "
                "cancel the other request first.")
        for year in self.years():
            allowed = self.entitlement(year)
            asked = self.days_in_year(year)
            taken = self.days_already_taken(year)
            if taken + asked > allowed:
                left = max(allowed - taken, 0)
                raise ValidationError(
                    f"This asks for {asked} working day(s) of {self.leave_type.name} in "
                    f"{year}, but {self.employee.full_name} has {left} of {allowed} left "
                    f"for {year} ({taken} already granted or waiting). Shorten the "
                    "request, or use a different kind of leave.")
