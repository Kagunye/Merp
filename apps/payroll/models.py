"""Payroll models.

Payroll is the one place in the book where the numbers are not typed by a person:
they are worked out from a table the accountant maintains — a pension share here,
a tiered income-tax table there — and every figure on a payslip has to be able to
say which table and which pay it came out of. So the rates live in rows, the
lines live in rows, and a run is only ever a calculation over both.
"""
import datetime
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import models
from apps.core.models import BaseModel, NotesMixin

CENTS = Decimal("0.01")


def money(value):
    """A figure rounded the way a payslip rounds it: half a cent goes up."""
    if value is None:
        return Decimal("0.00")
    return Decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


# The runs whose figures are somebody's decision rather than a working draft —
# including a cancelled one, whose payslips are still on file as what was paid.
# A table that has fed one of these is a record: changing its rates would make
# the slips it produced say something the table no longer says.
SETTLED_RUNS = ["APPROVED", "POSTED", "CANCELLED"]

# The staff a month's pay is worked out for. Somebody on leave is still on the
# payroll — annual, sick and maternity leave are paid — and leaving them off
# because their status says ON_LEAVE is how a person on maternity leave goes a
# month without pay and nobody notices. Unpaid leave is a pay element (a
# deduction), not a reason to drop the payslip.
PAYABLE_STATUSES = ["ACTIVE", "PROBATION", "ON_LEAVE"]

# How long after a pay period ends its payment date may fall. Longer than any
# honest delay in paying a month, short enough to catch a mistyped year.
PAYMENT_GRACE_DAYS = 60

# The most a month's pension contributions may take off taxable pay, NSSF and
# any private or occupational scheme added together. The Income Tax Act, as
# amended by the Tax Laws (Amendment) Act of December 2024, allows registered
# pension contributions up to KES 30,000 a month; anything above that is still
# deducted from pay and paid into the scheme, but it is taxed first. A
# part-month run gets the same share of the ceiling as it gets of the month.
# Held here as a constant rather than a company setting: it is the law, not a
# preference, and it changes only when Parliament changes it.
PENSION_RELIEF_CAP = Decimal("30000")

# The runs that are somebody's decision and are still standing. A contract that
# paid one of these is history the ledger relies on, so its pay, dates and
# employee are locked (see apps.hr.models.Contract.clean).
APPROVED_RUNS = ["APPROVED", "POSTED"]


def month_bounds(day):
    """The first and last day of the calendar month `day` falls in."""
    import calendar
    last = calendar.monthrange(day.year, day.month)[1]
    return day.replace(day=1), day.replace(day=last)


def days_in_month(day):
    """How many days the calendar month `day` falls in has."""
    import calendar
    return calendar.monthrange(day.year, day.month)[1]


def normalise_code(code):
    """A statutory code as the payslip prints it: trimmed, in capitals."""
    return (code or "").strip().upper()


