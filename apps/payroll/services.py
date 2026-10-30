"""Payroll: read the tables, work the month out, and pay it once.

Nothing in here knows what the pension rate is. It looks the rate up in the
company's own table as it stood on the last day of the pay period, applies it to
the pay the table says it applies to, and writes down both the figure and the
basis it was taken from, so a payslip can be proved months later without anyone
remembering anything.
"""
import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    APPROVED_RUNS, PAYABLE_STATUSES, PENSION_RELIEF_CAP, Payslip, PayslipLine,
    SalaryComponent, StatutoryRule, money,
)

Z = Decimal("0")

# The table a new employer starts from. Every row of it becomes data the
# accountant can change — these are opening figures, not rules the program
# enforces. (code, name, method, basis, employee %, employer %, cap, relief,
# allowable against tax, payable account, employer expense account, bands, and
# optionally the employee's monthly minimum)
#
# Kenya as at February 2026: NSSF is 6% each of pensionable (gross) pay up to the
# year-four ceiling of 108,000; SHIF is the employee's alone; and since December
# 2024 SHIF and the housing levy both come off the pay before PAYE is worked out.
# The PAYE steps are 24,000 / 32,333 / 500,000 / 800,000, less 2,400 relief.
# SHIF is never less than 300 a month for anybody who is paid at all.
DEFAULT_TABLE = [
    ("NSSF", "Pension — NSSF", "PERCENT", "GROSS", "6", "6", "108000", None, True,
     "2400", "6110", []),
    ("SHIF", "Health insurance — SHIF", "PERCENT", "GROSS", "2.75", "0",
     None, None, True, "2500", None, [], "300"),
    ("HOUSING", "Housing Levy", "PERCENT", "GROSS", "1.5", "1.5", None, None,
     True, "2550", "6130", []),
    ("PAYE", "Pay as you earn", "BANDS", "GROSS", "0", "0", None, "2400", False,
     "2300", None, [("0", "24000", "10"), ("24000", "32333", "25"),
                    ("32333", "500000", "30"), ("500000", "800000", "32.5"),
                    ("800000", None, "35")]),
]


def overlapping_run(company, start, end, exclude=None):
    """A live run that already pays any day of this period, or None.

    Matching only the exact dates is not enough: 1-31 October beside 1-30
    October pays the whole of October twice.
    """
    from .models import PayrollRun

    runs = PayrollRun.objects.filter(
        company=company, period_start__lte=end, period_end__gte=start,
    ).exclude(status="CANCELLED")
    if exclude is not None and exclude.pk:
        runs = runs.exclude(pk=exclude.pk)
    return runs.order_by("period_start").first()


def rules_as_at(company, on_date):
    """The table in force on that day: one rule per code, the newest that has started."""
    rules = StatutoryRule.objects.filter(
        company=company, is_active=True, effective_from__lte=on_date)
    latest = {}
    for rule in rules.order_by("code", "-effective_from"):
        # Grouped without regard to case: a row saved as 'nssf' before codes
        # were stored in capitals is a version of NSSF, not a second pension.
        latest.setdefault(rule.code.upper(), rule)
    # A tiered table is worked out last: it taxes what is left after the
    # allowable deductions have come off.
    return sorted(latest.values(), key=lambda r: (r.method == "BANDS", r.code))


