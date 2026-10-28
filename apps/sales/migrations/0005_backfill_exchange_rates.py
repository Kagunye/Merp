"""Store a rate on invoices and receipts saved before rates were kept.

Without one, any page that values an old document in a currency other than the
company's looked for a filed rate on its date, found none, and failed — the
sales home page among them.
"""
from django.db import migrations


def forwards(apps, schema_editor):
    from apps.accounting.legacy_rates import backfill
    backfill(apps, "Invoice", "sales", "total_amount", "total_amount_base", "RECEIVABLE", "debit_amount")
    backfill(apps, "CustomerPayment", "sales", "amount", "amount_base", "RECEIVABLE", "credit_amount")


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0004_customerpayment_status'),
        ('accounting', '0010_alter_journalentry_entry_type'),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