class StatutoryRule(BaseModel):
    """One deduction or contribution, as the law describes it that month.

    Rates change, and a payslip for January must still be recomputable in June
    from the table that was in force in January, so a rule carries the date it
    starts being the rule and is never edited into a different month.
    """
    METHODS = [
        ("PERCENT", "A share of the pay"),
        ("BANDS", "A tiered table, less a monthly relief"),
        ("FLAT", "The same figure every month"),
    ]
    # What the share is taken from. Pension tables cap the pay they look at,
    # health tables take the whole of it, so the basis is part of the rule.
    BASES = [
        ("BASIC", "Basic salary"),
        ("GROSS", "Gross pay"),
    ]

    company = models.ForeignKey(
        "organizations.Company", on_delete=models.CASCADE, related_name="statutory_rules")
    code = models.CharField(
        max_length=20, help_text="Short key used on the payslip, e.g. PAYE, NSSF, SHIF.")
    name = models.CharField(max_length=200)
    method = models.CharField(max_length=10, choices=METHODS, default="PERCENT")
    basis = models.CharField(max_length=10, choices=BASES, default="GROSS")
    employee_percent = models.DecimalField(
        max_digits=9, decimal_places=6, default=Decimal("0"),
        help_text="Withheld from the employee, as a percentage of the basis.")
    employer_percent = models.DecimalField(
        max_digits=9, decimal_places=6, default=Decimal("0"),
        help_text="Paid by the employer on top of the pay, as a percentage of the basis.")
    employee_flat = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="Used by a fixed monthly figure, or added to the percentage.")
    employer_flat = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"))
    basis_cap = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, default=None,
        help_text="Pay above this is ignored when working out the share — the pension "
                  "ceiling. Leave blank for no ceiling.")
    monthly_relief = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="A tiered tax table takes this off the tax it has just computed.")
    reduces_taxable_pay = models.BooleanField(
        default=False,
        help_text="A pension contribution is taken off the pay before income tax is "
                  "worked out; a health levy is not.")
    is_pension = models.BooleanField(
        default=False, verbose_name="Is a pension contribution",
        help_text="A pension contribution (NSSF) shares the monthly ceiling on pension "
                  "relief — KES 30,000 a month for NSSF and private schemes together — "
                  "with every other pension deduction on the payslip. SHIF and the "
                  "housing levy are allowed off taxable pay too, but they are not "
                  "pensions and are not capped.")
    employee_minimum = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, default=None,
        help_text="The least the employee pays in a month they earn anything — SHIF "
                  "is 2.75% of pay but never less than 300. Leave blank for no floor.")
    payable_account = models.ForeignKey(
        "accounting.Account", on_delete=models.PROTECT, related_name="statutory_rules",
        help_text="Where the money is owed to until it is remitted.")
    expense_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="statutory_expenses",
        help_text="What the employer's own share costs. Only needed if the employer "
                  "pays a share.")
    effective_from = models.DateField(
        help_text="The first pay period this table applies to.")

    class Meta:
        ordering = ["code", "-effective_from"]
        unique_together = [("company", "code", "effective_from")]

    def __str__(self):
        return f"{self.code} from {self.effective_from}"

    def save(self, *args, **kwargs):
        # A code is a key the run groups by, so 'nssf' and 'NSSF' are the same
        # table. Stored in one case, a lower-case row typed by hand is a dated
        # change to the table it names instead of a second pension charged on
        # top of the first.
        self.code = normalise_code(self.code)
        # NSSF is the National Social Security Fund — a pension by statute —
        # so every dated version of it shares the pension relief ceiling, even
        # one typed without the box ticked.
        if self.code == "NSSF":
            self.is_pension = True
        super().save(*args, **kwargs)

    def clean(self):
        self.code = normalise_code(self.code)
        super().clean()
        if self.method == "PERCENT" and self.employee_percent == 0 \
                and self.employer_percent == 0 and self.employee_flat == 0 \
                and self.employer_flat == 0:
            raise ValidationError(
                f"{self.code} takes nothing off anybody's pay and adds nothing to the "
                "cost of employing them — a rule with no rate and no figure is a "
                "description, not a table.")
        if self.method != "BANDS" and self.monthly_relief:
            raise ValidationError(
                "Only a tiered table can be relieved. A percentage or a fixed figure "
                "would have the relief built into the rate itself.")
        if self.employer_percent and not self.expense_account_id:
            raise ValidationError(
                "The employer pays a share, so the share needs an expense account to "
                "land in. Choose one, or set the employer percentage to zero.")
        if self.method == "BANDS":
            # A new row for the same code with its own start date is the table
            # changing on that date — the August run keeps reading the old
            # steps and the October run reads the new ones, which is exactly
            # how a rate change is meant to be written. A tiered table under a
            # different code is a second income tax: no row of a table ever
            # stops being in force except by being superseded under its own
            # code, so the two would both be read on the same pay day and each
            # tax the same pay.
            clash = StatutoryRule.objects.filter(
                company_id=self.company_id, method="BANDS", is_active=True,
            ).exclude(pk=self.pk).exclude(code__iexact=self.code).first()
            if clash:
                raise ValidationError(
                    f"'{clash.code}' is already this company's tiered table. Two of "
                    "them would each tax the same pay. To change the rates, add a "
                    f"new {clash.code} row with the date the new steps start; to "
                    f"replace the table with a different one, retire {clash.code} "
                    "first.")

    @property
    def is_settled(self):
        """Has this table paid a run somebody signed off?

        Its rates — and the steps of a tiered table are rates — are then a
        record of what those payslips were worked out from. Changing them
        afterwards, even by taking a band out or adding one, would leave a
        posted slip saying 30% on a step the table no longer has.
        """
        if not self.pk:
            return False
        return PayslipLine.objects.filter(
            rule_id=self.pk, payslip__payroll_run__status__in=SETTLED_RUNS).exists()

    def bands(self):
        """The table's steps, lowest first."""
        return self.tax_bands.order_by("lower")

    # Every monthly figure below takes a `fraction`: the share of a calendar
    # month the run covers. A run is always inside one calendar month (see
    # PayrollRun.clean), and a run for half of it applies half of every monthly
    # figure the table holds — the pension ceiling, the SHIF minimum, a flat
    # levy, the PAYE steps and the relief. That is what makes two half-month
    # runs withhold what one full-month run would: the same pay, cut in two,
    # meets the same table cut in two. Percentages are not scaled — 6% of half
    # the pay is already half of 6% of the pay. A full month is fraction 1, and
    # every figure is then exactly the table's own.

    def employee_amount(self, basic, gross, fraction=Decimal("1")):
        """What comes off this employee's pay for this rule.

        A tiered table is not answered here: income tax is worked out on the
        taxable pay by `tax_on`, because the pay it is worked out on is only
        known once the allowable deductions have come off.
        """
        if self.method == "BANDS":
            return Decimal("0")
        figure = self._figure(basic, gross, self.employee_percent, self.employee_flat,
                              fraction)
        # A floor is a minimum contribution for somebody who is paid at all: a
        # month with no pay owes nothing, but a month of very little pay still
        # owes the minimum the scheme charges, not 2.75% of very little.
        if self.employee_minimum and self.basis_for(basic, gross, fraction) > 0:
            figure = max(figure, money(self.employee_minimum * fraction))
        return figure

    def employer_amount(self, basic, gross, fraction=Decimal("1")):
        if self.employer_percent == 0 and self.employer_flat == 0:
            return Decimal("0")
        return self._figure(basic, gross, self.employer_percent, self.employer_flat,
                            fraction)

    def basis_for(self, basic, gross, fraction=Decimal("1")):
        """The pay this rule actually looks at, after any ceiling.

        Writing this figure on the line is what makes a payslip provable later:
        '6% of 36,000' means something even if the pension ceiling moves. The
        ceiling is a monthly one, so a run for part of a month caps the pay at
        the same part of it.
        """
        basis = gross if self.basis == "GROSS" else basic
        if self.basis_cap is not None:
            cap = money(self.basis_cap * fraction)
            if basis > cap:
                basis = cap
        return money(basis)

    def _figure(self, basic, gross, percent, flat, fraction=Decimal("1")):
        basis = self.basis_for(basic, gross, fraction)
        if self.method == "FLAT":
            # A fixed figure is the figure for the month, whatever the pay — a
            # floor charge is not scaled by pay, only by how much of the month
            # the run covers.
            return money(flat * fraction)
        return money(flat * fraction + basis * percent / Decimal("100"))

    def relief_for(self, fraction=Decimal("1")):
        """The monthly relief, for the share of the month the run covers."""
        return money(self.monthly_relief * fraction)

    def tax_on(self, amount, fraction=Decimal("1")):
        """The tiered tax on this pay, before the monthly relief.

        The steps are monthly: a run for part of a month reads each step at the
        same part of its width, so 24,000 at 10% becomes 12,000 at 10% for
        half a month.
        """
        if self.method != "BANDS":
            raise ValueError(f"{self.code} is not a tiered table.")
        tax = Decimal("0")
        for band in self.bands():
            lower = band.lower * fraction
            if amount <= lower:
                break
            ceiling = band.upper * fraction if band.upper is not None else amount
            slice_ = min(amount, ceiling) - lower
            if slice_ > 0:
                tax += slice_ * band.percent / Decimal("100")
        return money(tax)