def untaxed_reason(company, on_date):
    """Why a run on this day would withhold no income tax, or "" when that is fine.

    A book that has never kept a tiered table simply does not run income tax
    through payroll, and that is its business. But a book that has one — even
    retired, even only starting later — and has none in force on the period end
    has lost its tax table, usually by retiring the old row before the new one
    starts. Working the month out anyway hands everybody their pay with no PAYE
    taken off and calls it a success; the authority is still owed the tax, and
    the company now has to find it from the staff after they have spent it.
    """
    tables = StatutoryRule.objects.filter(company=company, method="BANDS")
    if not tables.exists():
        return ""
    retired = tables.filter(is_active=False, effective_from__lte=on_date).order_by(
        "-effective_from").first()
    later = tables.filter(is_active=True, effective_from__gt=on_date).order_by(
        "effective_from").first()
    if retired:
        detail = (f"the {retired.code} table from {retired.effective_from:%d %b %Y} "
                  "has been retired")
        if later:
            detail += (f" and the next one only starts on "
                       f"{later.effective_from:%d %b %Y}")
        fix = (f"Re-activate {retired.code} ({retired.effective_from:%d %b %Y}) — a "
               "dated change only needs the new row, not the old one retired — or "
               "date a table to start on or before the period end.")
    elif later:
        detail = (f"the only {later.code} table starts on "
                  f"{later.effective_from:%d %b %Y}")
        fix = ("Add a table that starts on or before the period end, or move the run "
               "to a month the table covers.")
    else:
        detail = "no tiered table is active"
        fix = "Re-activate the income-tax table before the run is worked out."
    return (f"No income-tax table is in force on {on_date:%d %b %Y}: {detail}. A run "
            f"worked out now would pay everybody with no PAYE withheld. {fix}")


def ensure_default_table(company, effective_from):
    """Give a new book the statutory table it is expected to run, once.

    The accounts a contribution needs are made on first use: a company founded
    before the housing levy existed has nowhere to remit it, and abandoning the
    row would leave the bookkeeper with a silent gap in the withholding.
    """
    from apps.accounting.gl_service import ensure_account
    from apps.accounting.models import Account

    def active(code):
        return Account.objects.filter(company=company, code=code, is_active=True).first()

    created = []
    for row in DEFAULT_TABLE:
        (code, name, method, basis, employee_percent, employer_percent, cap,
         relief, allowable, payable_code, expense_code, bands) = row[:12]
        minimum = row[12] if len(row) > 12 else None
        if StatutoryRule.objects.filter(company=company, code__iexact=code).exists():
            continue
        payable = active(payable_code) or ensure_account(
            company, payable_code, f"{name} Payable", "LIABILITY", "2000")
        expense = None
        if expense_code:
            expense = active(expense_code) or ensure_account(
                company, expense_code, f"{name} — Employer", "EXPENSE", "6100")
        rule = StatutoryRule.objects.create(
            company=company, code=code, name=name, method=method, basis=basis,
            employee_percent=Decimal(employee_percent) if employee_percent else Z,
            employer_percent=Decimal(employer_percent) if employer_percent else Z,
            basis_cap=Decimal(cap) if cap else None,
            monthly_relief=Decimal(relief) if relief else Z,
            reduces_taxable_pay=allowable, payable_account=payable,
            # NSSF is the pension in the table: its relief shares the monthly
            # pension ceiling with any private scheme on the payslip.
            is_pension=(code == "NSSF"),
            employee_minimum=Decimal(minimum) if minimum else None,
            expense_account=expense, effective_from=effective_from,
        )
        for lower, upper, percent in bands:
            rule.tax_bands.create(
                lower=Decimal(lower),
                upper=Decimal(upper) if upper else None,
                percent=Decimal(percent))
        created.append(rule.code)
    return created


def components_for(company, employee):
    """This person's own earnings and deductions, plus whatever the company pays
    to everybody. A retired element pays nobody, even on an older run."""
    return SalaryComponent.objects.filter(
        Q(employee__isnull=True) | Q(employee=employee),
        company=company, is_active=True).order_by("component_type", "name")


