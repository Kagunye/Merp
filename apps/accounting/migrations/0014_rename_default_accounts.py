"""Bring the clearer default names to companies already trading.

New companies get them from apps.core.services.CHART_OF_ACCOUNTS. An account
is renamed only while it still carries the old default name, matched exactly:
one a bookkeeper has renamed is theirs and is left alone, and so is a code that
means something else in a book seeded differently (the demo chart's 1200 is
Inventory). The code, type, parent and history do not change; only the label.

It runs in every database `migrate` is pointed at, like 0013.
"""
from django.db import migrations

RENAMES = [
    # (code, old default name, new name)
    ("1200", "Bank", "Bank Accounts"),
    ("4000", "Revenue", "Income"),
    ("4310", "Exchange Gain", "Realised Exchange Gain"),
    ("7200", "Interest Expense", "Interest on Loans & Overdrafts"),
    ("7300", "Exchange Loss", "Realised Exchange Loss"),
]


def _rename(apps, schema_editor, forward):
    Account = apps.get_model("accounting", "Account").objects.db_manager(
        schema_editor.connection.alias)
    for code, old, new in RENAMES:
        before, after = (old, new) if forward else (new, old)
        Account.filter(code=code, name=before).update(name=after)


def rename(apps, schema_editor):
    _rename(apps, schema_editor, forward=True)


def unrename(apps, schema_editor):
    _rename(apps, schema_editor, forward=False)


class Migration(migrations.Migration):

    dependencies = [
        ('accounting', '0013_withholding_commission_finance_accounts'),
    ]

    operations = [
        migrations.RunPython(rename, unrename),
    ]