class PayrollTaxBand(models.Model):
    """One step of a tiered table: this percent on the pay between these figures."""
    rule = models.ForeignKey(StatutoryRule, on_delete=models.CASCADE, related_name="tax_bands")
    lower = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    upper = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, default=None,
        help_text="Leave blank for the top step, which has no ceiling.")
    percent = models.DecimalField(max_digits=9, decimal_places=6)

    class Meta:
        ordering = ["lower"]

    def __str__(self):
        top = "and above" if self.upper is None else f"to {self.upper}"
        return f"{self.percent}% on {self.lower} {top}"

    def clean(self):
        super().clean()
        if self.upper is not None and self.upper <= self.lower:
            raise ValidationError("A band has to end above where it starts.")
        for other in PayrollTaxBand.objects.filter(rule_id=self.rule_id).exclude(pk=self.pk):
            other_top = other.upper if other.upper is not None else Decimal("Infinity")
            my_top = self.upper if self.upper is not None else Decimal("Infinity")
            if self.lower < other_top and other.lower < my_top:
                raise ValidationError(
                    f"This band overlaps '{other}' — a tiered table has to step "
                    "through the pay in order, with no gap and no overlap.")


class SalaryComponent(BaseModel):
    """An earning or a deduction that is not the law: a house allowance, a
    loan repayment, a night-out rate.

    Attached to one employee, or to the company as a whole when every payslip
    should carry it.
    """
    COMPONENT_TYPES = [
        ("ALLOWANCE", "Allowance (added to pay)"),
        ("DEDUCTION", "Deduction (taken off pay)"),
        ("OVERTIME", "Overtime (added to pay)"),
        ("BONUS", "Bonus (added to pay)"),
    ]
    CALCULATIONS = [
        ("FIXED", "A fixed monthly figure"),
        ("PERCENT_OF_BASIC", "A share of basic salary"),
        ("PERCENT_OF_GROSS", "A share of gross pay"),
    ]
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.CASCADE, related_name="salary_components")
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.CASCADE, null=True, blank=True,
        related_name="salary_components",
        help_text="Leave blank to pay this to everyone in the company.")
    name = models.CharField(max_length=200)
    component_type = models.CharField(max_length=15, choices=COMPONENT_TYPES)
    calculation = models.CharField(max_length=20, choices=CALCULATIONS, default="FIXED")
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    percent = models.DecimalField(max_digits=9, decimal_places=6, default=Decimal("0"))
    is_taxable = models.BooleanField(
        default=True, help_text="An earning that income tax is worked out on.")
    counts_as_statutory_pay = models.BooleanField(
        default=True, verbose_name="Counts as pay for statutory contributions",
        help_text="An earning the pension, health and housing shares are worked out "
                  "on. Untick it for a reimbursement — mileage, a refund of costs — "
                  "which gives the employee back their own money and is not pay.")
    reduces_taxable_pay = models.BooleanField(
        default=False, verbose_name="Reduces taxable pay",
        help_text="A deduction taken off the pay before income tax is worked out — a "
                  "registered pension contribution the tax authority allows. It shares "
                  "the KES 30,000 monthly ceiling on pension relief with NSSF: whatever "
                  "is above the ceiling is still deducted, but is taxed first.")
    prorate_part_months = models.BooleanField(
        default=True, verbose_name="Pro-rate in part months",
        help_text="A fixed monthly figure is paid for the share of the month the "
                  "person was employed, the way basic pay is: a joiner from the 16th "
                  "of a 30-day month gets 15/30 of a monthly house allowance. Untick "
                  "it for a figure that is the same however much of the month was "
                  "worked — a loan repayment, say. Ignored for a share of pay, which "
                  "already follows the pay it is measured on.")
    gl_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="salary_components",
        help_text="An earning: the expense account it is charged to (blank for salaries "
                  "and wages). A deduction: the account the withheld money is owed to.")

    class Meta:
        ordering = ["component_type", "name"]
        unique_together = [("company", "employee", "name")]

    def __str__(self):
        who = self.employee.full_name if self.employee_id else "everyone"
        return f"{self.name} — {who}"

    def clean(self):
        super().clean()
        if self.component_type == "BASIC":
            raise ValidationError(
                "Basic pay comes from the employee's contract, not from a component — "
                "allowing one here would pay the same salary twice.")
        if self.calculation == "FIXED" and self.amount == 0:
            raise ValidationError(
                f"'{self.name}' is a fixed figure with no figure in it.")
        if self.calculation != "FIXED" and self.percent == 0:
            raise ValidationError(
                f"'{self.name}' is a share of pay with no percentage in it.")
        if self.reduces_taxable_pay and self.component_type != "DEDUCTION":
            raise ValidationError(
                f"'{self.name}' is an earning, so it cannot reduce taxable pay — only "
                "a deduction (a pension contribution, say) comes off the pay before "
                "tax. Untick 'Reduces taxable pay', or untick 'taxable' if the earning "
                "itself is not taxed.")
        if self.component_type == "DEDUCTION" and not self.gl_account_id:
            raise ValidationError(
                f"'{self.name}' takes money out of pay and holds it for somebody else "
                "— a union, a bank, a scheme. It needs the account that debt sits in, "
                "or the run will book money that owes nobody anything.")
        problem = self.account_problem()
        if problem:
            raise ValidationError({"gl_account": problem})
        if self.company_id and self.name:
            # The database's own uniqueness check cannot see a company-wide
            # row: (company, NULL, name) never equals another NULL in SQL, so
            # two "House allowance" rows for everyone would both save and both
            # be paid. One name per company-wide element, and one per person.
            clash = SalaryComponent.objects.filter(
                company_id=self.company_id, employee_id=self.employee_id,
                name__iexact=self.name.strip()).exclude(pk=self.pk)
            if clash.exists():
                who = (f"{self.employee.full_name}" if self.employee_id
                       else "everyone in the company")
                raise ValidationError({"name": (
                    f"'{self.name}' is already a pay element for {who}. Two of them "
                    "would both be paid — edit the one that exists, or give this one "
                    "a different name.")})

    # Which kind of account each kind of element is booked to. An earning is a
    # cost of employing somebody, so it is charged to an expense; a deduction
    # is money kept back from pay and owed to somebody else, so it is credited
    # to a liability. Anything else books nonsense: an allowance "charged" to
    # the bank debits the bank, and a deduction "owed" to the bank credits it
    # as though money had left.
    EARNING_ACCOUNT_TYPES = ("EXPENSE",)
    DEDUCTION_ACCOUNT_TYPES = ("LIABILITY",)

    def account_problem(self, account=None):
        """Why this element's account is the wrong kind, or "" when it is fine."""
        account = account if account is not None else (
            self.gl_account if self.gl_account_id else None)
        if account is None:
            return ""
        if self.is_earning():
            allowed, what = self.EARNING_ACCOUNT_TYPES, "an expense account"
            why = "an earning is a cost of employing somebody"
        else:
            allowed, what = self.DEDUCTION_ACCOUNT_TYPES, "a liability account"
            why = "a deduction is money kept back from pay and owed to somebody else"
        if account.account_type in allowed:
            return ""
        return (f"{account.code} {account.name} is a "
                f"{account.get_account_type_display().lower()} account, but {why}, so "
                f"'{self.name}' has to be booked to {what}. Choose one of those.")

    def measure_on(self, basic, gross):
        """The pay a percentage element is measured against.

        Nothing for a fixed figure, which is the same whatever the pay — and the
        payslip writes this number down beside the rate, so '5% of 100,000' can
        still be proved a year later.
        """
        if self.calculation == "FIXED":
            return None
        return money(basic if self.calculation == "PERCENT_OF_BASIC" else gross)

    def amount_for(self, basic, gross):
        """The earning or the slice, on this pay.

        A deduction scaled off pay is measured against the pay it is taken from
        before anything else comes off, so the same figure is used for both
        sides of the payslip and can be proved against the gross it shows.
        """
        if self.calculation == "FIXED":
            return money(self.amount)
        return money(self.measure_on(basic, gross) * self.percent / Decimal("100"))

    def is_earning(self):
        return self.component_type != "DEDUCTION"

    def is_statutory_pay(self):
        """Does this earning count in the pay the statutory shares look at?"""
        return self.is_earning() and self.counts_as_statutory_pay