def contract_segments(employee, period_start, period_end):
    """Which contracts paid which days of the period, as [(contract, ranges)].

    `ranges` is a list of (first_day, last_day) pairs, both ends included, so
    pay can be worked out as a share of the calendar month day by day.

    A month is paid from the contracts that actually covered it, by their own
    start and end dates — not from whichever one happens to be flagged current
    today. That is what lets a leaver be paid for the fortnight they worked
    after their contract has ended, a raise from the 16th pay the old rate for
    the first half and the new one for the second, and a cancelled month be run
    again months later from the contracts in force at the time.

    Contracts are no longer allowed to overlap (apps.hr.models.Contract.clean),
    but older books may still hold two that do. Each day then goes to one
    contract only: the one that started later, because it is the newer
    statement of the person's pay. The current flag is deliberately not the
    tie-break — it is a label for today, and a back-dated contract typed with
    the box left ticked must never take the days of the contract that replaced
    it. Nothing is paid after the employee's termination date. The contracts
    are listed in date order.
    """
    last_day = period_end
    if employee.date_terminated and employee.date_terminated < last_day:
        last_day = employee.date_terminated
    if last_day < period_start:
        return []
    contracts = list(employee.contracts.filter(
        start_date__lte=last_day,
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=period_start),
    ))
    if not contracts:
        return []
    ranked = sorted(contracts, key=lambda c: (c.start_date, c.created_at), reverse=True)
    ranges = {}
    day = period_start
    while day <= last_day:
        for contract in ranked:
            if contract.start_date <= day and (
                    contract.end_date is None or contract.end_date >= day):
                spans = ranges.setdefault(contract.pk, [])
                if spans and spans[-1][1] == day - datetime.timedelta(days=1):
                    spans[-1] = (spans[-1][0], day)
                else:
                    spans.append((day, day))
                break
        day += datetime.timedelta(days=1)
    paid = [c for c in contracts if c.pk in ranges]
    return [(c, ranges[c.pk])
            for c in sorted(paid, key=lambda c: (c.start_date, c.created_at))]


def span_days(ranges):
    """How many days a list of (first, last) ranges covers."""
    return sum(((last - first).days + 1 for first, last in ranges), 0)


def contract_days(employee, period_start, period_end):
    """Which contracts paid how many days of the period, as [(contract, days)]."""
    return [(c, span_days(r))
            for c, r in contract_segments(employee, period_start, period_end)]


def current_contract(employee, period_start, period_end):
    """The latest contract that pays any day of the period, or None.

    Used to say whether somebody can be paid at all; the pay itself is worked
    out day by day from `contract_segments`.
    """
    paid = contract_days(employee, period_start, period_end)
    return paid[-1][0] if paid else None


def period_days(period_start, period_end):
    """Calendar days in the pay period, both ends included."""
    return (period_end - period_start).days + 1


def share_of_month(monthly, ranges, month_days):
    """A monthly figure, for the days of its calendar month in `ranges`.

    Pay is divided by the days in the calendar month, not by the length of
    the run: a 31,000 salary pays 1,000 a day in December whether December is
    paid as one run or as two. Each range is priced as the difference between
    "the month so far" at its last day and at the day before its first —
    money(31,000 x 15/31) for 1-15 December, then 31,000 less that for 16-31 —
    so the parts of a month always add up to exactly the monthly figure, with
    no cent lost or gained to rounding however the month is cut.
    """
    monthly = Decimal(monthly)
    if span_days(ranges) >= month_days:
        return money(monthly)
    total = Z
    for first, last in ranges:
        total += (money(monthly * Decimal(last.day) / Decimal(month_days))
                  - money(monthly * Decimal(first.day - 1) / Decimal(month_days)))
    return money(total)


