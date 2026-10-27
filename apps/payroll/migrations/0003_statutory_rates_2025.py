"""Correct the statutory table that new companies were given.

The opening table had the 25% PAYE band ending at 33,333 (the law says
32,333), the NSSF ceiling at 36,000 (now 72,000), an employer share of SHIF
(there is none), no SHIF floor of 300, and SHIF and the housing levy taxed
(both have come off pay before PAYE since 27 Dec 2024).

A company whose rows still hold exactly those opening figures gets a new row
per code, starting on the first of this month (or after the last approved or
posted run worked from the old row, if that is later), so those runs keep the
table they were worked from. Draft runs need recalculating. A table
the accountant has changed is theirs and is left alone.
"""
import datetime
from decimal import Decimal

from django.db import migrations, models
from django.db.models import Max

D = Decimal

# code: (old figures, new figures). Figures: basis, employee %, employer %,
# cap, reduces taxable pay, employee minimum, bands.
OLD_PAYE = [(D("0"), D("24000"), D("10")), (D("24000"), D("33333"), D("25")),
            (D("33333"), D("500000"), D("30")), (D("500000"), D("800000"), D("32.5")),
            (D("800000"), None, D("35"))]
NEW_PAYE = [(D("0"), D("24000"), D("10")), (D("24000"), D("32333"), D("25")),
            (D("32333"), D("500000"), D("30")), (D("500000"), D("800000"), D("32.5")),
            (D("800000"), None, D("35"))]
CHANGES = {
    "NSSF": (("BASIC", D("6"), D("6"), D("36000"), True, D("0"), None),
             ("BASIC", D("6"), D("6"), D("72000"), True, D("0"), None)),
    "SHIF": (("GROSS", D("2.75"), D("2.75"), None, False, D("0"), None),
             ("GROSS", D("2.75"), D("0"), None, True, D("300"), None)),
    "HOUSING": (("GROSS", D("1.5"), D("1.5"), None, False, D("0"), None),
                ("GROSS", D("1.5"), D("1.5"), None, True, D("0"), None)),
    "PAYE": (("GROSS", D("0"), D("0"), None, False, D("0"), OLD_PAYE),
             ("GROSS", D("0"), D("0"), None, False, D("0"), NEW_PAYE)),
}


def _figures(rule):
    bands = None
    if rule.method == "BANDS":
        bands = [(b.lower, b.upper, b.percent) for b in rule.tax_bands.order_by("lower")]
    return (rule.basis, rule.employee_percent, rule.employer_percent, rule.basis_cap,
            rule.reduces_taxable_pay, rule.employee_minimum, bands)


def correct_opening_table(apps, schema_editor):
    StatutoryRule = apps.get_model("payroll", "StatutoryRule")
    PayslipLine = apps.get_model("payroll", "PayslipLine")
    first_of_month = datetime.date.today().replace(day=1)

    for code, (old, new) in CHANGES.items():
        for rule in StatutoryRule.objects.filter(code=code, is_active=True).order_by(
                "company_id", "-effective_from"):
            latest = StatutoryRule.objects.filter(
                company_id=rule.company_id, code=code, is_active=True,
            ).order_by("-effective_from").first()
            if latest.pk != rule.pk or _figures(rule) != old:
                continue
            basis, emp, empr, cap, allowable, minimum, bands = new
            # A run already approved or posted keeps the table it was worked
            # from, so the new figures start after the last of those.
            settled_end = PayslipLine.objects.filter(
                rule_id=rule.pk,
                payslip__payroll_run__status__in=["APPROVED", "POSTED"],
            ).aggregate(end=Max("payslip__payroll_run__period_end"))["end"]
            start = first_of_month
            if settled_end and settled_end >= start:
                start = (settled_end.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
            if rule.effective_from >= start:
                target = rule  # nothing settled was worked from it: correct it in place
            elif StatutoryRule.objects.filter(
                    company_id=rule.company_id, code=code, effective_from=start).exists():
                continue  # a retired row already holds that date; leave it to the accountant
            else:
                target = StatutoryRule(
                    company_id=rule.company_id, code=code, name=rule.name,
                    method=rule.method, employee_flat=rule.employee_flat,
                    employer_flat=rule.employer_flat, monthly_relief=rule.monthly_relief,
                    payable_account_id=rule.payable_account_id, effective_from=start)
            target.basis = basis
            target.employee_percent = emp
            target.employer_percent = empr
            target.basis_cap = cap
            target.reduces_taxable_pay = allowable
            target.employee_minimum = minimum
            target.expense_account_id = rule.expense_account_id if empr else None
            target.save()
            if bands is not None:
                target.tax_bands.all().delete()
                for lower, upper, percent in bands:
                    target.tax_bands.create(lower=lower, upper=upper, percent=percent)


class Migration(migrations.Migration):

    dependencies = [
        ('payroll', '0002_alter_payslip_options_alter_salarycomponent_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='statutoryrule',
            name='employee_minimum',
            field=models.DecimalField(decimal_places=2, default=Decimal('0'), help_text='The least withheld from anyone who is paid at all — SHIF is never below 300 a month. Leave at 0 for no floor.', max_digits=12),
        ),
        migrations.AlterField(
            model_name='statutoryrule',
            name='reduces_taxable_pay',
            field=models.BooleanField(default=False, help_text='Taken off the pay before income tax is worked out. In Kenya NSSF, SHIF and the housing levy all are (since 27 Dec 2024).'),
        ),
        migrations.RunPython(correct_opening_table, migrations.RunPython.noop),
    ]