class PayrollRun(BaseModel, NotesMixin):
    """One month's pay for everybody, calculated from the tables, approved by a
    second pair of hands, and booked as a single journal entry."""
    STATUS_CHOICES = [
        ("DRAFT", "Draft"),
        ("REVIEW", "Under Review"),
        ("APPROVED", "Approved"),
        ("POSTED", "Posted"),
        ("CANCELLED", "Cancelled"),
    ]
    company = models.ForeignKey(
        "organizations.Company", on_delete=models.CASCADE, related_name="payroll_runs")
    reference = models.CharField(max_length=50, db_index=True)
    period_start = models.DateField()
    period_end = models.DateField()
    payment_date = models.DateField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="DRAFT")
    total_gross = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_deductions = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_net = models.DecimalField(max_digits=15, decimal_places=2, default=Decimal("0"))
    total_employer = models.DecimalField(
        max_digits=15, decimal_places=2, default=Decimal("0"),
        help_text="What the employer's own shares add to the cost of the month.")
    salary_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_runs",
        help_text="Where basic pay and any earning without its own account is charged.")
    paid_into = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payroll_paid_runs",
        help_text="The account the net pay leaves. Blank pays out of the bank account.")
    approved_by = models.ForeignKey(
        "accounts.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="approved_payroll_runs")
    approved_at = models.DateTimeField(null=True, blank=True)
    calculated_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When the payslips were last worked out. A contract, pay element "
                  "or table changed after this makes the figures out of date, and the "
                  "run has to be recalculated before it is approved.")
    journal_entry = models.ForeignKey(
        "accounting.JournalEntry", on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-period_end"]

    def __str__(self):
        return f"{self.reference} ({self.period_start} to {self.period_end})"

    def clean(self):
        super().clean()
        # Either date can still be missing: a form that failed to read one of
        # them validates the row with a blank half of the period in it.
        if self.period_start and self.period_end and self.period_end < self.period_start:
            raise ValidationError("A pay period cannot end before it starts.")
        # One calendar month per run. Monthly pay, the PAYE steps, the pension
        # ceiling and the SHIF minimum are all figures for a calendar month, so
        # a run is worked out as a share of one: 1-15 December is 15/31 of
        # December. A run from 16 November to 15 December would be half of
        # two different months with no single table, rate or day count to read
        # — pay it as two runs, one in each month.
        if self.period_start and self.period_end and (
                (self.period_start.year, self.period_start.month)
                != (self.period_end.year, self.period_end.month)):
            first, last = month_bounds(self.period_start)
            raise ValidationError({"period_end": (
                f"A pay run has to fall within one calendar month, and "
                f"{self.period_start:%d %b %Y} to {self.period_end:%d %b %Y} spans two. "
                f"End this run on {last:%d %b %Y} and raise another for the rest — "
                "pay, tax steps and reliefs are monthly figures, worked out as a share "
                "of the month the run is in.")})
        # The payment date is the date the whole month is booked on, so a typo
        # in it posts the month's pay into a different year. Pay is not handed
        # out before the period it is for has begun, and a month paid more than
        # PAYMENT_GRACE_DAYS after it ended is a mistake far more often than it
        # is a late payroll.
        if self.payment_date and self.period_start and self.period_end:
            latest = self.period_end + datetime.timedelta(days=PAYMENT_GRACE_DAYS)
            if self.payment_date < self.period_start:
                raise ValidationError({"payment_date": (
                    f"The payment date {self.payment_date:%d %b %Y} is before the pay "
                    f"period starts on {self.period_start:%d %b %Y}. Pay is made for a "
                    "period that has begun — check the year, or the period dates.")})
            if self.payment_date > latest:
                raise ValidationError({"payment_date": (
                    f"The payment date {self.payment_date:%d %b %Y} is more than "
                    f"{PAYMENT_GRACE_DAYS} days after the period ends on "
                    f"{self.period_end:%d %b %Y}. The month would be booked on that "
                    f"date, so choose a day no later than {latest:%d %b %Y}.")})

    @property
    def total_cost(self):
        """What the month's pay cost the company: the pay, plus the employer's shares."""
        return (self.total_gross or Decimal("0")) + (self.total_employer or Decimal("0"))

    @property
    def is_editable(self):
        """Only a draft may be recalculated: an approved run is somebody else's
        decision and a posted one is in the ledger."""
        return self.status in ("DRAFT", "REVIEW")

    @property
    def month_days(self):
        """Days in the calendar month this run falls in — what pay is divided by."""
        return days_in_month(self.period_start)

    @property
    def run_days(self):
        return (self.period_end - self.period_start).days + 1

    @property
    def month_fraction(self):
        """The share of its calendar month this run covers: 1 for a whole month."""
        if self.run_days >= self.month_days:
            return Decimal("1")
        return Decimal(self.run_days) / Decimal(self.month_days)

    @property
    def is_part_month(self):
        return self.run_days < self.month_days


class Payslip(BaseModel):
    """One employee's share of a run."""
    payroll_run = models.ForeignKey(PayrollRun, on_delete=models.CASCADE, related_name="payslips")
    employee = models.ForeignKey(
        "hr.Employee", on_delete=models.PROTECT, related_name="payslips")
    gross_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total_allowances = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    taxable_pay = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="The pay income tax was worked out on: taxable earnings less any "
                  "deduction the law allows off.")
    total_deductions = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    net_salary = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    employer_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="The employer's own contributions for this person.")

    class Meta:
        ordering = ["employee__last_name", "employee__first_name"]
        unique_together = [("payroll_run", "employee")]

    def __str__(self):
        return f"{self.employee.full_name} — {self.payroll_run.reference}"

    @property
    def total_cost(self):
        return self.gross_salary + self.employer_cost

    def retotal(self):
        """Work the summary back out of the lines, so a payslip can never show a
        total that its own detail contradicts."""
        earned = money(sum((l.amount for l in self.lines.filter(kind="EARNING")), Decimal("0")))
        # Basic pay is an earning too; the allowance figure is the rest of them.
        self.gross_salary = earned
        self.total_allowances = earned - self.basic_pay()
        self.total_deductions = money(
            sum((l.amount for l in self.lines.filter(kind="DEDUCTION")), Decimal("0")))
        # Never below nought: allowable deductions bigger than the taxable pay
        # leave nothing to tax, not a negative figure to tax. What comes off is
        # each line's own tax relief, not its whole amount: a pension above the
        # monthly ceiling is deducted in full but only relieved up to it.
        self.taxable_pay = max(money(
            sum((l.amount for l in self.lines.filter(kind="EARNING", is_taxable=True)),
                Decimal("0"))
            - sum((l.tax_relief for l in self.lines.filter(kind="DEDUCTION",
                                                             reduces_tax=True)),
                  Decimal("0"))), Decimal("0.00"))
        self.employer_cost = money(
            sum((l.amount for l in self.lines.filter(kind="EMPLOYER")), Decimal("0")))
        self.net_salary = earned - self.total_deductions

    def basic_pay(self):
        """Basic pay for the period. A month paid on two contracts — a raise
        from the 16th — has a basic line for each, so they are added up."""
        return money(sum((l.amount for l in self.lines.filter(kind="EARNING", code="BASIC")),
                         Decimal("0")))