def build_payslip(run, employee, rules):
    """One person's month, written out line by line from the tables.

    Returns the saved payslip. Raises when the tables cannot be applied to this
    pay honestly — a negative take-home is a table that is wrong, not a figure to
    book.

    Three rules decide the figures, and the payslip says each one out loud:

    * Pay is a share of the calendar month. A contract's basic and every fixed
      element marked "pro-rate in part months" are paid for the days of the
      month the person was employed, divided by the days in that month.
    * The statutory tables are read as they stood on the period end. A rate
      that changes in the middle of a month applies to the whole of that month
      (the run is not split at the change); the slip names the table it used.
    * A run for part of a month applies the same part of every monthly figure
      in the tables — PAYE steps, relief, pension ceiling, SHIF minimum — so
      two half-month runs withhold what one whole month would.
    """
    segments = contract_segments(employee, run.period_start, run.period_end)
    if not segments:
        return None
    month_days = run.month_days
    fraction = run.month_fraction
    paid_ranges = [span for _, spans in segments for span in spans]
    paid_days = span_days(paid_ranges)

    slip = Payslip.objects.create(payroll_run=run, employee=employee)
    payslip_lines = []
    basic = Z
    for contract, spans in segments:
        days = span_days(spans)
        amount = share_of_month(contract.basic_salary, spans, month_days)
        basic += amount
        if days >= month_days:
            description = "Basic salary"
        else:
            # Pro-rated pay says how it was pro-rated, so the slip can be
            # checked against the contract without anybody's help.
            description = (f"Basic salary — {days} of {month_days} days at "
                           f"{money(contract.basic_salary):,} a month")
        payslip_lines.append(dict(
            kind="EARNING", code="BASIC", description=description,
            basis=money(contract.basic_salary), amount=amount, is_taxable=True))
    basic = money(basic)
    earned = basic
    # The pay the statutory shares are worked out on. Basic pay always counts;
    # an earning that only gives the employee their own money back (mileage,
    # a refund of costs) does not — it is not pay, and charging pension and
    # health shares on it takes a cut of a reimbursement.
    statutory_gross = basic

    # The month's ceiling on pension relief, for the share of the month the
    # run covers, spent by every pension deduction in turn.
    pension_cap = money(PENSION_RELIEF_CAP * fraction)
    cap_left = pension_cap

    def relieve(amount):
        """How much of a pension deduction may come off taxable pay."""
        nonlocal cap_left
        allowed = min(amount, max(cap_left, Z))
        cap_left -= allowed
        return money(allowed)

    components = list(components_for(run.company_id, employee))
    # A percentage is always measured on pay that is already known: allowances
    # come off basic, and anything taken off gross is measured on the gross
    # before those deductions, so the same table twice gives the same run.
    fixed_rows = [c for c in components if c.calculation == "FIXED"]
    basic_rows = [c for c in components if c.calculation == "PERCENT_OF_BASIC"]
    gross_rows = [c for c in components if c.calculation == "PERCENT_OF_GROSS"]

    def emit(component, amount, measured):
        """One pay element on the slip, beside the pay its rate was read off."""
        nonlocal earned, statutory_gross
        description = component.name
        if (component.calculation == "FIXED" and component.prorate_part_months
                and paid_days < month_days):
            # A monthly figure for somebody paid for part of the month is
            # paid for the same part of it as their basic pay.
            measured = amount
            amount = share_of_month(amount, paid_ranges, month_days)
            description = f"{component.name} — {paid_days} of {month_days} days"
        if amount <= 0:
            return
        row = dict(code=component.component_type, description=description,
                   basis=measured if measured is not None else amount,
                   percent=component.percent, amount=amount,
                   gl_account_id=component.gl_account_id,
                   component_id=component.pk)
        if component.is_earning():
            earned += amount
            if component.is_statutory_pay():
                statutory_gross += amount
            row.update(kind="EARNING", is_taxable=component.is_taxable)
        else:
            # A deduction the tax authority allows off the pay is a registered
            # pension contribution, and shares the monthly pension ceiling
            # with NSSF: deducted in full, relieved only up to the ceiling.
            relief = relieve(amount) if component.reduces_taxable_pay else Z
            if component.reduces_taxable_pay and relief < amount:
                row["description"] = (f"{description} (tax relief {relief:,} — pension "
                                      f"ceiling {pension_cap:,} reached)")
            row.update(kind="DEDUCTION", reduces_tax=component.reduces_taxable_pay,
                       tax_relief=relief)
        payslip_lines.append(row)

    for component in fixed_rows + basic_rows:
        emit(component, component.amount_for(basic, basic),
             component.measure_on(basic, basic))
    reference_gross = earned
    for component in gross_rows:
        emit(component, component.amount_for(basic, reference_gross),
             component.measure_on(basic, reference_gross))

    # What comes off the pay is every deduction on the slip, not just the ones the
    # law ordered withheld: a bond repayment left out here is a take-home that
    # pays out money already promised to somebody else.
    deductions = sum((row["amount"] for row in payslip_lines
                      if row["kind"] == "DEDUCTION"), Z)
    employer_total = Z
    taxable = sum((row["amount"] for row in payslip_lines
                   if row["kind"] == "EARNING" and row["is_taxable"]), Z)
    taxable -= sum((row.get("tax_relief", Z) for row in payslip_lines
                    if row["kind"] == "DEDUCTION" and row.get("reduces_tax")), Z)
    part = (f" — steps and relief for {run.run_days} of {month_days} days"
            if fraction < 1 else "")
    for rule in rules:
        employee_share = rule.employee_amount(basic, statutory_gross, fraction)
        employer_share = rule.employer_amount(basic, statutory_gross, fraction)
        description = rule.name
        if rule.method == "BANDS":
            # Income tax is the last figure of the month, because it is worked
            # out on what is left after the allowable deductions — never on
            # less than nothing.
            taxable = max(taxable, Z)
            taxed = money(taxable)
            employee_share = rule.tax_on(taxable, fraction) - rule.relief_for(fraction)
            if employee_share < 0:
                employee_share = Z
            description = f"{rule.name}{part}"
        else:
            taxed = rule.basis_for(basic, statutory_gross, fraction)
        if employee_share > 0:
            deductions += employee_share
            relief = Z
            if rule.reduces_taxable_pay:
                relief = relieve(employee_share) if rule.is_pension else employee_share
                taxable -= relief
                if relief < employee_share:
                    description = (f"{rule.name} (tax relief {relief:,} — pension "
                                   f"ceiling {pension_cap:,} reached)")
            payslip_lines.append(dict(
                kind="DEDUCTION", code=rule.code, description=description,
                basis=taxed,
                percent=rule.employee_percent, amount=employee_share,
                reduces_tax=rule.reduces_taxable_pay, tax_relief=relief,
                rule_id=rule.pk, gl_account_id=rule.payable_account_id))
        if employer_share > 0:
            employer_total += employer_share
            payslip_lines.append(dict(
                kind="EMPLOYER", code=rule.code,
                description=f"{rule.name} (employer)",
                basis=taxed,
                percent=rule.employer_percent, amount=employer_share,
                rule_id=rule.pk, gl_account_id=rule.payable_account_id))

    net = earned - deductions
    if net < 0:
        slip.delete()
        raise ValidationError(
            f"{employee.full_name}'s deductions ({deductions}) are more than their pay "
            f"({earned}). The tables cannot be applied to this pay as it stands — "
            "retire the rule, or leave this person off the run.")

    for row in payslip_lines:
        PayslipLine.objects.create(payslip=slip, **row)
    slip.retotal()
    slip.save()
    return slip


