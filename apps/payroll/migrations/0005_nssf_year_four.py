"""Bring the NSSF row 0003 wrote up to the February 2026 figures.

0003 corrected the opening table to the year-three NSSF limits: 6% + 6% of
basic pay, capped at 72,000. Since February 2026 (year four of the NSSF Act
2013 phase-in) the ceiling is 108,000, and the contribution is worked on
pensionable pay, which is gross pay rather than basic. New companies already
start from those figures (services.DEFAULT_TABLE).

Only a row still holding exactly what 0003 wrote is touched. As in 0003, a run
already approved or posted keeps the table it was worked from, so the new
figures start after the last of those; a row nothing settled was worked from
is corrected in place. A table the accountant has changed is theirs and is
left alone. Draft runs need recalculating.
"""
import datetime
from decimal import Decimal

from django.db import migrations
from django.db.models import Max

D = Decimal

OLD = ("BASIC", D("6"), D("6"), D("72000"), True, D("0"))
NEW = ("GROSS", D("6"), D("6"), D("108000"), True, D("0"))


def _figures(rule):
    return (rule.basis, rule.employee_percent, rule.employer_percent, rule.basis_cap,
            rule.reduces_taxable_pay, rule.employee_minimum)


def year_four(apps, schema_editor):
    StatutoryRule = apps.get_model("payroll", "StatutoryRule")
    PayslipLine = apps.get_model("payroll", "PayslipLine")
    first_of_month = datetime.date.today().replace(day=1)
    for rule in StatutoryRule.objects.filter(code="NSSF", is_active=True).order_by(
            "company_id", "-effective_from"):
        latest = StatutoryRule.objects.filter(
            company_id=rule.company_id, code="NSSF", is_active=True,
        ).order_by("-effective_from").first()
        if latest.pk != rule.pk or _figures(rule) != OLD:
            continue
        settled_end = PayslipLine.objects.filter(
            rule_id=rule.pk,
            payslip__payroll_run__status__in=["APPROVED", "POSTED"],
        ).aggregate(end=Max("payslip__payroll_run__period_end"))["end"]
        start = first_of_month
        if settled_end and settled_end >= start:
            start = (settled_end.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
        if rule.effective_from >= start or not settled_end:
            target = rule  # nothing settled was worked from it: correct it in place
        elif StatutoryRule.objects.filter(
                company_id=rule.company_id, code="NSSF", effective_from=start).exists():
            continue  # a retired row already holds that date; leave it to the accountant
        else:
            target = StatutoryRule(
                company_id=rule.company_id, code="NSSF", name=rule.name,
                method=rule.method, employee_flat=rule.employee_flat,
                employer_flat=rule.employer_flat, monthly_relief=rule.monthly_relief,
                payable_account_id=rule.payable_account_id,
                expense_account_id=rule.expense_account_id, effective_from=start)
        (target.basis, target.employee_percent, target.employer_percent,
         target.basis_cap, target.reduces_taxable_pay, target.employee_minimum) = NEW
        target.save()


class Migration(migrations.Migration):

    dependencies = [
        ("payroll", "0004_statutory_pay_flags"),
    ]

    operations = [
        migrations.RunPython(year_four, migrations.RunPython.noop),
    ]