class PayslipLine(models.Model):
    """One figure on a payslip, with the rate and the pay that produced it.

    A payslip has to be auditable without a phone call: this row says the pension
    was 6% of 36,000, not just that it was 2,160.
    """
    KINDS = [
        ("EARNING", "Earning"),
        ("DEDUCTION", "Deduction"),
        ("EMPLOYER", "Employer contribution"),
    ]
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, related_name="lines")
    kind = models.CharField(max_length=10, choices=KINDS)
    code = models.CharField(max_length=20, blank=True, default="")
    description = models.CharField(max_length=200)
    basis = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="The pay the figure was taken from, after any ceiling.")
    percent = models.DecimalField(max_digits=9, decimal_places=6, default=Decimal("0"))
    amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    is_taxable = models.BooleanField(default=False)
    reduces_tax = models.BooleanField(default=False)
    tax_relief = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0"),
        help_text="How much of a deduction that reduces taxable pay was actually taken "
                  "off it. The whole amount, except a pension contribution above the "
                  "monthly ceiling on pension relief.")
    rule = models.ForeignKey(
        StatutoryRule, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payslip_lines")
    component = models.ForeignKey(
        SalaryComponent, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payslip_lines")
    gl_account = models.ForeignKey(
        "accounting.Account", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="payslip_lines")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "id"]

    def __str__(self):
        return f"{self.description}: {self.amount}"