def payable_staff(company, period_start):
    """The people a run for a period starting on that day has to pay.

    Everybody on a payable status, and also a leaver whose last day falls in
    or after the period: their status already says TERMINATED, but the days
    they worked before going are still owed. Somebody who left before the
    period began is not on it.
    """
    from apps.hr.models import Employee

    return Employee.objects.filter(
        company=company, is_active=True,
    ).filter(
        Q(status__in=PAYABLE_STATUSES) | Q(date_terminated__isnull=False),
    ).filter(
        Q(date_terminated__isnull=True) | Q(date_terminated__gte=period_start),
    ).order_by("last_name", "first_name")


def calculate(run):
    """Work the whole run out from the tables, replacing any draft figures.

    Handing back the names of the people who could not be paid is as important
    as the figures: an empty payslip column on a run the bookkeeper approves is
    how somebody goes a month unpaid without anyone noticing.
    """
    if not run.is_editable:
        raise ValidationError(
            f"'{run.reference}' is {run.status}, so its figures are settled. A posted "
            "run is in the ledger and an approved one is somebody's decision — reverse "
            "the entry and raise a fresh run instead of recalculating this one.")
    if (run.period_start.year, run.period_start.month) != (
            run.period_end.year, run.period_end.month):
        # Saved before runs were held to one calendar month. Its pay cannot be
        # divided by "the days in the month" when it is in two of them.
        raise ValidationError(
            f"'{run.reference}' runs from {run.period_start:%d %b %Y} to "
            f"{run.period_end:%d %b %Y}, across two calendar months. Pay, tax steps "
            "and reliefs are monthly figures, so a run has to fall within one month — "
            "amend the period to end on the last day of the first month, and raise "
            "a second run for the rest.")
    rules = rules_as_at(run.company, run.period_end)
    if not rules:
        raise ValidationError(
            "This company has no statutory table yet, so a run would pay people "
            "gross and withhold nothing. Set the rates up first.")
    tiered = [r.code for r in rules if r.method == "BANDS"]
    if len(tiered) > 1:
        raise ValidationError(
            f"{' and '.join(tiered)} are both tiered tables in force on "
            f"{run.period_end}. Two of them would each tax the same pay, so retire "
            "one before the run is worked out.")
    if not tiered:
        missing_tax = untaxed_reason(run.company, run.period_end)
        if missing_tax:
            raise ValidationError(missing_tax)
    for rule in rules:
        if rule.method == "BANDS" and not rule.tax_bands.exists():
            raise ValidationError(
                f"'{rule.code}' is set up as a tiered table but has no bands, so it "
                "would tax nobody. Add the steps to the table before the run is "
                "calculated.")
    unpaid = list(payable_staff(run.company, run.period_start))

    with transaction.atomic():
        run.payslips.all().delete()
        missing = []
        slips = []
        for employee in unpaid:
            if current_contract(employee, run.period_start, run.period_end) is None:
                missing.append(employee.full_name)
                continue
            slip = build_payslip(run, employee, rules)
            if slip is not None:
                slips.append(slip)
        run.total_gross = sum((s.gross_salary for s in slips), Z)
        run.total_deductions = sum((s.total_deductions for s in slips), Z)
        run.total_net = sum((s.net_salary for s in slips), Z)
        run.total_employer = sum((s.employer_cost for s in slips), Z)
        # The moment the figures were worked out, so approval can tell when a
        # contract, pay element or table has changed underneath them since.
        run.calculated_at = timezone.now()
        run.save(update_fields=[
            "total_gross", "total_deductions", "total_net", "total_employer",
            "calculated_at", "updated_at"])
    return {"count": len(slips), "unpaid": missing}


