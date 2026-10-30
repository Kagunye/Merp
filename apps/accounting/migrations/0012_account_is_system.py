"""Mark the accounts the ledger posts to by itself, so they cannot be retired.

Existing books are flagged from the same list of codes the chart seeder uses
(apps.accounting.models.SYSTEM_ACCOUNT_CODES, copied here so the migration
does not change if the list later does).
"""
from django.db import migrations, models

SYSTEM_CODES = [
    "1300", "1350", "1400", "1450", "1690",
    "2100", "2110", "2150", "2200", "2210",
    "3100", "3200", "3300",
    "4100", "4310", "4320", "4350",
    "5000", "5300",
    "6900", "6950",
    "7300", "7310",
]


def flag_system_accounts(apps, schema_editor):
    Account = apps.get_model("accounting", "Account")
    Account.objects.filter(code__in=SYSTEM_CODES).update(is_system=True)


class Migration(migrations.Migration):

    dependencies = [
        ("accounting", "0011_revaluation_share_and_minor_units"),
    ]

    operations = [
        migrations.AddField(
            model_name="account",
            name="is_system",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(flag_system_accounts, migrations.RunPython.noop),
    ]
