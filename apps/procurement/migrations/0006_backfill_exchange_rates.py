"""Store a rate on supplier bills saved before rates were kept (see sales 0005)."""
from django.db import migrations


def forwards(apps, schema_editor):
    from apps.accounting.legacy_rates import backfill
    backfill(apps, "Bill", "procurement", "total_amount", "total_amount_base", "PAYABLE", "credit_amount")


class Migration(migrations.Migration):

    dependencies = [
        ('procurement', '0005_bill_charge_account'),
        ('accounting', '0010_alter_journalentry_entry_type'),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
