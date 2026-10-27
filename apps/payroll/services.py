"""Payroll: read the tables, work the month out, and pay it once.

Nothing in here knows what the pension rate is. It looks the rate up in the
company's own table as it stood on the last day of the pay period, applies it to
the pay the table says it applies to, and writes down both the figure and the
basis it was taken from, so a payslip can be proved months later without anyone
remembering anything.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from .models import (
    Payslip, PayslipLine, SalaryComponent, StatutoryRule, money,
)

Z = Decimal("0")

# The table a new employer starts from. Every row of it becomes data the
# accountant can change — these are opening figures, not rules the program
# enforces. (code, name, method, basis, employee %, employer %, cap, relief,
# allowable against tax, payable account, employer expense account, bands,
# employee minimum)
#
# Kenya, as at 2025: NSSF 6% + 6% up to the 72,000 upper earnings limit; SHIF
# 2.75% from the employee only, never below 300; housing levy 1.5% + 1.5%. All
# three come off pay before PAYE (Tax Laws (Amendment) Act 2024). PAYE bands
# from the Finance Act 2023, personal relief 2,400.
DEFAULT_TABLE = [
    ("NSSF", "Pension — NSSF", "PERCENT", "BASIC", "6", "6", "72000", None, True,
     "2400", "6110", [], None),
    ("SHIF", "Health insurance — SHIF", "PERCENT", "GROSS", "2.75", "0",
     None, None, True, "2500", None, [], "300"),
    ("HOUSING", "Housing Levy", "PERCENT", "GROSS", "1.5", "1.5", None, None,
     True, "2550", "6130", [], None),
    ("PAYE", "Pay as you earn", "BANDS", "GROSS", "0", "0", None, "2400", False,
     "2300", None, [("0", "24000", "10"), ("24000", "32333", "25"),
                    ("32333", "500000", "30"), ("500000", "800000", "32.5"),
                    ("800000", None, "35")], None),
]


def overlapping_runs(company, start, end, exclude=None):
    """Live payroll runs that pay any of the days from `start` to `end`."""
    from .models import PayrollRun

    runs = PayrollRun.objects.filter(
        company=company, period_start__lte=end, period_end__gte=start,
    ).exclude(status="CANCELLED")
    if exclude is not None and exclude.pk and not exclude._state.adding:
        runs = runs.exclude(pk=exclude.pk)
    return runs.order_by("period_start")


def rules_as_at(company, on_date):
    """The table in force on that day: one rule per code, the newest that has started."""
    rules = StatutoryRule.objects.filter(
        company=company, is_active=True, effective_from__lte=on_date)
    latest = {}
    for rule in rules.order_by("code", "-effective_from"):
        latest.setdefault(rule.code, rule)
    # A tiered table is worked out last: it taxes what is left after the
    # allowable deductions have come off.
    return sorted(latest.values(), key=lambda r: (r.method == "BANDS", r.code))


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
    for (code, name, method, basis, employee_percent, employer_percent, cap,
         relief, allowable, payable_code, expense_code, bands, minimum) in DEFAULT_TABLE:
        if StatutoryRule.objects.filter(company=company, code=code).exists():
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
            employee_minimum=Decimal(minimum) if minimum else Z,
            reduces_taxable_pay=allowable, payable_account=payable,
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


def current_contract(employee, period_start, period_end):
    """The contract a month's pay is worked from.

    Being flagged current is not enough on its own: a contract that ended before
    the period began must not pay into it, and one that starts after the period
    ends is next month's business.
    """
    return employee.contracts.filter(
        is_current=True, start_date__lte=period_end,
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=period_start),
    ).order_by("-start_date").first()


def build_payslip(run, employee, rules):
    """One person's month, written out line by line from the tables.

    Returns the saved payslip. Raises when the tables cannot be applied to this
    pay honestly — a negative take-home is a table that is wrong, not a figure to
    book.
    """
    contract = current_contract(employee, run.period_start, run.period_end)
    basic = money(contract.basic_salary) if contract else None
    if basic is None:
        return None

    slip = Payslip.objects.create(payroll_run=run, employee=employee)
    payslip_lines = []
    earned = basic
    payslip_lines.append(dict(
        kind="EARNING", code="BASIC", description="Basic salary", basis=basic,
        amount=basic, is_taxable=True))

    components = list(components_for(run.company_id, employee))
    # A percentage is always measured on pay that is already known: allowances
    # come off basic, and anything taken off gross is measured on the gross
    # before those deductions, so the same table twice gives the same run.
    fixed_rows = [c for c in components if c.calculation == "FIXED"]
    basic_rows = [c for c in components if c.calculation == "PERCENT_OF_BASIC"]
    gross_rows = [c for c in components if c.calculation == "PERCENT_OF_GROSS"]

    def emit(component, amount, measured):
        """One pay element on the slip, beside the pay its rate was read off."""
        nonlocal earned
        if amount <= 0:
            return
        row = dict(code=component.component_type, description=component.name,
                   basis=measured if measured is not None else amount,
                   percent=component.percent, amount=amount,
                   gl_account_id=component.gl_account_id,
                   component_id=component.pk)
        if component.is_earning():
            earned += amount
            row.update(kind="EARNING", is_taxable=component.is_taxable)
        else:
            row.update(kind="DEDUCTION")
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
    for rule in rules:
        employee_share = rule.employee_amount(basic, earned)
        employer_share = rule.employer_amount(basic, earned)
        if rule.method == "BANDS":
            # Income tax is the last figure of the month, because it is worked
            # out on what is left after the allowable deductions.
            taxed = money(taxable)
            employee_share = rule.tax_on(taxable) - money(rule.monthly_relief)
            if employee_share < 0:
                employee_share = Z
        else:
            taxed = rule.basis_for(basic, earned)
        if employee_share > 0:
            deductions += employee_share
            if rule.reduces_taxable_pay:
                taxable -= employee_share
            payslip_lines.append(dict(
                kind="DEDUCTION", code=rule.code, description=rule.name,
                basis=taxed,
                percent=rule.employee_percent, amount=employee_share,
                reduces_tax=rule.reduces_taxable_pay, rule_id=rule.pk,
                gl_account_id=rule.payable_account_id))
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


def calculate(run):
    """Work the whole run out from the tables, replacing any draft figures.

    Handing back the names of the people who could not be paid is as important
    as the figures: an empty payslip column on a run the bookkeeper approves is
    how somebody goes a month unpaid without anyone noticing.
    """
    from apps.hr.models import Employee

    if not run.is_editable:
        raise ValidationError(
            f"'{run.reference}' is {run.status}, so its figures are settled. A posted "
            "run is in the ledger and an approved one is somebody's decision — reverse "
            "the entry and raise a fresh run instead of recalculating this one.")
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
    for rule in rules:
        if rule.method == "BANDS" and not rule.tax_bands.exists():
            raise ValidationError(
                f"'{rule.code}' is set up as a tiered table but has no bands, so it "
                "would tax nobody. Add the steps to the table before the run is "
                "calculated.")
    unpaid = list(Employee.objects.filter(
        company=run.company, status__in=["ACTIVE", "PROBATION"],
        is_active=True).order_by("last_name", "first_name"))

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
        run.save(update_fields=[
            "total_gross", "total_deductions", "total_net", "total_employer",
            "updated_at"])
    return {"count": len(slips), "unpaid": missing}