def stale_reason(run):
    """Why this draft's figures may no longer be what the tables say, or "".

    A run is worked out from the contracts, the pay elements, the staff list
    and the statutory tables as they stood when it was calculated. If any of
    them changed afterwards — a raise typed in, an allowance retired, a leaving
    date recorded, a band corrected — the payslips on the draft describe a
    month that no longer exists, and approving them pays the old figures. The
    approver cannot see that from the run itself, so it is checked here.
    """
    from apps.hr.models import Contract, Employee

    if run.calculated_at is None:
        return (f"'{run.reference}' has no record of when its figures were worked "
                "out, so nobody can tell whether they still match the contracts and "
                "tables. Recalculate it, then approve it.")
    since = run.calculated_at
    changed = []
    for contract in Contract.objects.filter(
            employee__company=run.company, updated_at__gt=since).select_related(
            "employee")[:3]:
        changed.append(f"{contract.employee.full_name}'s contract from "
                       f"{contract.start_date:%d %b %Y}")
    for employee in Employee.objects.filter(
            company=run.company, updated_at__gt=since)[:3]:
        changed.append(f"{employee.full_name}'s employee record")
    for component in SalaryComponent.objects.filter(
            company=run.company, updated_at__gt=since)[:3]:
        changed.append(f"the pay element '{component.name}'")
    for rule in StatutoryRule.objects.filter(
            company=run.company, effective_from__lte=run.period_end,
            updated_at__gt=since)[:3]:
        changed.append(f"the {rule.code} table from {rule.effective_from:%d %b %Y}")
    if not changed:
        return ""
    worked = timezone.localtime(since)
    return (f"'{run.reference}' was worked out on {worked:%d %b %Y} at {worked:%H:%M}, "
            f"and since then {', '.join(changed[:4])}"
            f"{' and more' if len(changed) > 4 else ''} changed. Its payslips may no "
            "longer be what the tables say — ask payroll to recalculate the run, check "
            "it, and then approve it.")
